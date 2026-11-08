"""Optional REAL PostgreSQL integration gate. Never falls back to SQLite.

Set ONTOFLOW_TEST_PG_ADMIN_DSN to a TEST server with CREATEDB privilege.
A uniquely named temporary database is created and dropped; no business DB is
reused or truncated. These tests are SKIPPED, not passed, when PG is unavailable.
"""
import os
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.store import now,uid
from app.version import SCHEMA_VERSION
from app.task_leases import Lease,LostLease,lease_scope,claim_job,heartbeat,timestamp
from app.sync_engine import SyncSession,SyncBusy

@pytest.fixture
def pg(tmp_path,monkeypatch):
    admin_dsn=os.getenv('ONTOFLOW_TEST_PG_ADMIN_DSN')
    if not admin_dsn:
        pytest.skip('Real PostgreSQL not configured: set ONTOFLOW_TEST_PG_ADMIN_DSN for isolated tests')
    psycopg=pytest.importorskip('psycopg',reason='Real PG tests require psycopg[binary,pool]')
    pytest.importorskip('psycopg_pool')
    from psycopg import sql
    from urllib.parse import urlsplit,urlunsplit
    from app.pg_store import PgStore
    name='ontoflow_audit_'+uuid4().hex
    admin=psycopg.connect(admin_dsn,autocommit=True)
    db=None;created=False
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)));created=True
        uri=urlsplit(admin_dsn)
        if uri.scheme not in ('postgresql','postgres'):raise ValueError('Test DSN must use a postgresql:// URL')
        monkeypatch.setenv('DATABASE_URL',urlunsplit(uri._replace(path='/'+name)))
        monkeypatch.setenv('ONTOFLOW_FRESH_ONLY','1')
        monkeypatch.setenv('PG_POOL_SIZE','10')
        db=PgStore(tmp_path/'pgdata')
        assert db.is_postgres
        pid=uid()
        with db.tx() as c:c.execute('INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)',
                                    (pid,'real-pg-audit',now(),now()))
        yield db,pid
    finally:
        if db:db.close()
        if created:
            admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()',(name,))
            admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        admin.close()


def enqueue(db,pid,kind='summary',source='source'):
    jid=uid()
    with db.tx() as c:c.execute('''INSERT INTO workflow_jobs(id,project_id,job_kind,stage,state,phase,
        source_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (jid,pid,kind,'','queued','queued',source,now(),now()))
    return jid


def test_real_pg_additive_schema_and_distinct_document_parse_keys(pg):
    db,pid=pg
    assert db.one('SELECT schema_version FROM ontoflow_install WHERE id=1')['schema_version']==SCHEMA_VERSION
    one=enqueue(db,pid,'parse','document-a');two=enqueue(db,pid,'parse','document-b')
    assert one!=two
    import psycopg
    with pytest.raises(psycopg.errors.UniqueViolation):enqueue(db,pid,'parse','document-a')
    # The failed transaction must not prevent the next connection from working.
    assert db.one('SELECT COUNT(*) n FROM workflow_jobs WHERE project_id=?',(pid,))['n']==2


def test_real_pg_multi_consumer_claim_once(pg):
    db,pid=pg;jid=enqueue(db,pid)
    with ThreadPoolExecutor(max_workers=6) as pool:
        result=list(pool.map(lambda n:claim_job(db,f'worker-{n}',jid),range(6)))
    assert len([r for r in result if r is not None])==1
    assert db.one('SELECT lease_generation FROM workflow_jobs WHERE id=?',(jid,))['lease_generation']==1


def test_real_pg_fencing_after_reclaim_and_heartbeat(pg):
    db,pid=pg;jid=enqueue(db,pid)
    a=claim_job(db,'a',jid)
    with db.tx() as c:c.execute('UPDATE workflow_jobs SET lease_until=? WHERE id=?',(timestamp(-5),jid))
    b=claim_job(db,'b',jid)
    assert b['lease_generation']==a['lease_generation']+1
    old=Lease(jid,'a',a['lease_generation'])
    assert not heartbeat(db,old)
    with lease_scope(old),pytest.raises(LostLease):
        with db.tx() as c:c.execute('UPDATE projects SET name=? WHERE id=?',('BAD',pid))
    assert db.project(pid)['name']=='real-pg-audit'
    fresh=Lease(jid,'b',b['lease_generation'])
    assert heartbeat(db,fresh)
    with lease_scope(fresh),db.tx() as c:c.execute('UPDATE projects SET name=? WHERE id=?',('valid-owner',pid))
    assert db.project(pid)['name']=='valid-owner'


def test_real_pg_commit_after_expiry_rolls_back(pg):
    db,pid=pg;jid=enqueue(db,pid);job=claim_job(db,'w',jid)
    lease=Lease(jid,'w',job['lease_generation'])
    with lease_scope(lease),pytest.raises(LostLease):
        with db.tx() as c:
            c.execute('UPDATE projects SET name=? WHERE id=?',('BAD',pid))
            c.execute('UPDATE workflow_jobs SET lease_until=? WHERE id=?',(timestamp(-1),jid))
    assert db.project(pid)['name']=='real-pg-audit'


def test_real_pg_sync_lock_serializes_independent_clients(pg):
    db,pid=pg
    one=SyncSession(db,pid,'endpoint','branch',0,'test').acquire()
    try:
        with pytest.raises(SyncBusy):SyncSession(db,pid,'endpoint','branch',0,'test').acquire()
        with db.tx() as c:c.execute('UPDATE sync_locks SET lease_until=? WHERE project_id=?',(timestamp(-5),pid))
        two=SyncSession(db,pid,'endpoint','branch',0,'test').acquire()
        one.release('stale',{})
        assert db.one('SELECT owner FROM sync_locks WHERE project_id=?',(pid,))['owner']==two.run_id
        two.release('complete',{})
    finally:one.release('closed',{})
