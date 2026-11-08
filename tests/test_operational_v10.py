"""Offline regression: PG SQL rewriting, pagination under parallel reads and
recoverable cache. Real PG failover/load must be tested on the target host.
"""
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from app.main import create_app
from app.pg_store import PgCursor
from app.store import Store
from app.incremental import cached_clues,sha


class FakeConn:
    def execute(self,sql,params):
        self.sql,self.params=sql,params
        return self
    def fetchone(self):return {'n':1}


def test_pg_legacy_sql_adapter():
    fake=FakeConn()
    c=PgCursor(fake)
    c.execute('SELECT * FROM messages WHERE project_id=? ORDER BY rowid DESC LIMIT 24',('pid',))
    assert 'ORDER BY created_at DESC, id DESC' in fake.sql and '%s' in fake.sql
    assert fake.params==('pid',)
    c.execute('BEGIN IMMEDIATE')
    assert fake.sql=='SELECT 1'


def test_50_parallel_readonly_requests(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('API_TOKEN',raising=False)
    with TestClient(create_app(tmp_path/'data')) as client:
        pid=client.post('/api/projects',json={'name':'parallel-read'}).json()['project']['id']
        def run_one(i):
            r=client.get('/api/ready' if i%2 else f'/api/projects/{pid}/proposals/page?kind=entity&page_size=10')
            return r.status_code
        with ThreadPoolExecutor(max_workers=20) as pool:
            responses=list(pool.map(run_one,range(50)))
        assert responses==[200]*50


def test_cache_survives_store_recreation(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    store=Store(tmp_path)
    counter=[]
    piece={'content_hash':sha('文档'), 'content':'文档'}
    model={'model':'qwen','base_url':'http://model.example'}
    def extract(piece):
        counter.append(1)
        return {'entities':[],'attributes':[],'relations':[]}
    assert cached_clues(store,piece,model,extract)[1] is False
    restored=Store(tmp_path)
    assert cached_clues(restored,piece,model,extract)[1] is True
    assert len(counter)==1


