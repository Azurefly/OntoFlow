"""Replay the downloaded business corpus against its frozen SHA256 manifest.

Requires project dev dependencies, requests, beautifulsoup4 and pdftotext.
Only source provenance enters the runner; historic test results are not inputs.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from scripts import audit_business_documents as audit


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--manifest', required=True)
    options, remaining = parser.parse_known_args()
    manifest = json.loads(Path(options.manifest).read_text(encoding='utf-8'))
    if manifest.get('frozen_application_commit') != audit.BASE_COMMIT:
        raise SystemExit('Manifest/application baseline mismatch; choose a matching frozen test version.')
    allowed = ('id', 'file', 'title', 'domain', 'url', 'landing', 'sha256', 'bytes', 'anchors')
    samples = [{key: item[key] for key in allowed if key in item}
               for item in manifest.get('samples', []) if item.get('status') == 'downloaded']
    if not samples:
        raise SystemExit('No downloaded source records in this manifest.')
    for item in samples:
        if not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', ''))):
            raise SystemExit('Every replay source requires the original SHA256.')
        if Path(item['file']).name != item['file'] or item['file'] in ('.', '..'):
            raise SystemExit('Unsafe fixture filename.')
        item.setdefault('anchors', [])
        item.setdefault('domain', '')
    original_download = audit.download
    changed = []

    def checked_download(sample, output):
        record = original_download(sample, output)
        if record.get('status') == 'downloaded':
            if record['sha256'] != sample['sha256'] or record['bytes'] != sample['bytes']:
                changed.append(sample['id'])
                record.update(status='source_changed', expected_sha256=sample['sha256'],
                              expected_bytes=sample['bytes'],
                              error='Upstream bytes differ; the changed file was not tested.')
                (output / sample['file']).unlink(missing_ok=True)
        return record

    audit.SAMPLES = samples
    audit.download = checked_download
    sys.argv = [sys.argv[0]] + remaining
    audit.main()
    if changed:
        raise SystemExit('Source drift detected, see manifest.json: ' + ', '.join(changed))


if __name__ == '__main__':
    main()
