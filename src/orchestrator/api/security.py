"""API authentication and request limits.

The orchestrator runs tools. Depending on configuration those tools read files,
spawn processes, and make network calls. An unauthenticated endpoint that can
start an execution is therefore an unauthenticated endpoint that can do all of
those things, which makes authentication a correctness property of this service
rather than a deployment nicety.

Two rules follow from that, and both are enforced here rather than left to a
deployment checklist:

* **Binding to a non-loopback address without a token is refused at startup.**
  Not warned about. A service that listens on 0.0.0.0 with no auth is not a
  service with a configuration problem, it is an open remote-execution
  endpoint, and it should not be possible to start one by forgetting a flag.
* **Loopback with no token is allowed, and says so.** A single-user desktop
  install is the common case and demanding a token there only teaches people
  to paste the same string into every config. The reasoning is recorded in the
  audit trail so the posture is visible rather than assumed.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..errors import OrchestratorError

# Endpoints that must answer before a caller can prove anything.
#
# The probes are here because they are how a load balancer decides whether
# this process is alive, and a probe that needs a secret reports a false
# outage. "/" is here because it serves the console: a static page holding no
# data, which is where a person types the token in the first place. Gating it
# would only mean nobody can ever sign in.
#
# "/health" is deliberately NOT here. It returned the storage backend, the
# config fingerprint, absolute config paths, every registered validator, every
# provider, and the full model inventory. That is a map of the deployment, and
# a probe does not need it: a load balancer asks *whether* this instance can
# serve, not *what* it is made of. It now requires admin, and the detail lives
# at /v1/health.
#
# Everything under /v1 — every execution, every audit trail, every artifact —
# is outside this set and stays outside it.
PUBLIC_PATHS = frozenset({"/", "/live", "/ready"})

# 2 MB. An objective is prose; anything larger is a mistake or an attack, and
# reading it into memory before finding that out is the part worth avoiding.
DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024

ENV_TOKEN = "ORCHESTRATOR_API_TOKEN"  # noqa: S105 - a variable name, not a secret


def client_address(request, trusted_proxies: Sequence[str] = ()) -> str:
    """The caller's address, believing forwarded headers only from a trusted hop."""
    peer = getattr(request.client, "host", "") or "unknown"
    if not trusted_proxies or peer not in trusted_proxies:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer
    # Left-most entry is the original client; the rest are intermediaries.
    return forwarded.split(",")[0].strip() or peer


def _too_large(limit: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=413,
        content={
            "error": "payload_too_large",
            "message": f"request body exceeds {limit} bytes",
        },
    )


async def _read_bounded_body(request, limit: int) -> bool:
    """Read the body, stopping once it passes ``limit``. True if it did.

    The stream is consumed here and replayed to the application, because an
    exception raised from inside the receive callable is swallowed by the
    framework's body parsing and surfaces as a confusing 400. Buffering is
    bounded by ``limit`` itself — reading stops one byte over — so a large
    upload costs at most the limit in memory rather than its full size.
    """
    chunks: list[bytes] = []
    seen = 0
    over = False

    while True:
        message = await request._receive()
        if message.get("type") != "http.request":
            # Disconnect or similar: replay it and stop.
            chunks.append(b"")
            break
        body = message.get("body", b"") or b""
        seen += len(body)
        if seen > limit:
            over = True
            break
        chunks.append(body)
        if not message.get("more_body", False):
            break

    if over:
        return True

    replayed = b"".join(chunks)
    sent = False

    async def replay():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": replayed, "more_body": False}

    request._receive = replay
    return False


async def _count_auth_failure(limiter, client: str) -> None:
    """Charge a failed authentication against its own budget.

    Separate from the general limit because credential guessing is the one
    traffic pattern where a generous allowance is exactly wrong, and because
    a failed request has no principal to key on — only an address.
    """
    if limiter is None:
        return
    try:
        from .ratelimit import STRICT_CATEGORIES

        await _check_limit(
            limiter, f"auth_failure:client:{client}",
            STRICT_CATEGORIES["auth_failure"],
        )
    except Exception:  # noqa: BLE001,S110 - counting must not break the 401
        pass


async def _check_limit(limiter, key, limit):
    """Consult the limiter, whichever kind it is.

    The shared limiter queries PostgreSQL and is a coroutine; the in-process
    one is not. One call site either way, so a deployment cannot end up
    enforcing the weaker of the two by accident.
    """
    check = getattr(limiter, "check_async", None)
    if check is not None:
        return await check(key, limit)
    return limiter.check(key, limit)


async def _resolve(identities, presented):
    """Resolve a credential, whichever registry is in use.

    The in-process registry answers immediately; the shared one queries
    PostgreSQL. Awaiting the result either way keeps one code path in the
    middleware — and one code path is why the lifecycle checks cannot be
    enforced by one implementation and skipped by the other.
    """
    import inspect

    result = identities.resolve(presented)
    if inspect.isawaitable(result):
        return await result
    return result


def _record(metrics, method: str, *args, **kwargs) -> None:
    """Record a metric, never letting it fail a request.

    A middleware that raises while counting a denial converts a clean 401 into
    a 500, which is strictly worse than losing the datapoint.
    """
    if metrics is None:
        return
    try:
        getattr(metrics, method)(*args, **kwargs)
    except Exception:  # noqa: BLE001,S110 - metrics must never fail a request
        pass


def _new_request_id() -> str:
    import uuid

    return uuid.uuid4().hex[:16]


class InsecureBinding(OrchestratorError):
    """Refused to start: reachable from the network with no authentication."""


@dataclass
class SecurityConfig:
    """How this instance authenticates callers."""

    tokens: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    allowed_origins: tuple[str, ...] = ()
    # Peers whose X-Forwarded-For may be believed. Empty means believe none:
    # an unverified forwarded header lets any caller claim any client IP,
    # which matters wherever that IP ends up — audit records, rate limits.
    trusted_proxies: tuple[str, ...] = ()
    # Set only by an operator who has put their own authentication in front of
    # this process. Named to be uncomfortable to type by accident.
    allow_unauthenticated_network_access: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.tokens)

    @classmethod
    def from_env(cls, *, host: str = "127.0.0.1", **kwargs) -> SecurityConfig:
        """Read tokens from the environment.

        Tokens live in an environment variable, never in the config file, so
        that a config can be committed to version control without thought.
        Multiple comma-separated tokens are supported so a token can be
        rotated without a window where neither the old nor the new one works.
        """
        raw = os.environ.get(ENV_TOKEN, "")
        tokens = tuple(t.strip() for t in raw.split(",") if t.strip())
        return cls(tokens=tokens, host=host, **kwargs)

    def verify_binding(self, *, identities=None) -> None:
        """Refuse to serve unauthenticated on an address others can reach.

        ``identities`` is consulted as well as ``tokens``. Without it, a
        deployment that configures ``api.principals`` correctly and sets no
        legacy ``ORCHESTRATOR_API_TOKEN`` was refused at startup — the check
        only knew about one of the two credential sources, so the correct
        configuration was the one that could not run.
        """
        if self.enabled or self.allow_unauthenticated_network_access:
            return
        if identities is not None and getattr(identities, "enabled", False):
            return
        if is_loopback(self.host):
            return
        raise InsecureBinding(
            f"refusing to bind {self.host} without authentication: this API can "
            f"start executions that run tools, so exposing it to the network "
            f"unauthenticated would be a remote execution endpoint. Set "
            f"{ENV_TOKEN} to one or more comma-separated tokens, configure "
            f"api.principals with token_env variables that are set, bind to "
            f"127.0.0.1 instead, or — only if you have put your own "
            f"authentication in front of this process — pass "
            f"allow_unauthenticated_network_access.",
            host=self.host,
            remedy=f"set {ENV_TOKEN}",
        )

    def describe(self) -> dict[str, object]:
        """What an operator needs to see in a log line to know the posture."""
        return {
            "authentication": "bearer token" if self.enabled else "none",
            "tokens_configured": len(self.tokens),
            "host": self.host,
            "loopback_only": is_loopback(self.host),
            "max_body_bytes": self.max_body_bytes,
        }


def is_loopback(host: str) -> bool:
    """True when only this machine can reach the given bind address."""
    if not host:
        return False
    cleaned = host.strip().strip("[]")
    if cleaned.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        # A hostname that is not literally localhost. Assume it resolves
        # somewhere reachable: guessing otherwise fails open.
        return False


def token_matches(presented: str, allowed: Iterable[str]) -> bool:
    """Constant-time comparison against every accepted token.

    Every token is checked even after one matches. Returning early would make
    the response time depend on which token was presented, and on how many
    were configured.
    """
    ok = False
    for candidate in allowed:
        if hmac.compare_digest(presented, candidate):
            ok = True
    return ok


def extract_bearer(header: str | None) -> str | None:
    """Pull the credential out of an Authorization header."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def install(
    app,
    config: SecurityConfig,
    *,
    audit=None,
    identities=None,
    rate_limiter=None,
    rate_limit=None,
    metrics=None,
) -> None:
    """Attach authentication and body limits to a FastAPI application.

    Applied as middleware rather than a per-route dependency: a new endpoint
    added later is protected by default, and protecting it is not something a
    contributor has to remember to do.
    """
    from fastapi.responses import JSONResponse

    config.verify_binding(identities=identities)

    @app.middleware("http")
    async def _guard(request, call_next):
        # Content-Length is a claim made by the caller. Trusting it alone means
        # a chunked request, or one that simply lies, is unlimited. So it is
        # used as a cheap early rejection and the actual stream is counted too.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > config.max_body_bytes:
            _record(metrics, "security_event", "payload_too_large")
            return _too_large(config.max_body_bytes)

        # Every method that can carry a body, not just the three that usually
        # do. DELETE with a body is legal HTTP and was previously unbounded;
        # so was any method a future route might add. Methods that cannot have
        # a body cost nothing to check, because there is nothing to read.
        if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            if await _read_bounded_body(request, config.max_body_bytes):
                _record(metrics, "security_event", "payload_too_large")
                return _too_large(config.max_body_bytes)

        # A correlation id on every request, so a caller's report and a log
        # line can be tied together without guessing from timestamps.
        request_id = request.headers.get("x-request-id") or _new_request_id()
        request.state.request_id = request_id

        from .identity import (
            ANONYMOUS,
            Forbidden,
            Unauthorized,
            required_scope,
        )

        principal = ANONYMOUS
        path = request.url.path

        if path in PUBLIC_PATHS:
            # Optional, not skipped. A public path still identifies a caller
            # who presents a credential, so /ready can decide how much detail
            # to return. A bad credential here is simply not authenticated —
            # the path is public, so it is not an error.
            if identities is not None and identities.enabled:
                from .tokens import TokenExpired, TokenNotYetValid, TokenRevoked

                try:
                    principal = await _resolve(
                        identities,
                        extract_bearer(request.headers.get("authorization")),
                    )
                except (Unauthorized, TokenExpired, TokenNotYetValid, TokenRevoked):
                    # A public path does not require a credential, so a bad
                    # one simply means "not authenticated" rather than an error.
                    principal = ANONYMOUS

        if path not in PUBLIC_PATHS:
            authenticating = config.enabled or (
                identities is not None and identities.enabled
            )
            if authenticating:
                presented = extract_bearer(request.headers.get("authorization"))
                from .tokens import TokenExpired, TokenNotYetValid, TokenRevoked

                rejection = "credential not recognised"
                try:
                    if identities is not None and identities.enabled:
                        principal = await _resolve(identities, presented)
                    elif presented is None or not token_matches(presented, config.tokens):
                        raise Unauthorized("credential not recognised")
                except (TokenExpired, TokenNotYetValid, TokenRevoked) as exc:
                    # Distinct from "not recognised": these tell an operator
                    # to reissue or to stop looking, rather than sending them
                    # hunting for a typo.
                    rejection = {
                        TokenExpired: "credential has expired",
                        TokenNotYetValid: "credential is not valid yet",
                        TokenRevoked: "credential has been revoked",
                    }[type(exc)]
                    if audit is not None:
                        audit.record(
                            "api.unauthorized",
                            path=path,
                            client=client_address(request, config.trusted_proxies),
                            reason=rejection,
                            request_id=request_id,
                        )
                    _record(metrics, "security_event", "authentication")
                    return JSONResponse(
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer",
                                 "X-Request-ID": request_id},
                        content={
                            "error": "unauthorized",
                            "message": rejection,
                            "request_id": request_id,
                        },
                    )
                except Unauthorized:
                    if audit is not None:
                        # That it happened and from where. Never what was
                        # presented, which may be a real token typed into the
                        # wrong window.
                        audit.record(
                            "api.unauthorized",
                            path=path,
                            client=client_address(request, config.trusted_proxies),
                            presented_credential=bool(presented),
                            request_id=request_id,
                        )
                    _record(metrics, "security_event", "authentication")
                    await _count_auth_failure(
                        rate_limiter, client_address(request, config.trusted_proxies)
                    )
                    return JSONResponse(
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer",
                                 "X-Request-ID": request_id},
                        content={
                            "error": "unauthorized",
                            "message": (
                                "this endpoint requires a bearer token; send "
                                "Authorization: Bearer <token>"
                            ),
                            "request_id": request_id,
                        },
                    )

                scope = required_scope(request.method, path)
                try:
                    principal.require(scope)
                except Forbidden as exc:
                    if audit is not None:
                        audit.record(
                            "api.forbidden",
                            path=path,
                            method=request.method,
                            principal=principal.id,
                            tenant=principal.tenant,
                            required_scope=scope,
                            request_id=request_id,
                        )
                    _record(metrics, "security_event", "authorization")
                    return JSONResponse(
                        status_code=403,
                        headers={"X-Request-ID": request_id},
                        content={
                            "error": "forbidden",
                            "message": exc.message,
                            "required_scope": scope,
                            "request_id": request_id,
                        },
                    )

        request.state.principal = principal
        # After authentication, so a limit can be counted against a principal
        # rather than an address: a credential is a stabler identity than an
        # IP, which many callers share and one caller can move between.
        if rate_limiter is not None and rate_limit is not None:
            from .ratelimit import STRICT_CATEGORIES, categorise, limiter_key

            # Keyed on the authenticated principal where there is one. An IP
            # is the wrong unit: many callers share an egress address, one
            # caller moves between them, and a shared limit means one noisy
            # client throttles everybody behind the same NAT.
            identity = limiter_key(
                principal.id, client_address(request, config.trusted_proxies)
            )
            category = categorise(request.method, path)
            budget = STRICT_CATEGORIES.get(category, rate_limit)
            key = f"{category}:{identity}"

            decision = await _check_limit(rate_limiter, key, budget)
            if not decision.allowed:
                if audit is not None:
                    audit.record(
                        "api.rate_limited",
                        path=path,
                        principal=principal.id,
                        request_id=request_id,
                    )
                _record(metrics, "security_event", "rate_limit")
                return JSONResponse(
                    status_code=429,
                    headers={
                        "Retry-After": str(int(decision.retry_after) + 1),
                        "X-Request-ID": request_id,
                        "X-RateLimit-Limit": str(decision.limit),
                        "X-RateLimit-Remaining": "0",
                    },
                    content={
                        "error": "rate_limited",
                        "message": (
                            f"rate limit of {decision.limit} requests exceeded; "
                            f"retry in {decision.retry_after:.0f}s"
                        ),
                        "request_id": request_id,
                    },
                )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    if config.allowed_origins:
        from fastapi.middleware.cors import CORSMiddleware

        # Only ever the origins an operator named. The console is served from
        # this same process, so the common deployment needs no CORS at all.
        #
        # "*" with credentials is refused rather than silently downgraded: the
        # browser would reject it anyway, and an operator who wrote it is
        # expecting something the setting cannot deliver.
        if "*" in config.allowed_origins:
            raise InsecureBinding(
                "a wildcard CORS origin cannot be combined with credentials. "
                "List the specific origins that may call this API.",
                remedy="name explicit origins",
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            max_age=600,
        )
