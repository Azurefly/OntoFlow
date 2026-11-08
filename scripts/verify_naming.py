"""Optional real Markdown name/code verification without remote writes or LLM.

Run python -m scripts.verify_naming --file /path/to/document.md
The second preview explicitly simulates proposed new entity codes for detected
cross-kind source collisions; it is NOT an approval or a migration of real data.
"""
from pathlib import Path
from collections import Counter,defaultdict
from contextlib import redirect_stdout
import argparse,io,json,uuid
from app.extraction import extract
from app.structured_discovery import deterministic_discover
from app.openmind import make_plan
from app.naming import is_chinese_name,code_error,relation_name
from app.schema_catalog import build_attribute_catalog


def run(file):
    file=Path(file);parsed=extract(file)
    documents=[{'filename':file.name,'chunks':parsed['chunks']}]
    catalog=build_attribute_catalog(documents)
    raw={'entities':[],'attributes':[],'relations':[]}
    for chunk in parsed['chunks']:
        if chunk.get('kind')=='heading':continue
        result=deterministic_discover({'content':chunk['text'],'kind':chunk.get('kind'),'section':chunk.get('section')})
        for key in raw:
            for obj in result[key]:
                raw[key].append((obj,chunk))
    def ident(kind,key):return str(uuid.uuid5(uuid.NAMESPACE_URL,f'offline-naming:{kind}:{key}'))
    es={}
    for e,c in raw['entities']:
        es[e['name']]={'id':ident('entity',e['name']),'name':e['name'],'code':e['code'],'kind':'class','description':e.get('description','')}
    attrs=[];rels=[]
    for a,c in raw['attributes']:
        attrs.append({'id':ident('attribute',a['owner']+':'+a['code']),'owner_id':ident('entity',a['owner']),
          'name':a['name'],'datatype':a['datatype'],'description':a.get('description',''),'required':False,'multivalued':False,
          'evidence':json.dumps([{'document_id':'offline','locator':c['locator'],'quote':a['quote'],'verified':True,
               'source_name':a['name'],'source_code':a['code']}],ensure_ascii=False)})
    for a,c in raw['relations']:
        rels.append({'id':ident('relation',a['predicate']),'source_id':ident('entity',a['source']),
          'target_id':ident('entity',a['target']),'predicate':a['predicate'],'name':relation_name(a),
          'code':a['code'],'description':a.get('description','')})
    snap={'project':{'revision':1},'entities':list(es.values()),'attributes':attrs,'relations':rels}
    params=('offline',ident('domain','test'),ident('branch','test'))
    raw_plan=make_plan(snap,*params)
    allowed={'string','integer','double','date','boolean'}
    compatible=[a for a in attrs if a['datatype'] in allowed]
    occupied={k['code'] for k in catalog['definitions'].values() if k['datatype'] in allowed}
    proposed={('entity',x['id']):'entity_'+x['code'] for x in es.values() if x['code'] in occupied}
    reviewed=make_plan({**snap,'attributes':compatible},*params,overrides=proposed)
    objects=[x['data'] for x in reviewed['steps']]
    return {'source':file.name,'execution':'Offline, no LLM, no PG, no remote writes',
      'parsed':{'entities':len(es),'independent_attributes':len(catalog['definitions']),
                'attribute_links':len(attrs),'relations':len(rels)},
      'raw_preview':{'ready':raw_plan['ready'],'blockers':raw_plan['blockers']},
      'simulated_user_choices':{'excluded_unsupported_attribute_links':len(attrs)-len(compatible),
          'proposed_entity_codes':sorted(proposed.values()),
          'note':'This preview assumes the user explicitly approves these naming choices; nothing is applied.'},
      'after_explicit_choices':{'ready':reviewed['ready'],'blockers':reviewed['blockers'],
        'counts':reviewed['counts'],'all_names_have_chinese':all(is_chinese_name(x['name']) for x in objects),
        'all_codes_are_valid_ascii':all(not code_error(s['kind'],s['data']['code']) for s in reviewed['steps']),
        'codes_unique':len(objects)==len({x['code'].casefold() for x in objects})}}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--file',required=True)
    print(json.dumps(run(p.parse_args().file),ensure_ascii=False,indent=2))
