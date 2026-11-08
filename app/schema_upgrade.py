"""Additive v1.3.7 metadata migration; no source/business data is deleted."""
JOB_COLUMNS={
 'worker_id':'TEXT','lease_until':'TEXT','heartbeat_at':'TEXT',
 'attempts':'INTEGER NOT NULL DEFAULT 0','next_run_at':'TEXT',
 'lease_generation':'INTEGER NOT NULL DEFAULT 0',
}
GAP_COLUMNS={
 'status':"TEXT NOT NULL DEFAULT 'open'",'decision':"TEXT NOT NULL DEFAULT ''",
 'decided_at':'TEXT','decision_revision':'INTEGER NOT NULL DEFAULT 0',
 'document_id':'TEXT','content_hash':'TEXT','cache_key':'TEXT',
 'segment_ordinal':'INTEGER','resolved_by':"TEXT NOT NULL DEFAULT ''",
}
TABLES=[
"""CREATE TABLE IF NOT EXISTS gap_decisions (
 id TEXT PRIMARY KEY, gap_id TEXT NOT NULL REFERENCES extraction_gaps(id),
 project_id TEXT NOT NULL REFERENCES projects(id),action TEXT NOT NULL,
 note TEXT NOT NULL,actor TEXT NOT NULL,revision INTEGER NOT NULL,
 result TEXT NOT NULL,created_at TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS sync_locks (
 project_id TEXT PRIMARY KEY REFERENCES projects(id),owner TEXT NOT NULL,
 generation INTEGER NOT NULL,lease_until TEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS openmind_operations (
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
 run_id TEXT NOT NULL,endpoint_hash TEXT NOT NULL,branch_id TEXT NOT NULL,
 revision INTEGER NOT NULL,kind TEXT NOT NULL,local_ids TEXT NOT NULL,
 remote_id TEXT NOT NULL,code TEXT NOT NULL,action TEXT NOT NULL,
 desired TEXT NOT NULL,request_hash TEXT NOT NULL,before_json TEXT NOT NULL,
 state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
"CREATE INDEX IF NOT EXISTS idx_sync_operations_reconcile ON openmind_operations(project_id,endpoint_hash,branch_id,state)",
]

def upgrade(conn, postgres=False):
    for table, columns in [('workflow_jobs',JOB_COLUMNS),('extraction_gaps',GAP_COLUMNS),
                           ('documents',{'content_revision':'INTEGER NOT NULL DEFAULT 0'}),
                           ('projects',{'proposal_generation':'INTEGER NOT NULL DEFAULT 0'})]:
        existing=None if postgres else {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        for name, definition in columns.items():
            if postgres:conn.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}')
            elif name not in existing:conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    # Parses and targeted gap-retries are unique by document/gap, not workspace.
    conn.execute('DROP INDEX IF EXISTS uq_jobs_active_per_stage')
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_active_workflow_stage ON workflow_jobs(project_id,job_kind,stage)
        WHERE state IN ('queued','running') AND job_kind NOT IN ('parse','gap_retry')""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_active_document_job ON workflow_jobs(project_id,job_kind,source_fingerprint)
        WHERE state IN ('queued','running') AND job_kind IN ('parse','gap_retry')""")
    for sql in TABLES:conn.execute(sql)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gap_status ON extraction_gaps(project_id,stage,status,created_at,id)')
