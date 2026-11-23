"""Every key-scoped model query applies the managed agent identity's local scope."""

from unittest.mock import MagicMock

import pytest

from logos import DBManager
from logos.dbutils import dbmanager


def _executed_sql(call, monkeypatch):
    monkeypatch.setattr(dbmanager, "text", lambda sql: sql)  # keep the statement inspectable
    db = DBManager.__new__(DBManager)
    db.session = MagicMock()
    call(db)
    return str(db.session.execute.call_args.args[0])


@pytest.mark.parametrize(
    "call",
    [
        lambda db: db.get_deployments_for_api_key(17),
        lambda db: db.get_models_for_api_key(17),
        lambda db: db.get_model_for_api_key(17, "mixed"),
        lambda db: db.resolve_proxy_model(17, "mixed"),
    ],
    ids=["deployments", "model-list", "model-retrieval", "proxy-model"],
)
def test_query_grants_local_models_and_ignores_explicit_grants_for_agent(call, monkeypatch):
    sql = _executed_sql(call, monkeypatch)
    assert "ki.internal_service = 'agent' AND p.provider_type = 'logosnode'" in sql
    assert sql.count("ki.custom = true AND ki.internal_service IS NULL") == 2
