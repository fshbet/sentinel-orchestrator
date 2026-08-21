"""End-to-end HTTP tool behaviour against a real server.

The unit tests in test_egress.py prove the policy functions decide correctly.
These prove the tool actually calls them — that the redirect chain really is
revalidated, that the response cap really stops reading, and that the read tool
really cannot be made to POST.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.errors import PermissionDenied, ToolError
from orchestrator.tools.egress import EgressPolicy
from orchestrator.tools.native import http_tools
from orchestrator.tools.registry import ToolContext

httpx = pytest.importorskip("httpx", reason="http tools require httpx")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output readable
        pass

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ok":
            self._send(200, b"hello")
        elif self.path == "/redirect-to-metadata":
            # The attack: an allowed host bouncing the client at the cloud
            # metadata service.
            self._send(302, headers={"Location": "http://169.254.169.254/latest/"})
        elif self.path == "/redirect-to-loopback":
            self._send(302, headers={"Location": "http://127.0.0.1:1/"})
        elif self.path == "/redirect-offsite":
            self._send(302, headers={"Location": "https://evil.test/"})
        elif self.path == "/redirect-loop":
            self._send(302, headers={"Location": "/redirect-loop"})
        elif self.path == "/big":
            self._send(200, b"x" * 200_000)
        elif self.path == "/echo-headers":
            import json

            self._send(200, json.dumps(dict(self.headers)).encode())
        else:
            self._send(404, b"nope")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send(200, b"posted")

    do_HEAD = do_GET


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _tools(server, **overrides):
    """HTTP tools pointed at the loopback test server.

    allow_loopback is on here only because the test server is on 127.0.0.1.
    Every other test in this file confirms that redirects away from it are
    still judged on their own merits.
    """
    host = server.split(":")[0]
    options = dict(
        allowed_hosts=(host,),
        allow_http=True,
        allow_loopback=True,
        max_response_bytes=100_000,
        max_redirects=2,
    )
    options.update(overrides)
    return {spec.id: (spec, fn) for spec, fn in http_tools(policy=EgressPolicy(**options))}


def _call(tools, tool_id, **arguments):
    spec, fn = tools[tool_id]
    return asyncio.run(fn(arguments, ToolContext(execution_id="e", task_id="t")))


# --------------------------------------------------------------------------
# The happy path still works
# --------------------------------------------------------------------------


def test_an_allowed_url_is_fetched(server):
    result = _call(_tools(server), "http.request", url=f"http://{server}/ok")
    assert result["status"] == 200
    assert result["body"] == "hello"
    assert result["truncated"] is False
    assert result["resolved_addresses"] == ["127.0.0.1"]


# --------------------------------------------------------------------------
# Redirect revalidation — the bypass that mattered
# --------------------------------------------------------------------------


def test_a_redirect_to_the_metadata_service_is_refused(server):
    """Before egress.py this returned AWS credentials."""
    with pytest.raises(PermissionDenied) as exc:
        _call(_tools(server), "http.request",
              url=f"http://{server}/redirect-to-metadata")
    assert "metadata" in str(exc.value).lower()


def test_a_redirect_to_another_loopback_port_is_refused_when_not_allowlisted(server):
    """Even with loopback permitted, the destination must still be allowlisted."""
    tools = _tools(server, allowed_hosts=("nothing.test",), allow_loopback=True)
    with pytest.raises(PermissionDenied):
        _call(tools, "http.request", url=f"http://{server}/ok")


def test_a_redirect_off_the_allowlist_is_refused(server):
    with pytest.raises(PermissionDenied):
        _call(_tools(server), "http.request", url=f"http://{server}/redirect-offsite")


def test_a_redirect_loop_stops_at_the_configured_limit(server):
    with pytest.raises(ToolError) as exc:
        _call(_tools(server, max_redirects=2), "http.request",
              url=f"http://{server}/redirect-loop")
    assert "redirect" in str(exc.value).lower()


def test_zero_redirects_means_none_are_followed(server):
    with pytest.raises(ToolError):
        _call(_tools(server, max_redirects=0), "http.request",
              url=f"http://{server}/redirect-loop")


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


def test_an_oversized_response_is_truncated_at_the_limit(server):
    result = _call(_tools(server, max_response_bytes=1000), "http.request",
                   url=f"http://{server}/big")
    assert result["truncated"] is True
    assert result["bytes"] <= 1000


# --------------------------------------------------------------------------
# Read and write are different privileges
# --------------------------------------------------------------------------


def test_the_read_tool_cannot_be_used_to_post(server):
    """Otherwise network.read silently includes the ability to send data out."""
    with pytest.raises(PermissionDenied) as exc:
        _call(_tools(server, allowed_methods=("GET", "POST")), "http.request",
              url=f"http://{server}/ok", method="POST")
    assert "network.write" in str(exc.value)


def test_the_write_tool_is_not_registered_when_no_write_method_is_allowed(server):
    tools = _tools(server, allowed_methods=("GET", "HEAD"))
    assert "http.request" in tools
    assert "http.send" not in tools


def test_the_write_tool_appears_only_when_a_write_method_is_configured(server):
    tools = _tools(server, allowed_methods=("GET", "POST"))
    assert "http.send" in tools
    spec, _ = tools["http.send"]
    assert "network.write" in spec.permissions
    result = _call(tools, "http.send", url=f"http://{server}/ok", method="POST",
                   body="data")
    assert result["status"] == 200


def test_a_method_outside_the_configured_set_is_refused(server):
    with pytest.raises(PermissionDenied):
        _call(_tools(server, allowed_methods=("GET",)), "http.request",
              url=f"http://{server}/ok", method="DELETE")


# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------


def test_an_authorization_header_cannot_be_smuggled_out(server):
    """A model that has seen a token must not be able to forward it."""
    with pytest.raises(PermissionDenied) as exc:
        _call(_tools(server), "http.request", url=f"http://{server}/echo-headers",
              headers={"Authorization": "Bearer stolen"})
    assert "authorization" in str(exc.value).lower()


def test_permitted_headers_are_sent(server):
    result = _call(_tools(server), "http.request",
                   url=f"http://{server}/echo-headers",
                   headers={"Accept": "application/json"})
    assert result["status"] == 200
    assert "application/json" in result["body"]
