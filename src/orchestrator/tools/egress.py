"""Outbound network policy: where a tool may connect, and how.

A model chooses the URL. That single fact is what makes this module necessary,
and it is why every check here is applied to the *resolved address*, not to the
string the model produced. A hostname is an assertion; an IP address is a
destination.

Server-side request forgery is the specific concern. An orchestrator that will
fetch a URL on request is, without these controls, a way to reach everything
the orchestrator's network position can reach — internal admin panels, other
services on the host, and above all the cloud metadata endpoint, where a single
GET to 169.254.169.254 returns credentials for the whole account.

The controls, and why each one exists rather than the obvious weaker version:

* **The allowlist is mandatory and fails closed.** An empty allowlist denies
  everything. The tempting alternative — treat empty as unset, therefore
  unrestricted — turns "the operator has not configured this yet" into
  "the operator has permitted everything".
* **Every redirect hop is revalidated.** Checking only the first URL is not a
  weaker check, it is no check: an allowed host that answers ``302 Location:
  http://169.254.169.254/`` bypasses it entirely.
* **All resolved addresses must pass, not the first.** A rebinding attacker
  returns one public and one internal answer and lets the client choose. If
  any answer is blocked, the name is refused.
* **Metadata addresses are refused even when private ranges are allowed.** An
  internal-services deployment needs 10.0.0.0/8; it does not need the endpoint
  that hands out its own credentials. These are separate switches on purpose,
  and the metadata one has no switch at all by design — see
  ``allow_cloud_metadata``.
* **HTTPS by default.** Plain HTTP is available for development, named as
  such.

**What this module does not do, stated plainly: it does not pin connections.**

Validation resolves the hostname and checks every address it gets back. httpx
then resolves the name *again* when it opens the socket. Between those two
resolutions an attacker who controls DNS for an allowlisted host can return a
public address to the check and an internal one to the connection. That is a
real time-of-check-to-time-of-use window and it is open.

Closing it means pinning the socket to the validated address, which breaks TLS
for any virtual-hosted endpoint: the certificate is validated against the
hostname, and connecting by IP loses the SNI that selects it. Doing that
correctly is a custom transport with per-request SNI overrides, and getting it
subtly wrong produces something worse than the current honest gap — a control
that appears to work.

So there is no ``pin_dns`` setting and no ``pinned_transport``. An earlier
version of this file described both; neither existed, and ``describe()``
reported ``dns_pinning: true`` to operators reading their posture. **If your
threat model includes an attacker who controls DNS for a host on your
allowlist, put an egress proxy in front of this process and allowlist there.**
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from ..errors import PermissionDenied

# Reads and writes are different privileges even over the same protocol.
READ_METHODS = ("GET", "HEAD", "OPTIONS")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
KNOWN_METHODS = READ_METHODS + WRITE_METHODS

# Link-local addresses that specific cloud providers use to serve instance
# credentials and configuration. Called out by address rather than left to the
# link-local check so that a deployment which legitimately needs link-local
# still cannot reach these.
CLOUD_METADATA_ADDRESSES = (
    "169.254.169.254",  # AWS, Azure, GCP, DigitalOcean, OpenStack
    "169.254.170.2",  # AWS ECS task metadata
    "100.100.100.200",  # Alibaba Cloud
    "192.0.0.192",  # Oracle Cloud
    "fd00:ec2::254",  # AWS IMDSv2 over IPv6
)

# Hostnames that resolve to metadata services. Blocked by name as well as by
# address because a split-horizon resolver can point them anywhere.
CLOUD_METADATA_HOSTNAMES = (
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_DOCUMENTATION_V4 = (
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
)
_DOCUMENTATION_V6 = (ipaddress.ip_network("2001:db8::/32"),)


def _is_ip_literal(host: str) -> bool:
    """True when the host is written as an address rather than a name."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def classify_address(address: str) -> str | None:
    """Name the reason an address is sensitive, or None if it is ordinary.

    Returning a category rather than a boolean means the denial message can
    say *why*, which is the difference between an operator fixing their
    allowlist and an operator disabling the check.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unparseable"

    # Checked before the broader link-local test so the message names the
    # metadata service specifically.
    if address in CLOUD_METADATA_ADDRESSES:
        return "cloud_metadata"

    # Before the private/reserved tests: Python marks the documentation
    # ranges private, which is true but tells an operator nothing useful.
    if any(
        ip in net
        for net in (_DOCUMENTATION_V4 + _DOCUMENTATION_V6)
        if ip.version == net.version
    ):
        return "reserved"

    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private:
        return "private"
    if ip.version == 4 and ip in _CGNAT:
        return "carrier_grade_nat"
    if ip.is_reserved:
        return "reserved"
    return None


@dataclass(frozen=True)
class Target:
    """A URL that has passed validation, with what it resolved to."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class EgressPolicy:
    """Where HTTP tools may connect.

    Defaults are the restrictive end of every choice: no hosts, read-only
    methods, HTTPS only, no internal networks. A usable configuration is
    therefore always an explicit one.
    """

    allowed_hosts: tuple[str, ...] = ()
    allowed_methods: tuple[str, ...] = READ_METHODS

    # Development-only. Named for what it costs, not for what it enables.
    allow_http: bool = False

    # Internal-services deployments. Separate switches so that needing one
    # does not silently grant the others.
    allow_private_networks: bool = False
    allow_loopback: bool = False
    allow_link_local: bool = False
    # Deliberately has no configuration path from YAML. Reaching the metadata
    # service is not a use case this platform supports; the field exists only
    # so tests can prove the check is the thing doing the blocking.
    allow_cloud_metadata: bool = False

    max_redirects: int = 3
    max_request_bytes: int = 1 * 1024 * 1024
    max_response_bytes: int = 5 * 1024 * 1024

    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    write_timeout: float = 30.0
    total_timeout: float = 60.0

    # Headers a caller may set. An arbitrary header set is a way to smuggle
    # credentials outward or to confuse an upstream proxy.
    allowed_request_headers: tuple[str, ...] = (
        "accept",
        "accept-language",
        "content-type",
        "user-agent",
    )

    def __post_init__(self) -> None:
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if self.max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        for name in ("connect_timeout", "read_timeout", "write_timeout", "total_timeout"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        unknown = sorted(set(m.upper() for m in self.allowed_methods) - set(KNOWN_METHODS))
        if unknown:
            raise ValueError(
                f"unsupported HTTP method(s): {', '.join(unknown)}. "
                f"Supported: {', '.join(KNOWN_METHODS)}"
            )

    # -- checks ------------------------------------------------------------

    def check_method(self, method: str) -> str:
        """Return the permission a method requires, or deny it.

        The permission is derived from the method rather than fixed per tool,
        so a tool that can POST is visibly a different privilege from one that
        can only GET.
        """
        upper = (method or "").upper()
        if upper not in KNOWN_METHODS:
            raise PermissionDenied(
                f"HTTP method {upper or '(empty)'} is not supported",
                method=upper,
                supported=list(KNOWN_METHODS),
            )
        if upper not in tuple(m.upper() for m in self.allowed_methods):
            raise PermissionDenied(
                f"HTTP method {upper} is not permitted by this configuration",
                method=upper,
                allowed=list(self.allowed_methods),
            )
        return "network.read" if upper in READ_METHODS else "network.write"

    def check_request_size(self, size: int) -> None:
        if size > self.max_request_bytes:
            raise PermissionDenied(
                f"request body of {size} bytes exceeds the "
                f"{self.max_request_bytes} byte limit",
                size=size,
                limit=self.max_request_bytes,
            )

    def host_allowed(self, host: str) -> bool:
        """Exact match, or a dot-anchored suffix match.

        Anchoring on the dot is what stops ``notexample.com`` and
        ``example.com.evil.test`` from matching ``example.com``.
        """
        host = (host or "").lower().rstrip(".")
        for pattern in self.allowed_hosts:
            pattern = pattern.lower().rstrip(".")
            if host == pattern or host.endswith("." + pattern):
                return True
        return False

    def blocked_categories(self) -> set[str]:
        """Which address categories this policy refuses."""
        blocked: set[str] = {
            "unparseable",
            "unspecified",
            "multicast",
            "reserved",
            "carrier_grade_nat",
        }
        if not self.allow_loopback:
            blocked.add("loopback")
        if not self.allow_private_networks:
            blocked.add("private")
        if not self.allow_link_local:
            blocked.add("link_local")
        if not self.allow_cloud_metadata:
            blocked.add("cloud_metadata")
        return blocked

    def describe(self) -> dict[str, object]:
        """The posture, for logs and the readiness report."""
        return {
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_methods": list(self.allowed_methods),
            "https_only": not self.allow_http,
            "private_networks": self.allow_private_networks,
            "loopback": self.allow_loopback,
            "link_local": self.allow_link_local,
            "cloud_metadata": self.allow_cloud_metadata,
            "max_redirects": self.max_redirects,
            "max_response_bytes": self.max_response_bytes,
            # Reported as a known gap rather than omitted: an operator
            # reading their posture should see it.
            "dns_rebinding_toctou": "open — use an egress proxy",
        }


def validate_url(url: str, policy: EgressPolicy) -> Target:
    """Check a URL's shape, scheme, and host. Does not resolve DNS."""
    if not policy.allowed_hosts:
        raise PermissionDenied(
            "HTTP tools are enabled but no allowed hosts are configured, so "
            "no destination is permitted. Set tools.http.allowed_hosts to the "
            "specific hosts this deployment may reach.",
            remedy="configure tools.http.allowed_hosts",
        )

    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise PermissionDenied(
            f"scheme {parsed.scheme or '(none)'} is not supported; only http and https are",
            scheme=parsed.scheme,
        )
    if parsed.scheme == "http" and not policy.allow_http:
        raise PermissionDenied(
            "plain http is refused; use https, or enable allow_http in a "
            "development configuration",
            url_scheme="http",
            remedy="use https",
        )
    if parsed.username or parsed.password:
        raise PermissionDenied(
            "credentials embedded in a URL are refused; send them as headers",
        )

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise PermissionDenied(f"could not read a host from {url!r}")

    if host in CLOUD_METADATA_HOSTNAMES and not policy.allow_cloud_metadata:
        raise PermissionDenied(
            f"{host} is a cloud metadata hostname and is never a permitted destination",
            host=host,
            category="cloud_metadata",
        )

    # When the host is written as a literal IP, classify it here — before the
    # allowlist — so an operator who has put a metadata or internal address
    # into their allowlist is told what is wrong rather than seeing it fail as
    # a plain "not allowed". A hostname is left to resolve_and_validate, which
    # judges what it actually resolves to.
    if _is_ip_literal(host):
        literal = classify_address(host)
        if literal is not None and literal in policy.blocked_categories():
            raise PermissionDenied(
                f"{host} is a {literal.replace('_', ' ')} address and is not a "
                f"permitted destination",
                host=host,
                category=literal,
            )

    if not policy.host_allowed(host):
        raise PermissionDenied(
            f"host {host} is not in the allowed list",
            host=host,
            allowed=list(policy.allowed_hosts),
        )

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return Target(url=url, scheme=parsed.scheme, host=host, port=port)


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    """Every address a hostname resolves to. Patched in tests."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return tuple(seen)


def resolve_and_validate(url: str, policy: EgressPolicy) -> Target:
    """Validate the URL, then resolve it and validate every address.

    All addresses must pass. Accepting a name because one of its answers is
    public would let a rebinding attacker decide which answer the connection
    actually used.
    """
    target = validate_url(url, policy)

    try:
        addresses = _resolve_host(target.host, target.port)
    except OSError as exc:
        raise PermissionDenied(
            f"could not resolve {target.host}: {exc}",
            host=target.host,
        ) from exc

    if not addresses:
        raise PermissionDenied(f"{target.host} resolved to no addresses", host=target.host)

    blocked = policy.blocked_categories()
    for address in addresses:
        category = classify_address(address)
        if category in blocked:
            raise PermissionDenied(
                f"{target.host} resolves to {address}, which is a "
                f"{category.replace('_', ' ')} address and is not a permitted "
                f"destination",
                host=target.host,
                address=address,
                category=category,
            )

    return Target(
        url=target.url,
        scheme=target.scheme,
        host=target.host,
        port=target.port,
        addresses=addresses,
    )


def validate_redirect(current_url: str, location: str, policy: EgressPolicy) -> Target:
    """Validate a redirect target with the same rules as the original request.

    Relative locations are resolved against the current URL first, so a
    ``Location: /admin`` cannot be mistaken for a host-less denial.
    """
    absolute = urljoin(current_url, location)
    return resolve_and_validate(absolute, policy)


def filter_headers(
    headers: dict[str, object] | None, policy: EgressPolicy
) -> dict[str, str]:
    """Drop headers the policy does not allow a caller to set.

    Silently dropping is wrong here — a caller who set Authorization should
    know it did not go out — so this raises instead.
    """
    if not headers:
        return {}
    allowed = {h.lower() for h in policy.allowed_request_headers}
    out: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).lower()
        if name not in allowed:
            raise PermissionDenied(
                f"request header {key!r} is not permitted; allowed headers are "
                f"{', '.join(sorted(allowed))}",
                header=str(key),
            )
        out[str(key)] = str(value)
    return out
