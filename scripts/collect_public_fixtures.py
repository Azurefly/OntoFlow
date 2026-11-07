"""Collect new upstream files without creating application-specific answers.

Only public HTTPS sources are read. Source bytes are not transformed; renaming
an email fixture's extension is recorded. Raw samples stay outside Git history.
The manifest pins upstream commits and SHA256 so future runs can verify bytes.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

MAX_BYTES = 2_000_000
REPOS = {}

def request(url, *, json_data=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'api.github.com', 'raw.githubusercontent.com'}:
        raise ValueError('Source host is not in the public fixture allowlist')
    headers = {'User-Agent': 'OntoFlow-independent-fixture-collector/1.0'}
    token = os.environ.get('GH_TOKEN', '')
    if token and parsed.hostname == 'api.github.com':
        headers['Authorization'] = 'Bearer ' + token
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as response:
                data = response.read(25_000_001 if json_data else MAX_BYTES + 1)
            if len(data) > (25_000_000 if json_data else MAX_BYTES):
                raise ValueError('Download exceeds safety limit')
            return json.loads(data) if json_data else data
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(str(last))

def repository(owner_name):
    if owner_name not in REPOS:
        base = 'https://api.github.com/repos/' + owner_name
        metadata = request(base, json_data=True)
        commit = request(base + '/commits/' + metadata['default_branch'], json_data=True)['sha']
        tree = request(base + '/git/trees/' + commit + '?recursive=1', json_data=True)
        if tree.get('truncated'):
            raise ValueError('Repository tree truncated; do not claim exhaustive selection')
        files = [x for x in tree['tree'] if x['type'] == 'blob' and 0 < x.get('size', 0) <= MAX_BYTES]
        REPOS[owner_name] = {'commit': commit, 'files': files,
                            'repository_license': (metadata.get('license') or {}).get('spdx_id'),
                            'license_url': 'https://github.com/' + owner_name + '/blob/' + commit + '/LICENSE'}
    return REPOS[owner_name]

def candidates(repo, prefix='', suffix='', preferred=(), limit=1):
    files = repository(repo)['files']
    matches = [x for x in files if x['path'].startswith(prefix) and x['path'].lower().endswith(suffix)]
    def rank(item):
        name = Path(item['path']).name
        if name in preferred:
            return (0, preferred.index(name), item['path'])
        return (1, item.get('size', 0), item['path'])
    return sorted(matches, key=rank)[:limit]

SPECS = [
    ('docling_docx', 'docling-project/docling', 'tests/data/docx/sources/', '.docx',
     ('unit_test_formatting.docx', 'unit_test_tables.docx'), 3, None),
    ('docling_pptx', 'docling-project/docling', 'tests/data/pptx/sources/', '.pptx',
     ('powerpoint_sample.pptx', 'pptx_chart.pptx'), 2, None),
    ('docling_html', 'docling-project/docling', 'tests/data/html/sources/', '.html',
     ('example_01.html', 'wiki_duck.html'), 2, None),
    ('docling_xml', 'docling-project/docling', 'tests/data/', '.xml', (), 1, None),
    ('pdfminer_pdf', 'pdfminer/pdfminer.six', 'samples/', '.pdf',
     ('simple1.pdf', 'simple2.pdf', 'simple3.pdf'), 3, None),
    ('pandas_csv', 'pandas-dev/pandas', 'pandas/tests/io/data/csv/', '.csv',
     ('iris.csv', 'tips.csv'), 2, None),
    ('pandas_tsv', 'pandas-dev/pandas', 'pandas/tests/io/data/', '.tsv', (), 1, None),
    ('pandas_xlsx', 'pandas-dev/pandas', 'pandas/tests/io/data/excel/', '.xlsx',
     ('test1.xlsx', 'test2.xlsx'), 2, None),
    ('pandas_jsonl', 'pandas-dev/pandas', 'pandas/tests/io/data/', '.jsonl', (), 1, None),
    ('openapi_yaml', 'OAI/OpenAPI-Specification', '', '.yaml',
     ('petstore.yaml',), 1, None),
    ('openapi_json', 'OAI/OpenAPI-Specification', '', '.json',
     ('petstore.json',), 1, None),
    ('cpython_eml', 'python/cpython', 'Lib/test/test_email/data/', '.txt',
     ('msg_01.txt', 'msg_02.txt'), 2, '.eml'),
    ('cpython_rst', 'python/cpython', 'Doc/faq/', '.rst', ('index.rst',), 1, None),
    ('cpython_xml', 'python/cpython', 'Lib/test/xmltestdata/', '.xml',
     ('simple.xml', 'simple-ns.xml'), 1, None),
    ('jieba_chinese_md', 'fxsjy/jieba', '', '.md', ('README.md',), 1, None),
    ('postgres_sql', 'postgres/postgres', 'src/test/regress/sql/', '.sql',
     ('create_misc.sql',), 1, None),
    ('pdfminer_text', 'pdfminer/pdfminer.six', '', '.txt', ('requirements.txt',), 1, None),
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='collected-public-fixtures')
    args = parser.parse_args()
    root = Path(args.output)
    raw = root / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    manifest = {'schema': 'ontoflow.external-fixtures/v1',
                'collected_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'selection_policy': 'Predeclared upstream paths; preferred names else smallest eligible files. No application extraction counts used for selection.',
                'license_policy': 'Upstream repository license recorded for reference, not a blanket grant for every embedded asset. No raw corpus committed to Git.',
                'samples': [], 'collection_failures': []}
    for label, repo, prefix, suffix, preferred, limit, rename in SPECS:
        try:
            info = repository(repo)
            items = candidates(repo, prefix, suffix, preferred, limit)
            if not items:
                raise ValueError('No matching upstream file; no synthetic replacement')
            for index, entry in enumerate(items, 1):
                path = entry['path']
                url = 'https://raw.githubusercontent.com/' + repo + '/' + info['commit'] + '/' + urllib.parse.quote(path)
                data = request(url)
                ext = rename or Path(path).suffix.lower()
                filename = label + '-' + str(index) + ext
                target = raw / filename
                target.write_bytes(data)
                manifest['samples'].append({
                    'id': label + '-' + str(index), 'file': 'raw/' + filename,
                    'repository': repo, 'upstream_commit': info['commit'],
                    'upstream_path': path, 'git_blob_sha': entry['sha'],
                    'source_url': url,
                    'browse_url': 'https://github.com/' + repo + '/blob/' + info['commit'] + '/' + urllib.parse.quote(path),
                    'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                    'extension': ext, 'original_extension': Path(path).suffix,
                    'bytes_modified': False,
                    'extension_alias_only': bool(rename),
                    'repository_license': info['repository_license'],
                    'license_url': info['license_url'],
                })
                print(label, index, 'downloaded', len(data), path, flush=True)
        except Exception as exc:
            manifest['collection_failures'].append({'id': label, 'error': str(exc)[:500]})
            print(label, 'COLLECTION FAILED', str(exc), flush=True)
        (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'collected': len(manifest['samples']), 'collection_failures': len(manifest['collection_failures'])}))
    if not manifest['samples']:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
