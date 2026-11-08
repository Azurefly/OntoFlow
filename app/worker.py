"""Single-host durable worker with renewable, fenced PostgreSQL claims."""
from __future__ import annotations
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from .task_leases import Lease, LostLease, claim_job, heartbeat, timestamp

log=logging.getLogger('ontoflow.worker')

def work(app,job):
    store=app.state.store
    lease=Lease(job['id'],job['worker_id'],job['lease_generation'])
    stop=threading.Event()
    def renew():
        while not stop.wait(max(5,int(os.getenv('JOB_LEASE_SECONDS','120'))//3)):
            try:
                if not heartbeat(store,lease):
                    log.warning('Lease lost: %s generation=%s',lease.job_id,lease.generation)
                    return
            except Exception:
                log.exception('Heartbeat failed for %s; fenced commits still enforce expiration',lease.job_id)
    watcher=threading.Thread(target=renew,daemon=True);watcher.start()
    try:
        app.state.run_job(job['project_id'],job['id'],job['job_kind'],job['stage'],
                         job['source_fingerprint'],({} if job['job_kind']=='review_all' else app.state.model_settings.effective()),
                         'background-worker',lease=lease)
    except LostLease:
        log.warning('Discarding stale result for %s',lease.job_id)
    except Exception:
        log.exception('Worker failed for %s',lease.job_id)
        with store.tx() as c:
            c.execute("""UPDATE workflow_jobs SET state='failed',phase='failed',error='Worker 异常，请查看日志',updated_at=?
                WHERE id=? AND worker_id=? AND lease_generation=? AND state='running' AND lease_until>?""",
                (timestamp(),lease.job_id,lease.owner,lease.generation,timestamp()))
    finally:
        stop.set();watcher.join(timeout=2)
        with store.tx() as c:
            c.execute("""UPDATE workflow_jobs SET worker_id=NULL,lease_until=NULL WHERE id=? AND worker_id=?
                AND lease_generation=? AND state<>'running'""",(lease.job_id,lease.owner,lease.generation))

def main():
    from .main import app
    store=app.state.store
    if not store.is_postgres:raise RuntimeError('后台 Worker 需要 PostgreSQL DATABASE_URL')
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'))
    size=max(1,min(16,int(os.getenv('WORKER_CONCURRENCY','2'))))
    host=uuid.uuid4().hex;last_beat=0
    with ThreadPoolExecutor(max_workers=size,thread_name_prefix='ontoflow-task') as pool:
        active={}
        while True:
            for future in list(active):
                if future.done():
                    try:future.result()
                    except Exception:log.exception('Unhandled job error')
                    del active[future]
            if time.monotonic()-last_beat>=5:
                with store.tx() as c:
                    c.execute('INSERT INTO worker_heartbeats(worker_id,updated_at) VALUES(?,?) ON CONFLICT(worker_id) DO UPDATE SET updated_at=excluded.updated_at',(host,timestamp()))
                last_beat=time.monotonic()
            while len(active)<size:
                job=claim_job(store,uuid.uuid4().hex)
                if not job:break
                active[pool.submit(work,app,job)]=job['id']
            time.sleep(1)

if __name__=='__main__':main()
