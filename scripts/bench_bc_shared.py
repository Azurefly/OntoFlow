"""Offline BC ontology-type and native shared-attribute test; no writes or LLM.

This is a synthetic approved snapshot derived from explicit Markdown definitions.
It does NOT mean these objects were human-approved in the real project.
"""
from __future__ import annotations
import argparse
import json
import uuid
from pathlib import Path
from collections import Counter
from app.extraction import extract
from app.structured_discovery import deterministic_discover
from app.schema_catalog import build_attribute_catalog, verified_source_attribute_code
from app.openmind import make_plan


def run(path):
    source=Path(path)
    chunks=extract(source)['chunks']
    docs=[{'filename':source.name,'chunks':chunks}]
    catalog=build_attribute_catalog(docs)
    entities={};attributes=[];relations=[]
    def uid(kind,key):return str(uuid.uuid5(uuid.NAMESPACE_URL,f'bc-offline:{kind}:{key}'))
    for ch in chunks:
        if ch.get('kind')=='heading':continue
        out=deterministic_discover({'content':ch['text'],'kind':ch.get('kind'),
                                    'section':ch.get('section')})
        for obj in out['entities']:
            entities[obj['name']]={'id':uid('entity',obj['name']),'name':obj['name'],
                    'kind':'class','code':obj.get('code',''),'description':obj.get('description','')}
        for obj in out['attributes']:
            attributes.append({'id':uid('attribute',obj['owner']+':'+obj['code']),
                 'owner_id':uid('entity',obj['owner']),'name':obj['name'],
                 'datatype':obj['datatype'],'description':obj.get('description',''),
                 'required':False,'multivalued':False,
                 'evidence':json.dumps([{'document_id':'offline','locator':ch['locator'],
                     'quote':obj['quote'],'verified':True}],ensure_ascii=False)})
        for obj in out['relations']:
            relations.append({'id':uid('relation',obj['predicate']),
                'source_id':uid('entity',obj['source']),
                'target_id':uid('entity',obj['target']),
                'predicate':obj['predicate'],'name':obj.get('label') or obj.get('name',''),
                'code':obj.get('code') or obj['predicate'],'description':obj.get('description','')})
    snapshot={'project':{'revision':1},'entities':list(entities.values()),
              'attributes':attributes,'relations':relations}
    full_plan=make_plan(snapshot,'bc-offline',uid('domain','demo'),uid('branch','demo'))
    unsupported=[x for x in attributes if x['datatype'] not in ('integer','double','string','date','boolean')]
    # A human could reject these unsupported fields. Preview that scenario
    # separately without ever changing real saved data.
    approved={**snapshot,'attributes':[a for a in attributes if a not in unsupported]}
    reviewed_plan=make_plan(approved,'bc-offline',uid('domain','demo'),uid('branch','demo'))
    print(json.dumps({'source':source.name,'catalog':catalog['counts'],
       'extracted':{'entities':len(entities),'per_entity_attribute_bindings':len(attributes),
                    'relation_types':len(relations),
                    'unsupported_attribute_bindings':len(unsupported),
                    'unsupported_distinct_codes':len({
                      verified_source_attribute_code(a) for a in unsupported})},
       'full_plan':{'ready':full_plan['ready'],
                    'counts':full_plan['counts'],'blockers':full_plan['blockers']},
       'without_unsupported_types':{'ready':reviewed_plan['ready'],
                    'counts':reviewed_plan['counts'],'blockers':reviewed_plan['blockers'],
                    'direct_entity_bindings':sum(len(x['data'].get('entityIds',[])) for x in reviewed_plan['steps'] if x['kind']=='attribute')},
       'note':'Offline native JSON preview only. No PostgreSQL, OpenMind API or human approval.'},ensure_ascii=False,indent=2))
    return catalog,full_plan,reviewed_plan


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--file',required=True)
    run(p.parse_args().file)
