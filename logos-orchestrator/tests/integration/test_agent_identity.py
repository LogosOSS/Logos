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
                CREATE TABLE teams (id int PRIMARY KEY, priority int);
                CREATE TABLE api_keys (
                    id serial PRIMARY KEY, key_value text UNIQUE, name text,
                    key_type text DEFAULT 'application', environment text, log text DEFAULT 'BILLING',
                    settings jsonb, team_id int, user_id int, default_priority int DEFAULT 1,
                    is_active boolean DEFAULT true, use_custom_permissions boolean DEFAULT true);
                CREATE TABLE providers (id int PRIMARY KEY, name text, provider_type text,
                    privacy_level text, cloud_provider_type text, base_url text);
                CREATE TABLE models (id int PRIMARY KEY, name text);
                CREATE TABLE model_provider (provider_id int, model_id int);
                CREATE TABLE model_aliases (model_id int, alias text);
                CREATE TABLE api_key_provider_permissions (api_key_id int, provider_id int);
                CREATE TABLE api_key_model_permissions (api_key_id int, model_id int);
                CREATE TABLE team_provider_permissions (team_id int, provider_id int);
                CREATE TABLE team_model_permissions (team_id int, model_id int);
                INSERT INTO providers (id, name, provider_type) VALUES (1, 'local', 'logosnode'), (2, 'cloud', 'cloud');
                INSERT INTO models VALUES (10, 'mixed'), (20, 'cloud-only');
                INSERT INTO model_provider VALUES (1, 10), (2, 10), (2, 20);
                INSERT INTO model_aliases VALUES (10, 'alias');
            """))
            migration = (
                Path(__file__).resolve().parents[3]
                / "logos-webservice/src/main/resources/liquibase/changelog/086_agent_service_identity.xml"
            )
            for sql in ElementTree.parse(migration).findall("{*}changeSet/{*}sql"):
                conn.execute(text(sql.text))
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
