"""Independent metamorphic regression of a BC-style source WITHOUT hardcoded answers.

No source document is bundled. Output is a JSON record of actual extraction,
reference comparisons, column permutation seeds, catalog audits and time.

    python -m scripts.verify_mutations_v135 --file /path/to/bc.md --output results.json
"""
import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from app.extraction import extract
from app.schema_catalog import build_attribute_catalog
from app.readable_overview import structured_overview
from scripts.independent_bc_audit import (
    raw_reference, production_extract, check, renumber_headings,
    swap_catalog_columns, append_valid_entity)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--file',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    source=Path(args.file).read_text(encoding='utf-8')
    variants=[('original',source),('renumbered',renumber_headings(source)),
              ('new_entity',append_valid_entity(source))]
    variants += [(f'column_seed_{seed}',swap_catalog_columns(source,seed=seed)[0])
                 for seed in range(20)]
    results=[]
    for label,variant in variants:
        started=time.perf_counter()
        oracle=raw_reference(variant)
        actual=production_extract(variant)
        compared=check(variant,oracle,actual)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.md'
            path.write_text(variant,encoding='utf-8')
            doc={'filename':'fixture.md','chunks':extract(path)['chunks']}
            catalog=build_attribute_catalog([doc])
            overview=structured_overview([doc])
        exact=all(compared[category]['exact_set_match'] for category in (
            'entities','per_entity_attributes','relations_source_target_label'))
        matched=(exact and not compared['evidence_bad_quotes'] and
                 catalog['counts']['shared_attributes']==len(oracle['shared']) and
                 catalog['counts']['direct_bindings']==len(oracle['attachments']) and
                 catalog['counts']['issues']==0 and
                 actual['counts'].get('semantic_units',0)==0)
        overview_ok=(overview is not None and
                     overview['coverage']['entity_definitions']==len(oracle['entities']) and
                     overview['coverage']['attribute_definitions']==len(oracle['shared']) and
                     overview['coverage']['relation_definitions']==len(oracle['relations']))
        results.append({'case':label,'pass':matched and overview_ok,
                        'source_sha256':hashlib.sha256(variant.encode()).hexdigest(),
                        'oracle':{'entities':len(oracle['entities']),
                                  'shared_attributes':len(oracle['shared']),
                                  'direct_bindings':len(oracle['attachments']),
                                  'relations':len(oracle['relations'])},
                        'program':{'entities':len(actual['entities']),
                                   'direct_bindings':len(actual['attributes']),
                                   'relations':len(actual['relations']),
                                   'unresolved':len(actual['gaps']),
                                   'semantic_units':actual['counts'].get('semantic_units',0)},
                        'catalog':catalog['counts'],
                        'overview_ok':overview_ok,
                        'comparison':{k:compared[k] for k in (
                            'entities','per_entity_attributes','relations_source_target_label')},
                        'bad_quotes':compared['evidence_bad_quotes'],
                        'duration_seconds':round(time.perf_counter()-started,5)})
    root=Path(__file__).resolve().parent.parent
    files=('app/structured_discovery.py','app/table_semantics.py','app/schema_catalog.py',
           'app/incremental.py','app/readable_overview.py','app/main.py','app/proposal_dedup.py')
    payload={'method':'independent raw markdown table oracle + unchanged deployed parser; '
                     'expected counts derived independently for each transformed input',
             'input_sha256':hashlib.sha256(source.encode()).hexdigest(),
             'code_sha256':{str(file):hashlib.sha256((root/file).read_bytes()).hexdigest() for file in files},
             'passed':sum(r['pass'] for r in results),'total':len(results),'cases':results,
             'limitations':[
                 'The BC source has been used in previous development; this is not an unseen-domain blind test.',
                 'Rules, synthetic API tests and the independent scanner were run offline; no real PG/OpenMind/LLM writes.',
                 'JSON attributes incompatible with the OpenMind native five scalar types remain human-review blockers.',
                 'Formatting metamorphic tests do not establish natural-language extraction recall.']}
    Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':payload['passed'],'total':payload['total'],
          'cases_failed':[r['case'] for r in results if not r['pass']]},ensure_ascii=False))
    if payload['passed']!=payload['total']:
        raise SystemExit(2)


if __name__=='__main__':main()
