"""One authoritative application release identifier."""
VERSION = '1.4.1-demo'
SCHEMA_VERSION = '1.4.0'


def build_identity():
    """Release-build evidence, or honest source-tree fallback; no secrets."""
    import json
    from pathlib import Path
    try:
        data=json.loads(Path(__file__).with_name('build_manifest.json').read_text())
        return data['build_id']
    except (OSError,ValueError,KeyError):
        return 'source-tree-unpackaged'
