"""Read-only integrity check for a freshly initialized PostgreSQL install.

Run after compose startup:
  docker compose exec ontology python -m scripts.verify_fresh_pg --expect-empty

--expect-empty is only valid before any workspace is created.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def verify(expect_empty=False):
    if not os.getenv('DATABASE_URL', '').startswith(('postgres://', 'postgresql://')):
        raise RuntimeError('当前进程没有使用 PostgreSQL DATABASE_URL')
    from app.store import Store
    db = Store(os.environ.get('DATA_DIR', '/app/data'))
    try:
        if not getattr(db, 'is_postgres', False):
            raise RuntimeError('当前数据库不是 PostgreSQL')
        marker = db.one('SELECT install_uuid,installed_at,schema_version FROM ontoflow_install WHERE id=1')
        if not marker:
            raise RuntimeError('数据库缺少全新安装标记')
        objects = {}
        for name in ('projects', 'documents', 'proposals', 'entities', 'attributes', 'relations',
                     'workflow_jobs', 'document_segments', 'extraction_cache', 'openmind_settings'):
            objects[name] = int(db.one(f'SELECT COUNT(*) AS n FROM {name}')['n'])
        if expect_empty and any(objects.get(name, 0) for name in
                                ('projects','documents','proposals','entities','attributes','relations')):
            raise RuntimeError('数据库已有工作空间或本体对象，未通过空库验证；不会清除任何数据')
        return {'ok': True, 'storage': 'postgresql', 'schema_version': marker['schema_version'],
                'install_uuid': marker['install_uuid'], 'counts': objects}
    finally:
        db.close()


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--expect-empty',action='store_true',help='确认尚未创建业务数据')
    args=parser.parse_args(argv)
    try:
        print(json.dumps(verify(args.expect_empty),ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        print(f'PostgreSQL 验证失败：{exc}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
