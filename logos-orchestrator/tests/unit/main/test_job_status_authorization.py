"""GET /jobs/{job_id}: a job is readable only by the key that submitted it."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import logos as main
from logos.routers import user_facing

client = TestClient(main.app, raise_server_exceptions=False)

_JOB = {
    "api_key_id": 11,
    "team_id": 21,
    "status": "success",
    "result_payload": {"status_code": 200, "data": {"answer": "secret"}},
    "error_message": None,
}


def _key(api_key_id: int, team_id: int):
    return SimpleNamespace(key_value=f"key-{api_key_id}", api_key_id=api_key_id, team_id=team_id)


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setattr(user_facing.JobService, "fetch", staticmethod(lambda job_id: dict(_JOB)))


def _get_as(monkeypatch, auth):
    monkeypatch.setattr(user_facing, "authenticate_api_key", lambda headers, client_ip=None: auth)
    return client.get("/jobs/1", headers={"Authorization": f"Bearer {auth.key_value}"})


def test_submitting_key_reads_its_job(monkeypatch, job):
    response = _get_as(monkeypatch, _key(11, 21))
    assert response.status_code == 200
    assert response.json()["result"] == _JOB["result_payload"]


def test_other_key_of_same_team_is_rejected(monkeypatch, job):
    assert _get_as(monkeypatch, _key(12, 21)).status_code == 403


def test_key_of_a_logos_admin_gets_no_cross_team_access(monkeypatch, job):
    # The holder's role is never looked up: a key acts for its own team only.
    def fail(*args, **kwargs):
        raise AssertionError("job authorization must not depend on the key holder's role")

    monkeypatch.setattr(user_facing, "DBManager", fail)
    response = _get_as(monkeypatch, _key(99, 98))
    assert response.status_code == 403
    assert "secret" not in response.text
