"""Durable, fenced task ownership. Long model calls never hold transactions.

All worker-originated DB transactions validate owner + generation + unexpired
lease while locking the job row. Every reclaimed attempt has a new generation.
The ContextVar is explicitly copied to extraction pool threads.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os

class LostLease(RuntimeError):
    pass

@dataclass
class Lease:
    job_id: str
    owner: str
    generation: int
    committed_state: str = 'running'

_ACTIVE=ContextVar('ontoflow_worker_lease',default=None)

def timestamp(delta=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=delta)).isoformat(timespec='microseconds')

@contextmanager
def lease_scope(lease):
    token=_ACTIVE.set(lease)
    try:yield
    finally:_ACTIVE.reset(token)

def current_lease():return _ACTIVE.get()

def validate_transaction(conn, postgres=False, *, finishing=False):
    lease=_ACTIVE.get()
    if lease is None:return None
    suffix=' FOR UPDATE' if postgres and not finishing else ''
    row=conn.execute('SELECT worker_id,lease_generation,lease_until,state FROM workflow_jobs WHERE id=?'+suffix,
                     (lease.job_id,)).fetchone()
    valid=(row and row['worker_id']==lease.owner and row['lease_generation']==lease.generation
           and row['lease_until'] and datetime.fromisoformat(row['lease_until'])>datetime.now(timezone.utc))
    if not valid:
        raise LostLease('任务租约已失效或被其他 Worker 接管；丢弃旧执行结果')
    if not finishing and row['state']!=lease.committed_state:
        raise LostLease('任务已取消或状态被其他执行器修改')
    return row['state']

def committed(state):
    lease=_ACTIVE.get()
    if lease is not None and state is not None:lease.committed_state=state

def claim_job(store, owner, job_id=None):
    stamp=timestamp();lease_seconds=max(30,int(os.getenv('JOB_LEASE_SECONDS','120')))
    with store.tx() as c:
        if not store.is_postgres:c.execute('BEGIN IMMEDIATE')
        where="((state='queued' AND (next_run_at IS NULL OR next_run_at<=?)) OR (state='running' AND (lease_until IS NULL OR lease_until<?)))"
        params=[stamp,stamp]
        if job_id:where+=' AND id=?';params.append(job_id)
        sql='SELECT * FROM workflow_jobs WHERE '+where+' ORDER BY created_at,id LIMIT 1'
        if store.is_postgres:sql+=' FOR UPDATE SKIP LOCKED'
        row=c.execute(sql,tuple(params)).fetchone()
        if not row:return None
        if row['attempts']>=int(os.getenv('JOB_MAX_ATTEMPTS','3')):
            c.execute("UPDATE workflow_jobs SET state='failed',phase='failed',error='已达到最大领取次数；需人工重试',updated_at=? WHERE id=?",(stamp,row['id']))
            return None
        generation=row['lease_generation']+1
        c.execute("""UPDATE workflow_jobs SET state='running',phase='preparing',worker_id=?,
                     lease_generation=?,attempts=attempts+1,lease_until=?,heartbeat_at=?,
                     next_run_at=NULL,updated_at=? WHERE id=?""",
                  (owner,generation,timestamp(lease_seconds),stamp,stamp,row['id']))
        job=dict(row);job.update(worker_id=owner,lease_generation=generation,state='running')
        return job

def heartbeat(store,lease):
    stamp=timestamp()
    with store.tx() as c:
        changed=c.execute("""UPDATE workflow_jobs SET lease_until=?,heartbeat_at=?
            WHERE id=? AND worker_id=? AND lease_generation=? AND state='running' AND lease_until>?""",
            (timestamp(max(30,int(os.getenv('JOB_LEASE_SECONDS','120')))),stamp,
             lease.job_id,lease.owner,lease.generation,stamp))
        return changed.rowcount==1
