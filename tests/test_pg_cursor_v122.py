"""Regression tests for v1.2.2 PostgreSQL adapter (no live server required)."""
from __future__ import annotations

import sqlite3
from app.pg_store import PgCursor
from app.store import Store, now, uid


class SqlitePsycopgLike:
    """Exercise the PgCursor API using SQLite without a running PG service."""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=()):
        return self.conn.execute(query.replace('%s', '?'), params)


def test_pg_cursor_supports_direct_result_iteration():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        pg = PgCursor(SqlitePsycopgLike(conn))
        pg.execute('CREATE TABLE records(id TEXT, name TEXT)')
        pg.execute('INSERT INTO records(id,name) VALUES (?,?)', ('r1', '网关'))
        pg.execute('INSERT INTO records(id,name) VALUES (?,?)', ('r2', '主板'))
        rows = [dict(r) for r in pg.execute('SELECT * FROM records ORDER BY id')]
        assert [r['name'] for r in rows] == ['网关', '主板']
        assert pg.execute('SELECT * FROM records ORDER BY id').fetchall()[0]['id'] == 'r1'
        assert pg.execute('SELECT * FROM records WHERE id=?', ('r2',)).fetchone()['name'] == '主板'
    finally:
        conn.close()


def test_pg_adapter_snapshot_of_created_workspace(tmp_path, monkeypatch):
    """Reproduce the logged code path: snapshot after transaction commits."""
    monkeypatch.delenv('DATABASE_URL', raising=False)
    store = Store(tmp_path / 'data')
    pid = uid()
    eid = 'ec45cfd1-1f24-480c-bce3-daeabf388ce4'
    with store.tx() as c:
        c.execute('INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)',
                  (pid, '已成功写入的工作空间', now(), now()))
        c.execute("""INSERT INTO entities(id,project_id,name,kind,description,class_id,evidence,origin)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (eid, pid, '网关', 'class', '', None, '[]', 'openmind'))
    # PG adapter must not raise TypeError during the response/snapshot phase.
    with store.tx() as c:
        pg = PgCursor(SqlitePsycopgLike(c))
        snapshot = store.snapshot(pid, conn=pg)
    assert snapshot['project']['id'] == pid
    assert [r['id'] for r in snapshot['entities']] == [eid]
    assert snapshot['attributes'] == []
    assert snapshot['relations'] == []
    assert store.project(pid)['name'] == '已成功写入的工作空间'


def test_pg_cursor_iterator_keeps_its_own_result():
    """Interleaved queries should not accidentally iterate the latest result."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        pg = PgCursor(SqlitePsycopgLike(conn))
        pg.execute('SELECT 1 AS first')
        iterator = iter(pg)
        pg.execute('SELECT 2 AS second')
        assert dict(next(iterator)) == {'first': 1}
        assert [dict(x) for x in pg] == [{'second': 2}]
    finally:
        conn.close()
