"""PostgreSQL-only production store with transactional fresh-schema initialization.

No SQLite data or legacy installation is imported into this database.
"""
from __future__ import annotations
import os
import re
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from .store import Store, SCHEMA


class PgCursor:
    def __init__(self, conn): self.conn = conn

    def execute(self, query, params=()):
        sql = query.strip()
        if sql.upper() == 'BEGIN IMMEDIATE':
            # PostgreSQL transaction already started. Lock on the project row
            # in review paths, never a global advisory lock.
            sql = 'SELECT 1'
        sql = re.sub(r'\bORDER BY rowid DESC\b', 'ORDER BY created_at DESC, id DESC', sql, flags=re.I)
        sql = re.sub(r'\bORDER BY rowid\b', 'ORDER BY created_at, id', sql, flags=re.I)
        # Existing queries use positional SQLite placeholders. There are no
        # literal question mark operators in the application's SQL contract.
        sql = sql.replace('?', '%s')
        from .metrics import count
        count('sql_statements')
        self.result = self.conn.execute(sql, params)
        return self

    @property
    def rowcount(self): return self.result.rowcount

    def fetchone(self): return self.result.fetchone()
    def fetchall(self): return self.result.fetchall()

    def __iter__(self):
        """Match the sqlite3 Cursor protocol used by existing read paths.

        The active psycopg result cursor supports iteration, but this adapter
        must forward it explicitly. Keep the result cursor local to the
        iterator so subsequent statements do not replace its reference.
        """
        return iter(self.result)


class PgStore(Store):
    is_postgres = True

    def __init__(self, root):
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError('使用 PostgreSQL 必须安装 psycopg[binary,pool]') from exc
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = None
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir(parents=True, exist_ok=True)
        dsn = os.environ['DATABASE_URL']
        if not dsn.startswith(('postgresql://', 'postgres://')):
            raise RuntimeError('DATABASE_URL 必须为 PostgreSQL 连接串。')
        self.pool = ConnectionPool(conninfo=dsn, min_size=1,
                                   max_size=int(os.getenv('PG_POOL_SIZE', '12')),
                                   kwargs={'autocommit': False,
                                           'row_factory': self._row_factory()})
        self.pool.wait(timeout=30)
        self._initialize_schema()

    @staticmethod
    def _row_factory():
        from psycopg.rows import dict_row
        return dict_row

    def _initialize_schema(self):
        statements=[x.strip() for x in SCHEMA.split(';') if x.strip() and not x.strip().upper().startswith('PRAGMA')]
        tables=[x for x in statements if x.upper().startswith('CREATE TABLE')]
        indexes=[x for x in statements if x.upper().startswith('CREATE INDEX')]
        projects=[x for x in tables if re.search(r'CREATE TABLE IF NOT EXISTS projects\b',x,re.I)]
        ordered=projects+[x for x in tables if x not in projects]+indexes
        extras=[
            "ALTER TABLE openmind_settings ADD COLUMN IF NOT EXISTS auth_mode TEXT NOT NULL DEFAULT 'token'",
            "ALTER TABLE openmind_settings ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE openmind_settings ADD COLUMN IF NOT EXISTS password_ciphertext TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE entities ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'local'",
            "ALTER TABLE workflow_jobs ADD COLUMN IF NOT EXISTS worker_id TEXT",
            "ALTER TABLE workflow_jobs ADD COLUMN IF NOT EXISTS lease_until TEXT",
            "ALTER TABLE workflow_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TEXT",
            "ALTER TABLE workflow_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE workflow_jobs ADD COLUMN IF NOT EXISTS next_run_at TEXT",
            "CREATE INDEX IF NOT EXISTS idx_jobs_pick ON workflow_jobs(state,lease_until,created_at,id)",

            "CREATE INDEX IF NOT EXISTS idx_proposals_page ON proposals(project_id,kind,status,created_at,id)",
            """CREATE TABLE IF NOT EXISTS worker_heartbeats (
                 worker_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS document_segments (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                locator TEXT NOT NULL, ordinal INTEGER NOT NULL,
                content_hash TEXT NOT NULL, content TEXT NOT NULL,
                parser_version TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(document_id,ordinal,parser_version))""",
            "CREATE INDEX IF NOT EXISTS idx_segments_document ON document_segments(document_id,ordinal)",
            """CREATE TABLE IF NOT EXISTS extraction_cache (
                cache_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, model_name TEXT NOT NULL,
                extractor_version TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS summary_cache (
                cache_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                model_name TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)""",
        ]
        with self.tx() as conn:
            # API and worker may start at once. An advisory transaction lock
            # serializes creation of all tables and the installation marker.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('ontoflow-schema-v1'))")
            had_projects = conn.execute("SELECT to_regclass('public.projects') AS table_name").fetchone()['table_name'] is not None
            had_marker = conn.execute("SELECT to_regclass('public.ontoflow_install') AS table_name").fetchone()['table_name'] is not None
            if os.getenv('ONTOFLOW_FRESH_ONLY') == '1':
                if had_projects and not had_marker:
                    raise RuntimeError('检测到未标记的既有 PostgreSQL 数据库；全新部署拒绝接管旧库。请使用新 Compose 项目和独立 PG 卷。')
                if had_marker and not had_projects:
                    raise RuntimeError('安装标记存在但核心 projects 表丢失；为防止数据覆盖已拒绝启动。')
            for sql in ordered+extras:
                conn.execute(sql)
            # v1.2.1: make remote IDs workspace-scoped without rewriting UUIDs.
            # This is an in-place, transactional migration of the v1.1/v1.2
            # schema. No existing projects, candidates, or baselines are erased.
            PgStore._ensure_workspace_entity_identity(conn)
            conn.execute('''CREATE TABLE IF NOT EXISTS ontoflow_install (
                id INTEGER PRIMARY KEY CHECK(id=1), install_uuid TEXT NOT NULL,
                installed_at TEXT NOT NULL, schema_version TEXT NOT NULL)''')
            if not had_marker:
                conn.execute('''INSERT INTO ontoflow_install(id,install_uuid,installed_at,schema_version)
                                VALUES (1,%s,%s,%s)''',
                             (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat(), '1.2.1'))
            elif not conn.execute('SELECT 1 AS ok FROM ontoflow_install WHERE id=1').fetchone():
                raise RuntimeError('PostgreSQL 安装标记缺失，拒绝启动。')
            from .schema_upgrade import upgrade
            upgrade(PgCursor(conn),postgres=True)
            from .version import SCHEMA_VERSION
            conn.execute("UPDATE ontoflow_install SET schema_version=%s WHERE id=1",(SCHEMA_VERSION,))

    @staticmethod
    def _ensure_workspace_entity_identity(conn):
        """Upgrade a global-UUID PK to (project_id,id) atomically.

        Requires every attribute and relation endpoint to belong to its own
        workspace. Abort rather than silently rewriting cross-project edges.
        """
        pk = conn.execute("""SELECT pg_get_constraintdef(oid) AS definition
            FROM pg_constraint WHERE conrelid='public.entities'::regclass AND contype='p'""").fetchone()
        if pk is None:
            raise RuntimeError('entities 表缺少主键；请先备份并排查数据库结构')
        if pk['definition'] == 'PRIMARY KEY (id)':
            checks = (
                ('attributes', 'owner_id'),
                ('relations', 'source_id'),
                ('relations', 'target_id'),
            )
            for table, column in checks:
                bad = conn.execute(f"""SELECT COUNT(*) AS n FROM {table} x
                    LEFT JOIN entities e ON e.id=x.{column} AND e.project_id=x.project_id
                    WHERE e.id IS NULL""").fetchone()['n']
                if bad:
                    raise RuntimeError(f'{table}.{column} 有 {bad} 条跨工作空间或无效引用，拒绝自动修改约束')
            # Drop old single-column FKs before removing the old global PK.
            conn.execute('ALTER TABLE attributes DROP CONSTRAINT IF EXISTS attributes_owner_id_fkey')
            conn.execute('ALTER TABLE relations DROP CONSTRAINT IF EXISTS relations_source_id_fkey')
            conn.execute('ALTER TABLE relations DROP CONSTRAINT IF EXISTS relations_target_id_fkey')
            conn.execute('ALTER TABLE entities DROP CONSTRAINT entities_pkey')
            conn.execute('ALTER TABLE entities ADD CONSTRAINT entities_pkey PRIMARY KEY(project_id,id)')
        elif pk['definition'] != 'PRIMARY KEY (project_id, id)':
            raise RuntimeError('entities 主键结构不在支持范围内，拒绝自动迁移：'+pk['definition'])
        for table, name, column in (
            ('attributes','attributes_owner_project_fkey','owner_id'),
            ('relations','relations_source_project_fkey','source_id'),
            ('relations','relations_target_project_fkey','target_id'),
        ):
            existing = conn.execute("""SELECT 1 AS ok FROM pg_constraint
                WHERE conrelid=%s::regclass AND conname=%s""",
                ('public.'+table,name)).fetchone()
            if not existing:
                conn.execute(f"""ALTER TABLE {table} ADD CONSTRAINT {name}
                    FOREIGN KEY(project_id,{column}) REFERENCES entities(project_id,id)""")

    @contextmanager
    def tx(self, *, read_only=False):
        from .task_leases import validate_transaction, committed
        final=None
        from .metrics import elapsed,count
        count('database_transactions')
        with elapsed('database_transaction_seconds'),self.pool.connection() as conn:
            cursor=PgCursor(conn)
            if not read_only:validate_transaction(cursor,postgres=True)
            yield cursor
            if not read_only:final=validate_transaction(cursor,postgres=True,finishing=True)
        committed(final)

    def close(self):
        self.pool.close()
