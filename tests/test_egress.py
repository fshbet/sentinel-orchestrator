"""Egress and SSRF controls.

Written against the vulnerabilities that existed before ``egress.py``: an
empty allowlist meant allow-everything, redirects were followed without
revalidation, and nothing stopped a request to 169.254.169.254.

Each test here names the attack it prevents rather than the function it calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.errors import PermissionDenied
from orchestrator.tools.egress import (
    CLOUD_METADATA_ADDRESSES,
    EgressPolicy,
    classify_address,
    resolve_and_validate,
    validate_url,
)


def _policy(**kwargs) -> EgressPolicy:
    base = dict(allowed_hosts=("example.com",), allow_http=True)
    base.update(kwargs)
    return EgressPolicy(**base)


# --------------------------------------------------------------------------
# The allowlist is mandatory
# --------------------------------------------------------------------------


def test_an_empty_allowlist_denies_everything_rather_than_permitting_it():
    """The original bug: `if allowed_hosts:` made an empty list mean allow-all.

    An operator who enables HTTP tools without naming hosts has configured
    nothing, and "nothing configured" must fail closed.
    """
    policy = EgressPolicy(allowed_hosts=())
    with pytest.raises(PermissionDenied) as exc:
        validate_url("https://example.com/", policy)
    assert "no allowed hosts" in str(exc.value).lower()


def test_a_host_outside_the_allowlist_is_denied():
    with pytest.raises(PermissionDenied):
        validate_url("https://evil.test/", _policy())


def test_subdomains_are_allowed_only_when_the_parent_is_listed():
    policy = _policy(allowed_hosts=("example.com",))
    assert validate_url("https://api.example.com/", policy).host == "api.example.com"

    # A suffix match must not be a substring match: notexample.com is not a
    # subdomain of example.com.
    with pytest.raises(PermissionDenied):
        validate_url("https://notexample.com/", policy)
    with pytest.raises(PermissionDenied):
        validate_url("https://example.com.evil.test/", policy)


# --------------------------------------------------------------------------
# Scheme
# --------------------------------------------------------------------------


def test_plain_http_is_refused_unless_explicitly_enabled():
    strict = EgressPolicy(allowed_hosts=("example.com",))
    with pytest.raises(PermissionDenied) as exc:
        validate_url("http://example.com/", strict)
    assert "https" in str(exc.value).lower()

    assert validate_url("http://example.com/", _policy()).scheme == "http"


def test_non_http_schemes_are_refused():
    for url in (
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "data:text/plain,hello",
    ):
        with pytest.raises(PermissionDenied):
            validate_url(url, _policy())


# --------------------------------------------------------------------------
# Address classification — the SSRF core
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        ("127.0.0.1", "loopback"),
        ("127.0.0.53", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.5", "private"),
        ("172.16.0.1", "private"),
        ("192.168.1.1", "private"),
        ("fd00::1", "private"),
        ("169.254.169.254", "cloud_metadata"),
        ("169.254.0.1", "link_local"),
        ("fe80::1", "link_local"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("100.64.0.1", "carrier_grade_nat"),
        ("192.0.2.1", "reserved"),
        ("8.8.8.8", None),
        ("1.1.1.1", None),
    ],
)
def test_addresses_are_classified_correctly(address, expected):
    assert classify_address(address) == expected


def test_every_known_cloud_metadata_address_is_classified_as_such():
    """These are the addresses that turn an SSRF into stolen credentials."""
    for address in CLOUD_METADATA_ADDRESSES:
        assert classify_address(address) == "cloud_metadata", address


def test_the_aws_metadata_endpoint_is_refused_even_when_its_host_is_allowed():
    """An allowlist entry must never be a way to reach the metadata service."""
    policy = EgressPolicy(allowed_hosts=("169.254.169.254",), allow_http=True)
    with pytest.raises(PermissionDenied) as exc:
        resolve_and_validate("http://169.254.169.254/latest/meta-data/", policy)
    assert "metadata" in str(exc.value).lower()


def test_loopback_and_private_addresses_are_refused_by_default():
    policy = EgressPolicy(allowed_hosts=("localhost", "10.0.0.5"), allow_http=True)
    for url in ("http://localhost/", "http://10.0.0.5/"):
        with pytest.raises(PermissionDenied):
            resolve_and_validate(url, policy)


def test_internal_targets_can_be_reached_only_by_explicit_opt_in():
    """An internal-services deployment is legitimate; it must be deliberate."""
    policy = EgressPolicy(
        allowed_hosts=("10.0.0.5",), allow_http=True, allow_private_networks=True
    )
    target = resolve_and_validate("http://10.0.0.5/health", policy)
    assert target.addresses == ("10.0.0.5",)

    # Opting into private ranges must NOT also open the metadata service.
    metadata = EgressPolicy(
        allowed_hosts=("169.254.169.254",),
        allow_http=True,
        allow_private_networks=True,
        allow_link_local=True,
    )
    with pytest.raises(PermissionDenied):
        resolve_and_validate("http://169.254.169.254/", metadata)


# --------------------------------------------------------------------------
# DNS rebinding
# --------------------------------------------------------------------------


def test_a_host_resolving_to_any_blocked_address_is_refused(monkeypatch):
    """DNS rebinding: one public answer alongside one internal answer.

    Validating only the first result would let the attacker pick which one the
    connection actually used. Every returned address must pass.
    """
    from orchestrator.tools import egress

    monkeypatch.setattr(
        egress, "_resolve_host", lambda host, port: ("93.184.216.34", "127.0.0.1")
    )
    policy = _policy()
    with pytest.raises(PermissionDenied) as exc:
        resolve_and_validate("http://example.com/", policy)
    assert "127.0.0.1" in str(exc.value)


def test_a_host_resolving_only_to_public_addresses_is_allowed(monkeypatch):
    from orchestrator.tools import egress

    monkeypatch.setattr(
        egress, "_resolve_host", lambda host, port: ("93.184.216.34", "93.184.216.35")
    )
    target = resolve_and_validate("http://example.com/", _policy())
    assert target.addresses == ("93.184.216.34", "93.184.216.35")


def test_a_host_that_does_not_resolve_is_refused_not_attempted(monkeypatch):
    from orchestrator.tools import egress

    def boom(host, port):
        raise OSError("nodename nor servname provided")

    monkeypatch.setattr(egress, "_resolve_host", boom)
    with pytest.raises(PermissionDenied) as exc:
        resolve_and_validate("http://example.com/", _policy())
    assert "resolve" in str(exc.value).lower()


# --------------------------------------------------------------------------
# Methods, split read/write
# --------------------------------------------------------------------------


def test_only_configured_methods_are_permitted():
    policy = _policy(allowed_methods=("GET", "HEAD"))
    assert policy.check_method("GET") == "network.read"
    with pytest.raises(PermissionDenied) as exc:
        policy.check_method("POST")
    assert "not permitted" in str(exc.value).lower()


def test_reads_and_writes_require_different_permissions():
    """A tool that can POST is not a tool that can only fetch."""
    policy = _policy(
        allowed_methods=("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
    )
    for method in ("GET", "HEAD", "OPTIONS"):
        assert policy.check_method(method) == "network.read"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert policy.check_method(method) == "network.write"


def test_an_unsupported_method_is_rejected_when_the_policy_is_built():
    """Config-time, not call-time: a typo in a config should not wait for traffic."""
    with pytest.raises(ValueError, match="TRACE"):
        _policy(allowed_methods=("GET", "TRACE"))


def test_a_method_outside_the_known_set_is_never_treated_as_a_read():
    policy = _policy(allowed_methods=("GET",))
    with pytest.raises(PermissionDenied):
        policy.check_method("TRACE")
    with pytest.raises(PermissionDenied):
        policy.check_method("")


def test_the_default_method_set_is_read_only():
    """Enabling HTTP tools must not silently grant the ability to write."""
    assert set(EgressPolicy(allowed_hosts=("x.test",)).allowed_methods) == {
        "GET",
        "HEAD",
        "OPTIONS",
    }


# --------------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------------


def test_a_redirect_to_an_internal_address_is_refused(monkeypatch):
    """The original bug: follow_redirects=True validated only the first URL.

    An allowed host answering 302 -> 169.254.169.254 was a complete bypass.
    """
    from orchestrator.tools import egress

    monkeypatch.setattr(egress, "_resolve_host", lambda host, port: ("93.184.216.34",))
    policy = _policy()
    with pytest.raises(PermissionDenied) as exc:
        egress.validate_redirect(
            "https://example.com/start", "http://169.254.169.254/latest/", policy
        )
    assert "metadata" in str(exc.value).lower()


def test_a_redirect_off_the_allowlist_is_refused(monkeypatch):
    from orchestrator.tools import egress

    monkeypatch.setattr(egress, "_resolve_host", lambda host, port: ("93.184.216.34",))
    with pytest.raises(PermissionDenied):
        egress.validate_redirect("https://example.com/a", "https://evil.test/b", _policy())


def test_a_redirect_within_the_allowlist_is_permitted(monkeypatch):
    from orchestrator.tools import egress

    monkeypatch.setattr(egress, "_resolve_host", lambda host, port: ("93.184.216.34",))
    target = egress.validate_redirect(
        "https://example.com/a", "https://api.example.com/b", _policy()
    )
    assert target.host == "api.example.com"


def test_a_relative_redirect_resolves_against_the_current_url(monkeypatch):
    from orchestrator.tools import egress

    monkeypatch.setattr(egress, "_resolve_host", lambda host, port: ("93.184.216.34",))
    target = egress.validate_redirect("https://example.com/a/b", "/c", _policy())
    assert target.url == "https://example.com/c"


def test_a_redirect_from_https_down_to_http_is_refused_under_a_strict_policy(monkeypatch):
    from orchestrator.tools import egress

    monkeypatch.setattr(egress, "_resolve_host", lambda host, port: ("93.184.216.34",))
    strict = EgressPolicy(allowed_hosts=("example.com",))  # allow_http False
    with pytest.raises(PermissionDenied):
        egress.validate_redirect("https://example.com/a", "http://example.com/b", strict)


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


def test_limits_have_finite_defaults():
    policy = EgressPolicy(allowed_hosts=("x.test",))
    assert 0 < policy.max_redirects <= 10
    assert 0 < policy.max_request_bytes <= 16 * 1024 * 1024
    assert 0 < policy.max_response_bytes <= 64 * 1024 * 1024
    assert 0 < policy.connect_timeout <= 60
    assert 0 < policy.read_timeout <= 300


def test_an_oversized_request_body_is_refused_before_it_is_sent():
    policy = _policy(max_request_bytes=100)
    with pytest.raises(PermissionDenied) as exc:
        policy.check_request_size(5000)
    assert "request body" in str(exc.value).lower()


def test_a_policy_cannot_be_configured_with_nonsensical_limits():
    with pytest.raises(ValueError):
        EgressPolicy(allowed_hosts=("x.test",), max_redirects=-1)
    with pytest.raises(ValueError):
        EgressPolicy(allowed_hosts=("x.test",), max_response_bytes=0)


# --------------------------------------------------------------------------
# Credential and header safety
# --------------------------------------------------------------------------


def test_credentials_embedded_in_a_url_are_refused():
    """http://user:pass@host smuggles credentials and confuses host parsing."""
    with pytest.raises(PermissionDenied):
        validate_url("https://user:secret@example.com/", _policy())


def test_a_url_with_no_host_is_refused():
    for url in ("https:///path", "not-a-url", ""):
        with pytest.raises(PermissionDenied):
            validate_url(url, _policy())
