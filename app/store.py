"""SQLite persistence: all accepted changes are transactional and auditable."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def uid():
    return uuid.uuid4().hex[:14]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def j(value):
    return json.dumps(value, ensure_ascii=False)


def unpack(row):
    return dict(row) if row is not None else None


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS openmind_bindings (
 project_id TEXT PRIMARY KEY REFERENCES projects(id), domain_id TEXT NOT NULL,
 branch_id TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS openmind_codes (
 project_id TEXT NOT NULL REFERENCES projects(id),kind TEXT NOT NULL,local_id TEXT NOT NULL,
 code TEXT NOT NULL, PRIMARY KEY(project_id,kind,local_id)
);
CREATE TABLE IF NOT EXISTS openmind_mappings (
 project_id TEXT NOT NULL REFERENCES projects(id),kind TEXT NOT NULL,local_id TEXT NOT NULL,
 remote_id TEXT NOT NULL,code TEXT NOT NULL,lock_version INTEGER,
 updated_at TEXT NOT NULL,PRIMARY KEY(project_id,kind,local_id)
);
CREATE TABLE IF NOT EXISTS openmind_sync_runs (
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
 revision INTEGER NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS openmind_baselines (
 project_id TEXT PRIMARY KEY REFERENCES projects(id),
 domain_id TEXT NOT NULL,branch_id TEXT NOT NULL,endpoint_hash TEXT NOT NULL DEFAULT '',
 catalog_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
 entity_count INTEGER NOT NULL,attribute_count INTEGER NOT NULL,relation_count INTEGER NOT NULL,
 action_count INTEGER NOT NULL,capability_count INTEGER NOT NULL,
 refreshed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS openmind_settings (
 id INTEGER PRIMARY KEY CHECK(id=1),base_url TEXT NOT NULL DEFAULT '',
 access_token_ciphertext TEXT NOT NULL DEFAULT '',
 allowed_hosts TEXT NOT NULL DEFAULT '[]',admin_token_hash TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_settings (
 id INTEGER PRIMARY KEY CHECK(id=1), base_url TEXT NOT NULL DEFAULT '',
 model TEXT NOT NULL DEFAULT '', api_key_ciphertext TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'UPLOAD',
 summary TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '', scope_confirmed INTEGER NOT NULL DEFAULT 0,
 candidate_summary TEXT NOT NULL DEFAULT '', candidate_scope TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0, published_version INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 filename TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, path TEXT NOT NULL,
 content TEXT NOT NULL, chunks TEXT NOT NULL, warnings TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL,
 UNIQUE(project_id,sha256)
);
CREATE TABLE IF NOT EXISTS summary_jobs (
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
 state TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
 completed_batches INTEGER NOT NULL DEFAULT 0,total_batches INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',source_fingerprint TEXT NOT NULL,
 project_revision INTEGER NOT NULL, created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_jobs_project ON summary_jobs(project_id,created_at);
CREATE TABLE IF NOT EXISTS workflow_jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 job_kind TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
 completed_units INTEGER NOT NULL DEFAULT 0, total_units INTEGER NOT NULL DEFAULT 0,
 detail TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 source_fingerprint TEXT NOT NULL DEFAULT '',
 result TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_jobs_project ON workflow_jobs(project_id,job_kind,stage,created_at);
CREATE TABLE IF NOT EXISTS job_progress_events (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 job_id TEXT NOT NULL REFERENCES workflow_jobs(id),
 stage TEXT NOT NULL, phase TEXT NOT NULL,
 content TEXT NOT NULL, percent INTEGER,
 eta_seconds INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_progress_job ON job_progress_events(job_id,created_at,id);
CREATE INDEX IF NOT EXISTS idx_job_progress_project ON job_progress_events(project_id,created_at,id);
CREATE TABLE IF NOT EXISTS extraction_gaps (
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
 stage TEXT NOT NULL,batch_number INTEGER NOT NULL,
 kind TEXT NOT NULL,payload TEXT NOT NULL,evidence TEXT NOT NULL,
 reason TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extraction_gaps_project ON extraction_gaps(project_id,stage,created_at);
CREATE TABLE IF NOT EXISTS extraction_batch_audit (
 project_id TEXT NOT NULL REFERENCES projects(id),stage TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL,batch_number INTEGER NOT NULL,
 proposed_count INTEGER NOT NULL,accepted_for_review INTEGER NOT NULL,
 reused_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(project_id,stage,source_fingerprint,batch_number)
);
CREATE INDEX IF NOT EXISTS idx_extraction_batch_audit ON extraction_batch_audit(project_id,stage,source_fingerprint);
CREATE TABLE IF NOT EXISTS proposal_progress (
 project_id TEXT NOT NULL REFERENCES projects(id),stage TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL, next_batch INTEGER NOT NULL,
 total_batches INTEGER NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(project_id,stage)
);
CREATE TABLE IF NOT EXISTS proposals (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 stage TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]',
 source TEXT NOT NULL DEFAULT 'human', status TEXT NOT NULL DEFAULT 'pending',
 base_revision INTEGER NOT NULL, review_note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS entities (
 id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'class', description TEXT NOT NULL DEFAULT '',
 class_id TEXT, evidence TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(project_id,id), UNIQUE(project_id,name,kind)
);
CREATE TABLE IF NOT EXISTS attributes (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 owner_id TEXT NOT NULL, name TEXT NOT NULL,
 datatype TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 0, multivalued INTEGER NOT NULL DEFAULT 0,
 description TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '[]',
 UNIQUE(project_id,owner_id,name),
 CONSTRAINT attributes_owner_project_fkey FOREIGN KEY(project_id,owner_id) REFERENCES entities(project_id,id)
);
CREATE TABLE IF NOT EXISTS relations (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 source_id TEXT NOT NULL, target_id TEXT NOT NULL,
 predicate TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '[]',
 UNIQUE(project_id,source_id,predicate,target_id),
 CONSTRAINT relations_source_project_fkey FOREIGN KEY(project_id,source_id) REFERENCES entities(project_id,id),
 CONSTRAINT relations_target_project_fkey FOREIGN KEY(project_id,target_id) REFERENCES entities(project_id,id)
);
CREATE TABLE IF NOT EXISTS relation_labels (
 project_id TEXT NOT NULL REFERENCES projects(id),
 relation_id TEXT NOT NULL REFERENCES relations(id),
 name TEXT NOT NULL, PRIMARY KEY(project_id,relation_id)
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 stage TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
 source TEXT NOT NULL DEFAULT 'chat'
);
CREATE TABLE IF NOT EXISTS releases (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 version INTEGER NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
 note TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(project_id,version)
);
CREATE TABLE IF NOT EXISTS audit (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_project ON proposals(project_id,status);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit(project_id,created_at);

CREATE TABLE IF NOT EXISTS document_segments (
 id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),
 project_id TEXT NOT NULL REFERENCES projects(id),locator TEXT NOT NULL,
 ordinal INTEGER NOT NULL,content_hash TEXT NOT NULL,content TEXT NOT NULL,
 parser_version TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(document_id,ordinal,parser_version)
);
CREATE INDEX IF NOT EXISTS idx_segments_document ON document_segments(document_id,ordinal);
CREATE TABLE IF NOT EXISTS extraction_cache (
 cache_key TEXT PRIMARY KEY,content_hash TEXT NOT NULL,model_name TEXT NOT NULL,
 extractor_version TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summary_cache (
 cache_key TEXT PRIMARY KEY,content_hash TEXT NOT NULL,
 model_name TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_page ON proposals(project_id,kind,status,created_at,id);
CREATE TABLE IF NOT EXISTS project_deletions (
 project_id TEXT PRIMARY KEY, project_name TEXT NOT NULL,
 actor TEXT NOT NULL, deleted_at TEXT NOT NULL, counts_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS modeling_contexts (
 project_id TEXT PRIMARY KEY REFERENCES projects(id),version INTEGER NOT NULL,
 spec TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS modeling_decisions (
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),version INTEGER NOT NULL,
 before_spec TEXT NOT NULL,after_spec TEXT NOT NULL,note TEXT NOT NULL,
 actor TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(project_id,version)
);
CREATE TABLE IF NOT EXISTS proposal_origins (
 proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),project_id TEXT NOT NULL REFERENCES projects(id),
 basis TEXT NOT NULL,context_fingerprint TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_modeling_decisions_project ON modeling_decisions(project_id,version);
CREATE INDEX IF NOT EXISTS idx_proposal_origins_project ON proposal_origins(project_id,context_fingerprint);


"""


class Store:
    is_postgres = False

    def __new__(cls, root):
        import os
        if cls is Store and os.getenv('DATABASE_URL', '').strip():
            from .pg_store import PgStore
            return object.__new__(PgStore)
        return object.__new__(cls)

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'ontology.sqlite3'
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir(exist_ok=True)
        with self.tx() as conn:
            conn.executescript(SCHEMA)
            om_cols={row['name'] for row in conn.execute('PRAGMA table_info(openmind_settings)')}
            for column, ddl in [('auth_mode',"TEXT NOT NULL DEFAULT 'token'"),
                                ('username',"TEXT NOT NULL DEFAULT ''"),
                                ('password_ciphertext',"TEXT NOT NULL DEFAULT ''")]:
                if column not in om_cols:
                    conn.execute(f'ALTER TABLE openmind_settings ADD COLUMN {column} {ddl}')
            columns={row['name'] for row in conn.execute('PRAGMA table_info(messages)')}
            if 'source' not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'")
            audit_cols={row['name'] for row in conn.execute('PRAGMA table_info(extraction_batch_audit)')}
            if 'reused_count' not in audit_cols:
                conn.execute('ALTER TABLE extraction_batch_audit ADD COLUMN reused_count INTEGER NOT NULL DEFAULT 0')
            baseline_columns={row['name'] for row in conn.execute('PRAGMA table_info(openmind_baselines)')}
            if 'endpoint_hash' not in baseline_columns:
                conn.execute("ALTER TABLE openmind_baselines ADD COLUMN endpoint_hash TEXT NOT NULL DEFAULT ''")
            entity_columns={row['name'] for row in conn.execute('PRAGMA table_info(entities)')}
            if 'origin' not in entity_columns:
                conn.execute("ALTER TABLE entities ADD COLUMN origin TEXT NOT NULL DEFAULT 'local'")
            from .schema_upgrade import upgrade
            upgrade(conn)
            # An in-process worker cannot survive a service restart. A stale job
            # must be visible as interrupted, never as a forever-spinning task.
            conn.execute("UPDATE summary_jobs SET state='interrupted',phase='interrupted',error='服务已重启或任务意外中断，请重新生成概要。',updated_at=? WHERE state IN ('queued','running')",(now(),))
            conn.execute("UPDATE workflow_jobs SET state='interrupted',phase='interrupted',error='服务已重启，后台任务已中断。可以重新运行；已有批次结果仍然保留。',updated_at=? WHERE state IN ('queued','running')",(now(),))

    @contextmanager
    def tx(self, *, read_only=False):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=30000')
        from .metrics import count as metric_count
        from time import perf_counter
        begin=perf_counter();metric_count('database_transactions')
        conn.set_trace_callback(lambda _sql:metric_count('sql_statements'))
        from .task_leases import validate_transaction, committed, current_lease
        try:
            if not read_only and current_lease() is not None:
                conn.execute('BEGIN IMMEDIATE')
                validate_transaction(conn)
            yield conn
            final=validate_transaction(conn,finishing=True) if not read_only else None
            conn.commit()
            committed(final)
        except Exception:
            conn.rollback()
            raise
        finally:
            metric_count('database_transaction_seconds',perf_counter()-begin)
            conn.close()

    def one(self, sql, params=()):
        with self.tx(read_only=True) as c:
            return unpack(c.execute(sql, params).fetchone())

    def many(self, sql, params=()):
        with self.tx(read_only=True) as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def project(self, project_id):
        return self.one('SELECT * FROM projects WHERE id=?', (project_id,))

    def audit(self, conn, project_id, actor, action, detail):
        conn.execute('INSERT INTO audit VALUES(?,?,?,?,?,?)',
                     (uid(), project_id, actor, action, j(detail), now()))

    def snapshot(self, project_id, conn=None):
        def collect(c):
            e = [dict(r) for r in c.execute('SELECT * FROM entities WHERE project_id=? ORDER BY name', (project_id,)).fetchall()]
            a = [dict(r) for r in c.execute('SELECT * FROM attributes WHERE project_id=? ORDER BY name', (project_id,)).fetchall()]
            r = [dict(r) for r in c.execute('SELECT * FROM relations WHERE project_id=? ORDER BY predicate', (project_id,)).fetchall()]
            p = unpack(c.execute('SELECT id,name,scope,summary,revision,published_version FROM projects WHERE id=?', (project_id,)).fetchone())
            from .naming import enrich_snapshot
            enrich_snapshot(c,project_id,e,a,r)
            return {'project': p, 'entities': e, 'attributes': a, 'relations': r}
        if conn is not None:
            return collect(conn)
        with self.tx(read_only=True) as c:
            return collect(c)

    def revision(self, conn, project_id):
        conn.execute('UPDATE projects SET revision=revision+1,updated_at=? WHERE id=?', (now(),project_id))

    def record_action(self, conn, project_id, stage, statement, reply):
        """Atomically synchronize a validated UI decision into Agent history.

        This records a factual UI event and deterministic acknowledgement, not
        a fabricated LLM completion. The real Agent consumes these in chat history.
        """
        for role, content in [('user', statement), ('assistant', reply)]:
            conn.execute('''INSERT INTO messages
                (id,project_id,stage,role,content,created_at,source)
                VALUES (?,?,?,?,?,?,?)''',
                (uid(),project_id,stage,role,content[:4000],now(),'action'))
