"""Fetch the frozen *public* source corpus and verify original byte identities.

No application output or hard-coded ontology count chooses/changes input bytes.
Raw upstream assets are downloaded locally, never checked into Git.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def fetch(url: str, maximum: int = 2_000_000) -> bytes:
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != 'raw.githubusercontent.com':
        raise ValueError('Expected a pinned public GitHub raw HTTPS URL')
    error = None
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={'User-Agent':'OntoFlow-corpus-replay/1'}), timeout=40) as response:
                if urlsplit(response.geturl()).hostname != 'raw.githubusercontent.com':
                    raise ValueError('Unexpected redirect host')
                content = response.read(maximum + 1)
                if len(content) > maximum:
                    raise ValueError('Fixture exceeds maximum bytes')
                return content
        except Exception as exc:
            error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(str(error))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for item in manifest['samples']:
        try:
            destination = (output / item['file']).resolve()
            if output not in destination.parents:
                raise ValueError('Unsafe manifest path')
            data = fetch(item['source_url'])
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('SHA256 mismatch; do not substitute changed upstream file')
            if hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != item['git_blob_sha']:
                raise ValueError('Git blob mismatch')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            records.append({'id':item['id'], 'status':'verified', 'bytes':len(data)})
            print(item['id'], 'verified', len(data), flush=True)
        except Exception as exc:
            records.append({'id':item['id'], 'status':'failed', 'error':str(exc)[:500]})
            print(item['id'], 'FAILED', str(exc), flush=True)
    (output/'download-results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if any(x['status'] != 'verified' for x in records):
        raise SystemExit('Corpus incomplete; inspect download-results.json. No synthetic replacement was used.')


if __name__ == '__main__':
    main()
