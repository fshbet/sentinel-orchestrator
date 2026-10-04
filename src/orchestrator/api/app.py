"""REST API.

Optional (spec section 50). Versioned under ``/v1``, with explicit schemas and
structured errors. It exposes the same operations as the CLI and nothing more:
there is no endpoint that can bypass a gate, grant a tool, or force an
execution to completion.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config.loader import load
from ..core.domain.enums import (
    TERMINAL_EXECUTION_STATUSES,
    ExecutionStatus,
    OrchestrationPattern,
    PlanStrategy,
)
from ..core.domain.models import ResourceLimits
from ..errors import (
    ConfigurationError,
    NotFound,
    OrchestratorError,
    PolicyViolation,
)
from ..platform import Orchestrator
from ..validation.advocate import summarise as advocate_summary
from . import settings as _settings
from .security import SecurityConfig
from .security import install as install_security


def _console_path():
    """Locate ui/console.html.

    Three layouts, checked in order, because the file lives in a different
    place in each and a lookup that only handles the checkout silently
    produces an API with no console once installed:

    1. Packaged inside ``orchestrator/ui/`` — how an installed wheel or a
       container image carries it. Checked first because it is the only one
       that does not depend on the working directory.
    2. ``ORCHESTRATOR_CONSOLE_PATH`` — an explicit override for operators who
       serve a customised console.
    3. Walking up from this file, then from the working directory — the
       development checkout, where ``ui/`` sits beside ``src/``.
    """
    import os
    from pathlib import Path

    packaged = Path(__file__).resolve().parent.parent / "ui" / "console.html"
    if packaged.is_file():
        return packaged

    override = os.environ.get("ORCHESTRATOR_CONSOLE_PATH", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None

    here = Path(__file__).resolve()
    roots = [here.parent.parent, *here.parents, Path.cwd(), *Path.cwd().parents]
    for base in roots:
        candidate = base / "ui" / "console.html"
        if candidate.is_file():
            return candidate
    return None


API_VERSION = "v1"

_LOG = logging.getLogger("orchestrator.api")

# Error context keys that carry filesystem layout or credentials. The message
# itself is authored by this codebase and safe to return; the structured
# context is where absolute paths and tokens accumulate.
_UNSAFE_ERROR_KEYS = frozenset(
    {
        "path",
        "root",
        "cwd",
        "file",
        "filename",
        "directory",
        "workspace",
        "token",
        "api_key",
        "key",
        "secret",
        "password",
        "authorization",
        "traceback",
        "stack",
    }
)


def _safe_error(exc) -> dict:
    """An error payload with internals removed."""
    from ..observability.logging import redact

    raw = exc.to_dict() if hasattr(exc, "to_dict") else {"message": str(exc)}
    if not isinstance(raw, dict):
        return {"message": str(exc)}

    def strip(node, depth=0):
        if depth > 6 or not isinstance(node, dict):
            return node
        return {
            key: strip(value, depth + 1)
            for key, value in node.items()
            if key.lower() not in _UNSAFE_ERROR_KEYS
        }

    # Recursive: to_dict() nests the useful context under "details", so a
    # single-level filter left every path in place.
    return redact(strip(raw))


def _shared_identities(config, api_section, state):
    """Token state in PostgreSQL, so revocation reaches every replica.

    The pool is *not* opened here. `create_app` runs before uvicorn's event
    loop exists, and an asyncpg pool is bound to the loop that created it —
    opening one now produced a pool attached to a loop that was immediately
    closed, and every request then failed with "Event loop is closed". A
    factory is handed over instead and awaited once, on the serving loop.

    `token_count` is therefore derived from configuration rather than from a
    query: the insecure-binding check needs it before any request, and the
    question it answers — "is authentication configured" — is a question about
    configuration, not about the database.
    """
    import os

    from ..core.state.postgres_store import resolve_dsn
    from .shared_tokens import PostgresTokenStore, SharedIdentityRegistry

    section = config.section("storage").get("postgres", {}) or {}
    dsn = resolve_dsn(section)
    multi_tenant = str(api_section.get("tenancy", "single")).lower() == "multi"

    async def factory():
        import asyncpg

        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=int(section.get("max_connections", 10)),
            command_timeout=float(section.get("command_timeout", 30.0)),
        )
        return pool

    store = PostgresTokenStore(factory=factory)
    state["token_store"] = store

    # Count what configuration declares, not what the database holds.
    configured = sum(
        1
        for raw in (api_section.get("principals") or [])
        if isinstance(raw, dict)
        and os.environ.get(str(raw.get("token_env") or ""), "").strip()
    )
    configured += len(
        [t for t in os.environ.get("ORCHESTRATOR_API_TOKEN", "").split(",") if t.strip()]
    )

    return SharedIdentityRegistry(
        store,
        multi_tenant=multi_tenant,
        token_count=configured,
        seed=api_section,
    )


def create_app(
    *,
    config_path: str | None = None,
    orchestrator: Orchestrator | None = None,
    security: "SecurityConfig | None" = None,
    identity_registry: "Any | None" = None,
    metrics_collector: "Any | None" = None,
):
    """Build the API.

    ``security`` defaults to reading a token from the environment and binding
    loopback-only. See :mod:`orchestrator.api.security` for why an
    unauthenticated non-loopback bind is refused rather than warned about.
    """
    try:
        from fastapi import Body, FastAPI, Query, Request
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise OrchestratorError(
            "the REST API requires fastapi and pydantic; install"
            " universal-orchestrator[api]"
        ) from exc

    class StartRequest(BaseModel):
        objective: str = Field(..., min_length=1, description="What to accomplish.")
        context: dict[str, Any] = Field(default_factory=dict)
        pattern: str | None = Field(None, description="Force an orchestration pattern.")
        plan_strategy: str | None = Field(None, description="Force a plan strategy.")
        run: bool = Field(True, description="Run immediately, or only create.")
        limits: dict[str, Any] | None = None

    class ApprovalRequest(BaseModel):
        approved: bool = True
        response: Any = None
        responder: str | None = None

    class KeyRequest(BaseModel):
        # An empty string is meaningful: it removes the credential. That is the
        # only way to un-set one from the console, so it must not be rejected
        # as a missing field.
        value: str = Field("", description="The credential. Empty removes it.")

    class ProfileRequest(BaseModel):
        profile: str = Field(..., description="development, internal-pilot, or production.")

    from contextlib import asynccontextmanager

    state: dict[str, Any] = {"orchestrator": orchestrator, "lock": asyncio.Lock()}

    @asynccontextmanager
    async def lifespan(_app):
        # Seed shared token state here, not in create_app: this runs on the
        # loop that will serve requests, which is the loop the pool must
        # belong to.
        store = state.get("token_store")
        if store is not None and getattr(identities, "seed", None):
            await store.seed_from_config(identities.seed)

        # Credentials saved through the console live in a file, not the
        # environment, so they must be loaded before any provider is built.
        # Anything already exported wins - see settings.load_secrets.
        try:
            loaded = _settings.load_secrets(
                load(paths=[config_path] if config_path else None)
            )
            if loaded:
                _LOG.info("loaded %d stored credential(s)", len(loaded))
        except Exception:  # noqa: BLE001 - never block startup on this
            _LOG.warning("could not load stored credentials", exc_info=True)
        yield
        # Only close what this app created; an injected orchestrator is the
        # caller's to manage.
        if state["orchestrator"] is not None and orchestrator is None:
            await state["orchestrator"].close()
        store = state.get("token_store")
        if store is not None:
            await store.close()

    app = FastAPI(
        title="Universal AI Orchestration Platform",
        version="0.1.0",
        description="Domain-agnostic, MCP-first orchestration.",
        lifespan=lifespan,
    )

    # Before any route is registered, so nothing can be reached unguarded.
    identities = identity_registry
    loaded_config = None
    if identities is None:
        from ..config.loader import load as _load
        from .identity import IdentityRegistry

        try:
            loaded_config = _load(paths=[config_path] if config_path else None)
            api_section = loaded_config.section("api")
        except Exception:  # noqa: BLE001 - config errors surface on first use
            api_section = {}

        # Shared token state when the deployment has a shared database.
        # Without it, revoking on one replica leaves the credential working on
        # the others until they restart — which makes "revoked" untrue for
        # roughly half the traffic and says nothing about it.
        backend = ""
        if loaded_config is not None:
            backend = str(loaded_config.get("storage.backend", "")).lower()

        if backend == "postgres":
            identities = _shared_identities(loaded_config, api_section, state)
        else:
            identities = IdentityRegistry.from_config(api_section)

    from ..observability.metrics import Metrics

    # One collector for this application's lifetime. Handed to the
    # middleware for API-level signals and to the orchestrator for
    # engine-level ones, so /metrics has a single source.
    app_metrics = metrics_collector if metrics_collector is not None else Metrics()
    state["metrics"] = app_metrics

    resolved_security = security or SecurityConfig.from_env()

    from .ratelimit import build as build_limiter

    try:
        from ..config.loader import load as _load2

        _rate_config = _load2(paths=[config_path] if config_path else None).get(
            "api.rate_limit", {}
        )
    except Exception:  # noqa: BLE001 - config errors surface on first use
        _rate_config = {}
    limiter, limit = build_limiter(_rate_config)

    # The shared limiter needs the connection pool, which only exists once
    # the shared token store has been built. Falling back to the in-process
    # limiter would silently halve the enforced rate, so it is an error.
    if limiter is None:
        store = state.get("token_store")
        if store is None:
            raise ConfigurationError(
                "api.rate_limit.backend is 'postgres' but storage.backend is "
                "not: the shared limiter counts in the same database as the "
                "shared token state. Set storage.backend: postgres, or choose "
                "another rate_limit backend."
            )
        from .ratelimit import PostgresRateLimiter

        # Same lazily-opened pool as the token store, so there is one pool per
        # process rather than two.
        limiter = PostgresRateLimiter(factory=store._acquire_pool)

    install_security(
        app,
        resolved_security,
        identities=identities,
        rate_limiter=limiter,
        rate_limit=limit,
        metrics=app_metrics,
    )

    async def get_orchestrator() -> Orchestrator:
        if state["orchestrator"] is None:
            async with state["lock"]:
                if state["orchestrator"] is None:
                    config = load(paths=[config_path] if config_path else None)
                    state["orchestrator"] = await Orchestrator.create(
                        config=config, metrics=state.get("metrics")
                    )
        return state["orchestrator"]

    def execution_payload(execution) -> dict[str, Any]:
        return {
            "id": execution.id,
            "status": execution.status.value,
            "confidence": execution.confidence.value,
            "objective": execution.objective,
            "summary": execution.summary,
            "plan_version": execution.plan_version,
            "usage": execution.usage.to_dict(),
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "status": t.status.value,
                    "depends_on": t.dependencies,
                    "agent": t.assigned_agent,
                    "model": t.assigned_model,
                    "attempts": t.attempts,
                    "summary": t.result.summary if t.result else "",
                }
                for t in execution.tasks.values()
            ],
            "artifacts": [a.to_dict() for a in execution.artifacts],
            "validations": [
                {
                    "validator": v.validator,
                    "target": v.target_id,
                    "passed": v.passed,
                    "mandatory": v.mandatory,
                    "confidence": v.confidence.value,
                    "message": v.message,
                }
                for v in execution.validations
            ],
            "approvals": [a.to_dict() for a in execution.approvals],
            "failures": [f.to_dict() for f in execution.failures],
            # The devil's advocate's objections live in validation evidence,
            # which is not otherwise serialised. Lift them out so an interface
            # can show the case against a result without re-running anything.
            "advocate": advocate_summary(execution.validations),
        }

    @app.exception_handler(OrchestratorError)
    async def orchestrator_error_handler(request, exc: OrchestratorError):
        # A refusal is not a malformed request. The console shows the two
        # differently - one is "fix your input", the other is "this deployment
        # will not let you do that", and collapsing both into 400 made a
        # deliberate policy decision look like a client bug.
        if isinstance(exc, NotFound):
            status = 404
        elif isinstance(exc, PolicyViolation):
            status = 403
        else:
            status = 400
        return JSONResponse(
            status_code=status,
            content={
                "error": _safe_error(exc),
                "request_id": getattr(request.state, "request_id", ""),
            },
            headers={"X-Request-ID": getattr(request.state, "request_id", "")},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request, exc: Exception):
        """An unexpected failure returns an id, not a stack trace.

        A traceback in an HTTP response is a map of the filesystem, the
        package versions, and often the arguments in scope. The detail goes to
        the log, where it belongs, tied to the same request id the caller sees.
        """
        request_id = getattr(request.state, "request_id", "")
        _LOG.exception("unhandled API error", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": "internal_error",
                    "message": (
                        "the request could not be completed; quote the request "
                        "id when reporting this"
                    ),
                },
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    # -- ownership ---------------------------------------------------------
    #
    # Enforced on the object rather than on the query. Filtering a list by
    # tenant is easy to get right and easy to forget on the next endpoint;
    # checking the fetched execution's own tenant means a caller who guesses
    # an id gets the same answer as one who guesses a nonexistent id.

    TENANT_KEY = "__tenant"
    OWNER_KEY = "__owner"

    def caller(http_request) -> Any:
        from .identity import ANONYMOUS

        return getattr(http_request.state, "principal", ANONYMOUS)

    def stamp_ownership(context: dict[str, Any], principal) -> dict[str, Any]:
        merged = dict(context or {})
        merged[TENANT_KEY] = principal.tenant
        merged[OWNER_KEY] = principal.id
        return merged

    def check_ownership(execution, principal) -> None:
        """404, not 403: a wrong-tenant id must not confirm the id exists."""
        tenant = (execution.context or {}).get(TENANT_KEY)
        if not principal.owns(tenant):
            log = audit_log()
            if log is not None:
                log.record(
                    "api.forbidden",
                    reason="cross_tenant_access",
                    execution_id=execution.id,
                    principal=principal.id,
                    principal_tenant=principal.tenant,
                    object_tenant=tenant,
                )
            raise NotFound(f"execution {execution.id} was not found")

    def audit_log():
        """The audit log, if one exists.

        Guarded on `.record` because Orchestrator.audit is also the name of a
        coroutine that reads a trail; asking for the attribute alone returns
        that method and silently does nothing useful.
        """
        orc = state.get("orchestrator")
        if orc is None:
            return None
        log = getattr(orc, "audit_log", None)
        if log is not None and hasattr(log, "record"):
            return log
        candidate = getattr(orc, "audit", None)
        return candidate if hasattr(candidate, "record") else None

    async def owned_execution(execution_id: str, http_request):
        orc = await get_orchestrator()
        execution = await orc.status(execution_id)
        check_ownership(execution, caller(http_request))
        return orc, execution

    # -- executions --------------------------------------------------------

    @app.post(f"/{API_VERSION}/executions", status_code=201)
    async def create_execution(http_request: Request, request: StartRequest = Body(...)):
        orc = await get_orchestrator()
        principal = caller(http_request)
        kwargs: dict[str, Any] = {"context": stamp_ownership(request.context, principal)}
        if request.pattern:
            kwargs["pattern"] = OrchestrationPattern(request.pattern)
        if request.plan_strategy:
            kwargs["plan_strategy"] = PlanStrategy(request.plan_strategy)
        if request.limits:
            kwargs["limits"] = ResourceLimits.from_dict(request.limits)
        if request.run:
            execution = await orc.run(request.objective, **kwargs)
        else:
            execution = await orc.start(request.objective, **kwargs)
        return execution_payload(execution)

    @app.get(f"/{API_VERSION}/executions")
    async def list_executions(
        http_request: Request,
        status: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        orc = await get_orchestrator()
        principal = caller(http_request)
        parsed = ExecutionStatus(status) if status else None

        # Over-fetch and filter, because the store does not index tenant.
        # Slower than a WHERE clause and correct without a migration; the
        # honest tradeoff is noted in docs/durable-execution.md.
        fetch = limit if not identities_multi_tenant() else min(limit * 4 + offset, 500)
        summaries = await orc.list(status=parsed, limit=fetch, offset=offset)

        if identities_multi_tenant():
            visible = []
            for summary in summaries:
                try:
                    execution = await orc.status(summary.id)
                except OrchestratorError:
                    continue
                if principal.owns((execution.context or {}).get(TENANT_KEY)):
                    visible.append(summary)
                if len(visible) >= limit:
                    break
            summaries = visible

        return {"executions": [s.to_dict() for s in summaries]}

    def identities_multi_tenant() -> bool:
        return bool(identities is not None and identities.multi_tenant)

    @app.get(f"/{API_VERSION}/executions/{{execution_id}}")
    async def get_execution(execution_id: str, http_request: Request):
        _, execution = await owned_execution(execution_id, http_request)
        return execution_payload(execution)

    @app.post(f"/{API_VERSION}/executions/{{execution_id}}/pause")
    async def pause_execution(execution_id: str, http_request: Request):
        orc, _ = await owned_execution(execution_id, http_request)
        return execution_payload(await orc.pause(execution_id))

    @app.post(f"/{API_VERSION}/executions/{{execution_id}}/resume")
    async def resume_execution(execution_id: str, http_request: Request):
        orc, _ = await owned_execution(execution_id, http_request)
        return execution_payload(await orc.resume(execution_id))

    @app.post(f"/{API_VERSION}/executions/{{execution_id}}/cancel")
    async def cancel_execution(
        execution_id: str, http_request: Request, reason: str = Query("")
    ):
        orc, _ = await owned_execution(execution_id, http_request)
        return execution_payload(await orc.cancel(execution_id, reason=reason))

    @app.delete(f"/{API_VERSION}/executions/{{execution_id}}", status_code=200)
    async def delete_execution(execution_id: str, http_request: Request):
        """Delete one run and everything recorded about it.

        Refuses while the run is still live. Deleting the record of something
        that is currently executing does not stop it - it removes the only
        place its progress, its approvals, and its audit trail are written
        down, leaving work running that nothing is tracking. Cancel first.

        The deletion is permanent and takes the audit trail with it, which is
        the point: this is how someone removes a run whose objective or result
        should not be sitting on the disk. It is recorded in the audit log of
        the *system*, not of the deleted run, for the obvious reason.
        """
        orc, execution = await owned_execution(execution_id, http_request)
        if execution.status not in TERMINAL_EXECUTION_STATUSES:
            raise ConfigurationError(
                f"this run is {execution.status.value}, not finished; cancel it "
                "before deleting, so nothing keeps running unrecorded.",
                remedy="POST .../cancel first, then delete",
            )

        log = audit_log()
        if log is not None:
            log.record(
                "execution.deleted",
                execution_id=execution_id,
                actor=getattr(caller(http_request), "id", "local"),
                status=execution.status.value,
            )
        await orc.store.delete(execution_id)
        return {"deleted": execution_id}

    @app.get(f"/{API_VERSION}/executions/{{execution_id}}/audit")
    async def get_audit(
        execution_id: str, http_request: Request, after: int = Query(0, ge=0)
    ):
        orc, _ = await owned_execution(execution_id, http_request)
        events = await orc.audit(execution_id, after=after)
        return {"events": [e.to_dict() for e in events]}

    @app.get(f"/{API_VERSION}/executions/{{execution_id}}/artifacts")
    async def get_artifacts(execution_id: str, http_request: Request):
        orc, _ = await owned_execution(execution_id, http_request)
        return {"artifacts": [a.to_dict() for a in await orc.artifacts(execution_id)]}

    @app.post(f"/{API_VERSION}/executions/{{execution_id}}/approvals/{{approval_id}}")
    async def respond_to_approval(
        execution_id: str,
        approval_id: str,
        http_request: Request,
        request: ApprovalRequest = Body(...),
    ):
        orc, _ = await owned_execution(execution_id, http_request)
        execution = await orc.approve(
            execution_id,
            approval_id,
            approved=request.approved,
            response=request.response,
            responder=request.responder or caller(http_request).id,
        )
        return execution_payload(execution)

    # -- tokens ------------------------------------------------------------
    #
    # Admin only. Lists metadata and revokes by id; there is no endpoint that
    # returns or issues a secret over HTTP — a credential that can be fetched
    # from an API is a credential the API can leak.

    @app.get(f"/{API_VERSION}/tokens")
    async def list_tokens():
        """Token metadata: id, principal, scopes, expiry, revocation status."""
        import inspect

        if identities is None or not hasattr(identities, "list_tokens"):
            return {"tokens": []}
        listed = identities.list_tokens()
        if inspect.isawaitable(listed):
            listed = await listed
        return {"tokens": listed}

    @app.post(f"/{API_VERSION}/tokens/{{token_id}}/revoke")
    async def revoke_token(token_id: str, http_request: Request, reason: str = Query("")):
        """Revoke a token by id. Effective on the next request, no restart.

        Per-instance: behind a load balancer this reaches the replica that
        served the call. To be certain a credential is dead everywhere,
        remove it from its token_env and roll the deployment.
        """
        from fastapi.responses import JSONResponse

        if identities is None or not hasattr(identities, "revoke"):
            return JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "message": "token management is not enabled",
                },
            )

        import inspect

        actor = caller(http_request)
        # Both registries take the same signature, so no branching here.
        revoked = identities.revoke(
            token_id, reason=reason or "revoked via API", actor=actor.id
        )
        if inspect.isawaitable(revoked):
            revoked = await revoked
        if not revoked:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "message": f"no token with id {token_id}"},
            )

        log = audit_log()
        if log is not None:
            log.record(
                "token.revoked",
                token_id=token_id,
                revoked_by=actor.id,
                reason=reason,
                request_id=getattr(http_request.state, "request_id", ""),
            )
        distributed = bool(
            getattr(getattr(identities, "tokens", None), "distributed", False)
        )
        return {
            "revoked": True,
            "token_id": token_id,
            # True when token state is shared: the revocation is already
            # visible to every replica. False means this instance only.
            "distributed": distributed,
        }

    # -- registries --------------------------------------------------------

    @app.get(f"/{API_VERSION}/agents")
    async def list_agents():
        orc = await get_orchestrator()
        return {"agents": orc.describe()["agents"]}

    @app.get(f"/{API_VERSION}/capabilities")
    async def list_capabilities():
        orc = await get_orchestrator()
        return {"capabilities": orc.describe()["capabilities"]}

    @app.get(f"/{API_VERSION}/tools")
    async def list_tools():
        orc = await get_orchestrator()
        return {"tools": orc.describe()["tools"]}

    @app.get(f"/{API_VERSION}/models")
    async def list_models():
        orc = await get_orchestrator()
        return {"models": orc.describe()["models"]}

    @app.get(f"/{API_VERSION}/skills")
    async def list_skills():
        orc = await get_orchestrator()
        return {"skills": orc.describe()["skills"]}

    @app.get(f"/{API_VERSION}/workflows")
    async def list_workflows():
        orc = await get_orchestrator()
        return {"workflows": orc.describe()["workflows"]}

    @app.get(f"/{API_VERSION}/mcp")
    async def list_mcp():
        orc = await get_orchestrator()
        if orc.mcp is None:
            return {"servers": []}
        return {"servers": await orc.mcp.health()}

    # -- settings ----------------------------------------------------------
    #
    # Read by anyone who may reach the API; written only in the development
    # profile. See orchestrator.api.settings for why that asymmetry is the
    # whole design rather than a limitation.

    def _config():
        """The loaded configuration, without forcing a full orchestrator."""
        orc = state.get("orchestrator")
        if orc is not None and getattr(orc, "config", None) is not None:
            return orc.config
        return load(paths=[config_path] if config_path else None)

    def _invalidate_orchestrator() -> None:
        """Drop the cached orchestrator so the next call rebuilds it.

        Providers read their credential once, at construction. Without this a
        key saved through the console would sit in the environment doing
        nothing until someone restarted the process - which looks exactly like
        the key being wrong.
        """
        state["orchestrator"] = None

    @app.get(f"/{API_VERSION}/settings")
    async def get_settings():
        return _settings.describe(_config())

    @app.put(f"/{API_VERSION}/settings/keys/{{env_name}}")
    async def put_provider_key(
        env_name: str, http_request: Request, request: KeyRequest = Body(...)
    ):
        config = _config()
        result = _settings.set_provider_key(config, env_name, request.value)
        _invalidate_orchestrator()

        log = audit_log()
        if log is not None:
            # The name, never the value - and only that a change happened.
            log.record(
                "settings.credential_changed",
                actor=getattr(caller(http_request), "id", "local"),
                variable=result["env"],
                present=result.get("set", False),
            )
        return {"key": result, "reload": "applied to the next run"}

    @app.put(f"/{API_VERSION}/settings/profile")
    async def put_profile(http_request: Request, request: ProfileRequest = Body(...)):
        config = _config()
        written = _settings.set_profile(config, request.profile, config_path=config_path)
        log = audit_log()
        if log is not None:
            log.record(
                "settings.profile_changed",
                actor=getattr(caller(http_request), "id", "local"),
                profile=request.profile,
            )
        return {
            "profile": request.profile,
            "written": Path(written).name,
            "restart_required": True,
            "note": (
                "Saved. A profile decides tool permissions, egress rules, and "
                "whether a token is required, so it takes effect on restart "
                "rather than mid-request."
            ),
        }

    # -- console -----------------------------------------------------------

    @app.get("/")
    async def console():
        """The web console.

        Served from the same origin as the API so the page can call /v1
        directly. Missing is not an error: the API is fully usable without a
        front end, so say what is absent rather than returning a 404 that
        looks like a broken deployment.
        """
        from fastapi.responses import HTMLResponse

        page = _console_path()
        if page is None:
            return HTMLResponse(
                "<h1>Orchestrator</h1><p>The API is running. The web console "
                "(<code>ui/console.html</code>) was not found alongside this "
                "install, so only the API is available here.</p>",
                status_code=200,
            )
        return HTMLResponse(page.read_text(encoding="utf-8"))

    # -- operations --------------------------------------------------------

    @app.get("/live")
    async def live():
        """Is the process up? Answers without touching models or storage.

        Separate from readiness on purpose: a liveness probe that checks
        dependencies restarts a healthy process because something downstream
        is briefly unavailable, which is how a partial outage becomes a
        total one.
        """
        return {"status": "alive"}

    @app.get("/ready")
    async def ready(http_request: Request):
        """Can this instance actually serve work?

        Reachable without a credential, because that is what a load balancer
        has. The *detail* — provider names, model ids, MCP servers, exception
        text — is not: it maps the deployment for anyone who can reach the
        port. An unauthenticated caller gets ready or not-ready and nothing
        else; an authenticated one gets the report.
        """
        from fastapi.responses import JSONResponse

        from .identity import ADMIN

        principal = getattr(http_request.state, "principal", None)
        # Both conditions: ANONYMOUS deliberately holds admin scope so a
        # loopback console is usable, so scope alone does not distinguish an
        # authenticated caller from an unauthenticated one.
        detailed = (
            principal is not None
            and principal.source != "unauthenticated"
            and principal.has(ADMIN)
        )
        # With no authentication configured at all, there is no one to hide
        # the detail from: the network boundary is what is being relied on.
        if principal is not None and principal.source == "unauthenticated":
            detailed = not (identities is not None and identities.enabled)

        try:
            orc = await get_orchestrator()
            report = await orc.health()
        except Exception as exc:  # noqa: BLE001 - report, never crash the probe
            content: dict[str, Any] = {"status": "not_ready"}
            if detailed:
                content["reason"] = str(exc)
            return JSONResponse(status_code=503, content=content)

        if not orc.router.all_models():
            content = {"status": "not_ready"}
            if detailed:
                content["reason"] = "no models are registered, so no work can run"
                content["detail"] = report
            return JSONResponse(status_code=503, content=content)

        return {"status": "ready", "detail": report} if detailed else {"status": "ready"}

    async def _health_report() -> dict[str, Any]:
        orc = await get_orchestrator()
        return await orc.health()

    @app.get(f"/{API_VERSION}/health")
    async def detailed_health():
        """Full health detail. Requires admin.

        Names the storage backend, every configured provider and model, the
        registered validators, and the config sources — which together are a
        map of the deployment. A load balancer needs none of it; it needs
        /live and /ready. This is for an operator holding a credential.
        """
        return await _health_report()

    @app.get("/health")
    async def health():
        """Kept for callers that already point here. Also requires admin now.

        It used to be public and returned the same detail, which meant absolute
        config paths and the full model inventory were readable by anyone who
        could reach the port. Retained rather than removed so existing
        authenticated callers keep working; /v1/health is the versioned name.
        """
        return await _health_report()

    @app.get("/metrics")
    async def metrics():
        """Prometheus exposition, from the Metrics collector alone.

        Single source of truth on purpose. The previous implementation built
        its own gauge text inline, which meant two exporters with two sets of
        cardinality rules — and only one of them had any. Point-in-time gauges
        that genuinely need a query (executions by status, registered tools)
        are refreshed *through* the collector here, so they inherit the same
        label validation as everything else.
        """
        from fastapi.responses import PlainTextResponse

        try:
            orc = await get_orchestrator()
            for status in ExecutionStatus:
                rows = await orc.list(status=status, limit=200)
                app_metrics.gauge(
                    "orchestrator_executions_current",
                    len(rows),
                    help="Executions currently in each status.",
                    status=status.value,
                )
            app_metrics.gauge(
                "orchestrator_tools_registered",
                len(orc.tools.list()),
                help="Tools currently registered.",
            )
        except Exception:  # noqa: BLE001 - a scrape must not fail on a gauge
            # Whatever else is wrong, the recorded counters are still worth
            # serving: they are how you find out what is wrong.
            _LOG.warning("could not refresh point-in-time gauges", exc_info=True)

        return PlainTextResponse(
            app_metrics.render(), media_type="text/plain; version=0.0.4"
        )

    # Exposed so callers (the CLI banner) can report the posture that is
    # actually enforced rather than constructing their own registry — which
    # is how the container ended up bypassing the shared token store.
    app.state.identities = identities
    app.state.rate_limiter = limiter
    return app
