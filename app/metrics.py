"""Task-scoped cost counters. No prompts, payloads, secrets or tokens are logged."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from time import perf_counter

_METRICS=ContextVar('ontoflow_metrics',default=None)
class Metrics:
    def __init__(self):self.lock=Lock();self.values={};self.started=perf_counter()
    def add(self,key,value=1):
        with self.lock:self.values[key]=self.values.get(key,0)+value
    def public(self):
        with self.lock:data={k:round(v,6) if isinstance(v,float) else v for k,v in self.values.items()}
        data['elapsed_seconds']=round(perf_counter()-self.started,6)
        data['token_note']='仅记录供应商实际返回的 usage；缺失时不估造 token 数'
        return data

def count(key,value=1):
    metrics=_METRICS.get()
    if metrics:metrics.add(key,value)

@contextmanager
def scope():
    m=Metrics();token=_METRICS.set(m)
    try:yield m
    finally:_METRICS.reset(token)

@contextmanager
def elapsed(key):
    start=perf_counter()
    try:yield
    finally:count(key,perf_counter()-start)


def attach_to_job_result(conn, job_id):
    """Save the available metrics in the SAME transaction as a terminal state.

    The task wrapper can update elapsed/SQL counters after final progress events,
    but polling must never observe succeeded/partial/failed without the measured
    call/cache statistics already accumulated by the task. With no active scope,
    do not invent measurements or change a historical record.
    """
    import json
    metrics=_METRICS.get()
    if metrics is None:return
    row=conn.execute('SELECT result FROM workflow_jobs WHERE id=?',(job_id,)).fetchone()
    if row is None:return
    report=json.loads(row['result'] or '{}')
    report['metrics']=metrics.public()
    conn.execute('UPDATE workflow_jobs SET result=? WHERE id=?',
                 (json.dumps(report,ensure_ascii=False),job_id))
