"""Deleting a run, through the HTTP surface a console actually uses.

Deletion destroys the audit trail, which is a security control rather than a
convenience, so the interesting cases are the ones where it must refuse.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from orchestrator.api.app import create_app  # noqa: E402
from orchestrator.api.identity import ADMIN, EXECUTIONS_WRITE, required_scope  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "profile: development\n"
        f"workspace: {tmp_path.as_posix()}\n"
        "storage:\n"
        "  backend: sqlite\n"
        f"  path: {(tmp_path / 'state.db').as_posix()}\n"
        "models:\n"
        "  providers: []\n",
        encoding="utf-8",
    )
    with TestClient(create_app(config_path=str(config))) as c:
        yield c


def _create(client, *, run=False):
    response = client.post(
        "/v1/executions",
        json={"objective": "a run that exists only to be deleted", "run": run},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_deleting_a_run_that_never_started_is_refused(client):
    """`created` is not terminal.

    Deleting the record of something that has not finished removes the only
    place its progress and approvals are written down. Cancel first.
    """
    created = _create(client)
    response = client.delete(f"/v1/executions/{created['id']}")

    assert response.status_code == 400
    assert "cancel" in response.json()["error"]["message"].lower()
    # And it is still there.
    assert client.get(f"/v1/executions/{created['id']}").status_code == 200


def test_a_cancelled_run_can_be_deleted_and_stays_deleted(client):
    created = _create(client)
    assert client.post(f"/v1/executions/{created['id']}/cancel").status_code == 200

    response = client.delete(f"/v1/executions/{created['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": created["id"]}

    assert client.get(f"/v1/executions/{created['id']}").status_code == 404
    ids = [r["id"] for r in client.get("/v1/executions").json()["executions"]]
    assert created["id"] not in ids


def test_deleting_something_that_is_not_there_is_a_404_not_a_success(client):
    assert client.delete("/v1/executions/no-such-run").status_code == 404


def test_the_audit_trail_goes_with_it(client):
    created = _create(client)
    client.post(f"/v1/executions/{created['id']}/cancel")
    assert client.get(f"/v1/executions/{created['id']}/audit").status_code == 200

    client.delete(f"/v1/executions/{created['id']}")
    assert client.get(f"/v1/executions/{created['id']}/audit").status_code == 404


def test_deletion_requires_admin_not_merely_write():
    """More destructive than cancelling, so not covered by executions.write.

    A token that may start and stop its own work should not also be able to
    erase the record that it did.
    """
    assert required_scope("DELETE", "/v1/executions/abc") == ADMIN
    assert required_scope("POST", "/v1/executions") == EXECUTIONS_WRITE


def test_settings_require_admin():
    """They name absolute paths, the backend, and every configured provider."""
    assert required_scope("GET", "/v1/settings") == ADMIN
    assert required_scope("PUT", "/v1/settings/profile") == ADMIN
    assert required_scope("PUT", "/v1/settings/keys/X_API_KEY") == ADMIN
