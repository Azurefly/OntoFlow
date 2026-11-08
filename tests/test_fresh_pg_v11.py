"""Fresh install safety tests; do not claim they replace real PG integration."""
import os
from contextlib import contextmanager

import pytest

from app.main import create_app
from app.pg_store import PgStore
from scripts.init_fresh_env import create_env, generate_settings


def parse_config(config):
    return dict(line.split('=', 1) for line in config.splitlines()
                if line and not line.startswith('#'))


def test_env_generation_has_strong_secret_and_isolated_project():
    first=parse_config(generate_settings())
    second=parse_config(generate_settings())
    assert first['APP_ENV']=='production'
    assert first['ONTOFLOW_FRESH_ONLY']=='1'
    assert first['COMPOSE_PROJECT_NAME']=='ontoflow-fresh'
    assert first['PORT']=='8001'
    assert first['PG_PASSWORD']!=second['PG_PASSWORD']
    assert first['ONTOFLOW_DEMO_MODE']=='true'
    assert all(key not in first for key in ('API_TOKEN','MODEL_ADMIN_TOKEN','MODEL_SETTINGS_KEY'))
    assert all(c.isascii() and (c.isalnum() or c in '-_') for c in first['PG_PASSWORD'])


def test_env_does_not_overwrite_existing_file(tmp_path):
    path=tmp_path/'.env'
    create_env(path)
    old=path.read_text()
    assert not (path.stat().st_mode & 0o077)
    with pytest.raises(FileExistsError):create_env(path)
    assert path.read_text()==old
    with pytest.raises(ValueError):generate_settings('../bad',8001)
    with pytest.raises(ValueError):generate_settings('a',0)


def test_production_requires_postgres_even_when_api_token_set(monkeypatch,tmp_path):
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('API_TOKEN','valid-test-token')
    monkeypatch.delenv('DATABASE_URL',raising=False)
    with pytest.raises(RuntimeError, match='PostgreSQL DATABASE_URL'):
        create_app(tmp_path/'production')
    assert not (tmp_path/'production'/'ontology.sqlite3').exists()


class ReturnRow:
    def __init__(self, data=None):self.data=data
    def fetchone(self):return self.data


class SqlProbe:
    def __init__(self, projects=False, marker=False):
        self.projects=projects
        self.marker=marker
        self.calls=[]
    def execute(self,sql,params=()):
        self.calls.append((sql,params))
        if "to_regclass('public.projects')" in sql:return ReturnRow({'table_name':'projects' if self.projects else None})
        if "to_regclass('public.ontoflow_install')" in sql:return ReturnRow({'table_name':'ontoflow_install' if self.marker else None})
        if 'SELECT 1 AS ok FROM ontoflow_install' in sql:return ReturnRow({'ok':1} if self.marker else None)
        if 'SELECT pg_get_constraintdef' in sql:return ReturnRow({'definition':'PRIMARY KEY (project_id, id)'})
        if 'SELECT 1 AS ok FROM pg_constraint' in sql:return ReturnRow({'ok':1})
        return ReturnRow({'ok':1})


class PgProbe:
    def __init__(self,conn):self.conn=conn
    @contextmanager
    def tx(self):yield self.conn


def test_new_pg_initializes_its_marker_and_tables(monkeypatch):
    monkeypatch.setenv('ONTOFLOW_FRESH_ONLY','1')
    probe=SqlProbe()
    PgStore._initialize_schema(PgProbe(probe))
    sql=[x[0] for x in probe.calls]
    assert 'CREATE TABLE IF NOT EXISTS projects' in ' '.join(sql)
    assert any('INSERT INTO ontoflow_install' in x for x in sql)
    assert sum('INSERT INTO ontoflow_install' in x for x in sql)==1
    assert any('pg_advisory_xact_lock' in x for x in sql)


def test_fresh_install_refuses_old_pg_before_modification(monkeypatch):
    monkeypatch.setenv('ONTOFLOW_FRESH_ONLY','1')
    probe=SqlProbe(projects=True,marker=False)
    with pytest.raises(RuntimeError,match='拒绝接管旧库'):
        PgStore._initialize_schema(PgProbe(probe))
    assert not any(x[0].startswith('CREATE ') for x in probe.calls)
    assert not any('INSERT INTO' in x[0] for x in probe.calls)


def test_restart_with_valid_marker_keeps_install_uuid(monkeypatch):
    monkeypatch.setenv('ONTOFLOW_FRESH_ONLY','1')
    probe=SqlProbe(projects=True,marker=True)
    PgStore._initialize_schema(PgProbe(probe))
    assert not any('INSERT INTO ontoflow_install' in x[0] for x in probe.calls)
