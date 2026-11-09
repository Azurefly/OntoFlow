"""Real PostgreSQL regression for repeated source occurrences; never mocks PG."""
from test_postgres_integration_v137 import pg
from app.store import uid,now,j
from app.incremental import persist_segments


def test_real_pg_repeated_segments_reparse_is_atomic_and_idempotent(pg):
    db,pid=pg;did=uid()
    with db.tx() as c:
        c.execute('INSERT INTO documents(id,project_id,filename,sha256,bytes,path,content,chunks,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                  (did,pid,'repeated.html','fixed-hash',0,'/not-used-by-this-test','条款',j([]),now()))
    doc={'id':did,'project_id':pid,'chunks':[{'kind':'prose','locator':'HTML 行1','text':'申请资料需审核。'} for _ in range(100)]}
    before=persist_segments(db,doc);after=persist_segments(db,doc)
    assert len({p['id'] for p in before})==100
    assert [p['id'] for p in before]==[p['id'] for p in after]
    assert db.one('SELECT COUNT(*) n FROM document_segments WHERE document_id=?',(did,))['n']==100
