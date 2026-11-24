"""Permission queries against disposable PostgreSQL, including mixed providers.

Set AGENT_TEST_DB_URL to run; uses its own temporary schema. Load real
SQLAlchemy/psycopg2 before pytest (tests/conftest.py otherwise installs stubs):
Import sqlalchemy.orm, sqlalchemy.ext.declarative and psycopg2.extras, then
invoke pytest.main(["tests/integration/test_agent_identity.py"]).

"""

import os
import uuid
from pathlib import Path
from xml.etree import ElementTree

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from logos.dbutils.dbmanager import DBManager

pytestmark = pytest.mark.integration


@pytest.fixture
def database():
    url = os.getenv("AGENT_TEST_DB_URL")
    if not url:
        pytest.skip("Set AGENT_TEST_DB_URL to a disposable PostgreSQL database")
    schema = "agent_identity_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE users (id int PRIMARY KEY, role text);
                CREATE TABLE teams (id int PRIMARY KEY, priority int, keycloak_group text);
                CREATE TABLE team_members (source text);
                CREATE TABLE api_keys (
                    id serial PRIMARY KEY, key_value text UNIQUE, name text,
                    key_type text DEFAULT 'application', environment text, log text DEFAULT 'BILLING',
                    settings jsonb, team_id int, user_id int, default_priority int DEFAULT 1,
                    is_active boolean DEFAULT true, use_custom_permissions boolean DEFAULT true);
                CREATE TABLE providers (id int PRIMARY KEY, name text, provider_type text,
                    privacy_level text, cloud_provider_type text, base_url text,
                    auth_name text, auth_format text, api_key text);
                CREATE TABLE models (id int PRIMARY KEY, name text, description text);
                CREATE TABLE model_provider (provider_id int, model_id int, endpoint text, api_key text);
                CREATE TABLE model_aliases (model_id int, alias text);
                CREATE TABLE api_key_provider_permissions (api_key_id int, provider_id int);
                CREATE TABLE api_key_model_permissions (api_key_id int, model_id int);
                CREATE TABLE team_provider_permissions (team_id int, provider_id int);
                CREATE TABLE team_model_permissions (team_id int, model_id int);
                INSERT INTO providers (id, name, provider_type) VALUES (1, 'local', 'logosnode'), (2, 'cloud', 'cloud');
                INSERT INTO models VALUES (10, 'mixed', NULL), (20, 'cloud-only', NULL);
                INSERT INTO model_provider VALUES (1, 10), (2, 10), (2, 20);
                INSERT INTO model_aliases VALUES (10, 'alias');
            """))
            migration = (
                Path(__file__).resolve().parents[3]
                / "logos-webservice/src/main/resources/liquibase/changelog/086_agent_service_identity.xml"
            )
            for sql in ElementTree.parse(migration).findall("{*}changeSet/{*}sql"):
                conn.execute(text(sql.text))
            privacy = migration.with_name("095_team_privacy.xml")
            for change in ElementTree.parse(privacy).findall("{*}changeSet"):
                if "contextFilter" not in change.attrib:
                    conn.execute(text(change.find("{*}sql").text))
        db = DBManager.__new__(DBManager)
        db.session = Session(engine)
        try:
            yield db
        finally:
            db.session.close()
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def test_agent_can_only_reach_local_copy_even_with_explicit_cloud_grants(database):
    db = database
    kid = db.get_agent_api_key_id()
    db.session.execute(text("INSERT INTO api_key_provider_permissions VALUES (:kid, 2)"), {"kid": kid})
    db.session.execute(text("INSERT INTO api_key_model_permissions VALUES (:kid, 20)"), {"kid": kid})
    db.session.commit()
    deployments = db.get_deployments_for_api_key(kid)
    assert [(d["provider_id"], d["model_id"]) for d in deployments] == [(1, 10)]
    assert deployments[0]["aliases"] == "alias"
    row = db.get_api_key_by_id(kid)
    assert db.get_api_key_by_value(row["key_value"]) is None
    db.session.execute(text("UPDATE api_keys SET is_active = false WHERE id = :kid"), {"kid": kid})
    db.session.commit()
    assert db.get_agent_api_key_id() is None
    assert db.get_api_key_by_id(kid) is None
    assert db.get_deployments_for_api_key(kid) == []


def test_agent_discovers_exactly_its_local_models_without_explicit_grants(database):
    db = database
    kid = db.get_agent_api_key_id()
    assert [(m["id"], m["name"], m["aliases"]) for m in db.get_models_for_api_key(kid)] == [(10, "mixed", ["alias"])]
    assert db.get_model_for_api_key(kid, "mixed")["id"] == 10
    assert db.get_model_for_api_key(kid, "cloud-only") is None
    assert db.resolve_proxy_model(kid, "alias") == (10, "mixed")
    assert db.resolve_proxy_model(kid, "cloud-only") is None
    assert db.get_auth_info_to_deployment(10, 1, kid)["provider_id"] == 1
    assert db.get_auth_info_to_deployment(10, 2, kid) is None


def test_normal_key_keeps_its_explicit_cloud_permissions(database):
    db = database
    kid = db.session.execute(
        text("INSERT INTO api_keys (key_value, name) VALUES ('normal', 'ordinary') RETURNING id")
    ).scalar_one()
    db.session.execute(text("INSERT INTO api_key_provider_permissions VALUES (:kid, 2)"), {"kid": kid})
    db.session.execute(text("INSERT INTO api_key_model_permissions VALUES (:kid, 20)"), {"kid": kid})
    db.session.commit()
    assert [(d["provider_id"], d["model_id"]) for d in db.get_deployments_for_api_key(kid)] == [(2, 20)]
    assert db.get_api_key_by_value("normal")["id"] == kid


@pytest.mark.parametrize("scope", ["LOCAL", "EU"])
def test_tightening_team_scope_preserves_historical_batch_settlement(database, scope):
    db = database
    db.session.execute(text("""
        CREATE TABLE batch_objects (
            id int PRIMARY KEY, upstream_id text, provider_id int, api_key_id int, team_id int,
            user_id int, status text, log_level text, kind text, execution text, settled_at timestamptz,
            updated_at timestamptz, settlement_lease_expires_at timestamptz);
        INSERT INTO teams (id, privacy_scope) VALUES (7, 'WORLDWIDE');
        INSERT INTO batch_objects (id, upstream_id, provider_id, api_key_id, team_id, status, kind, execution)
            VALUES (1, 'batch-1', 2, 11, 7, 'completed', 'batch', 'provider');
    """))
    db.session.execute(text("UPDATE teams SET privacy_scope = :scope WHERE id = 7"), {"scope": scope})
    [pending] = db.get_unsettled_batches()
    assert (pending["upstream_id"], pending["provider_id"], pending["api_key_id"], pending["team_id"]) == (
        "batch-1",
        2,
        11,
        7,
    )
    assert db.claim_batch_for_settlement(pending["id"])
    db.mark_batch_settled(pending["id"])
    assert db.get_unsettled_batches() == []


@pytest.mark.parametrize("custom", [False, True])
def test_team_scope_tightens_existing_keys_and_retains_the_local_alias(database, custom):
    db = database
    db.session.execute(text("INSERT INTO teams (id) VALUES (7)"))
    kid = db.session.execute(
        text(
            "INSERT INTO api_keys (key_value, team_id, use_custom_permissions) VALUES ('team', 7, :custom) RETURNING id"
        ),
        {"custom": custom},
    ).scalar_one()
    for table, owner in [("team", 7), ("api_key", kid)]:
        db.session.execute(
            text(f"INSERT INTO {table}_provider_permissions VALUES (:owner, 1), (:owner, 2)"), {"owner": owner}
        )
        db.session.execute(
            text(f"INSERT INTO {table}_model_permissions VALUES (:owner, 10), (:owner, 20)"), {"owner": owner}
        )
    db.session.execute(text("UPDATE providers SET privacy_level = 'LOCAL' WHERE id = 1"))
    db.session.execute(
        text(
            "UPDATE providers SET privacy_level = 'CLOUD_IN_EU_BY_US_PROVIDER', "
            "base_url = 'https://example.eu' WHERE id = 2"
        )
    )

    def hosts():
        return {d["provider_id"] for d in db.get_deployments_for_api_key(kid)}

    assert hosts() == {1, 2}
    db.session.execute(text("UPDATE teams SET privacy_scope = 'EU' WHERE id = 7"))
    assert hosts() == {1}
    db.session.execute(text("UPDATE providers SET eu_processing_verified = true WHERE id = 2"))
    assert hosts() == {1, 2}
    db.session.execute(
        text("UPDATE model_provider SET endpoint = 'https://external.invalid/chat' WHERE provider_id = 2")
    )
    assert hosts() == {1}
    db.session.execute(text("UPDATE model_provider SET endpoint = '/v1/chat/completions' WHERE provider_id = 2"))
    assert hosts() == {1, 2}
    db.session.execute(text("UPDATE teams SET privacy_scope = 'LOCAL' WHERE id = 7"))
    assert hosts() == {1}
    assert db.resolve_proxy_model(kid, "alias") == (10, "mixed")
    assert db.resolve_proxy_model(kid, "cloud-only") is None
    assert db.get_auth_info_to_deployment(10, 1, kid)["provider_id"] == 1
    assert db.get_auth_info_to_deployment(10, 2, kid) is None
