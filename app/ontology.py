"""Domain validation, acyclic hierarchy checking, and standards-based RDF export."""
import re
from rdflib import Graph, Namespace, URIRef, Literal
from rdflib.namespace import RDF,RDFS,OWL,XSD
from urllib.parse import quote
from .ontology_abstraction import instance_reason
from .naming import code_error,relation_name,naming_errors

DATATYPES={'string':XSD.string,'integer':XSD.integer,'decimal':XSD.decimal,'double':XSD.double,'boolean':XSD.boolean,'date':XSD.date}
PREDICATES={'subClassOf','hasPart','partOf','relatedTo','hasComponent','locatedAt','connectsTo','dependsOn'}


class InvalidOntology(ValueError):
    pass


def validate_payload(kind, payload, snapshot):
    """Validate an auditable add/update/delete request, never mutate the snapshot."""
    if not isinstance(payload, dict):
        raise InvalidOntology('候选内容必须是 JSON 对象。')
    collection = {'entity': 'entities', 'attribute': 'attributes', 'relation': 'relations'}.get(kind)
    if collection is None:
        raise InvalidOntology('不支持的候选类型。')
    operation = payload.get('operation', 'add')
    if operation not in ('add', 'update', 'delete'):
        raise InvalidOntology('操作必须为 add、update 或 delete。')
    target_id = payload.get('record_id') if kind == 'relation' and operation != 'add' else payload.get('target_id')
    existing = next((x for x in snapshot[collection] if x['id'] == target_id), None)
    if operation != 'add' and not existing:
        raise InvalidOntology('待修改或删除的对象不存在。')
    if operation != 'add' and existing and existing.get('origin')=='openmind':
        raise InvalidOntology('这是从 OpenMind 读取的只读实体基线。不得在本地直接修改或删除；需通过专门的远端变更审核流程。')
    if operation == 'delete':
        return {'operation': 'delete', 'record_id' if kind == 'relation' else 'target_id': target_id}

    # Ignore the old version while checking an update's uniqueness and hierarchy.
    test_snapshot = dict(snapshot)
    test_snapshot[collection] = [x for x in snapshot[collection] if x['id'] != target_id] if operation == 'update' else snapshot[collection]
    es = {e['id']: e for e in snapshot['entities']}
    if kind == 'entity':
        name = str(payload.get('name', '')).strip()
        ek = payload.get('kind', 'class')
        if not 1 <= len(name) <= 90:
            raise InvalidOntology('实体名称长度必须为 1–90 字。')
        if ek != 'class':
            raise InvalidOntology('本系统只构建本体类型（class），不创建任何实例；请抽象成自然人或业务角色等类型。')
        reason=instance_reason(name)
        if reason:
            raise InvalidOntology(reason+'；请用自然人、事件类型、属性或关系定义表达。')
        class_id = None
        if operation == 'update' and existing['kind'] != 'class':
            raise InvalidOntology('既有实例记录不能作为本体类直接修改，请先核查原有数据。')
        if any(e['name'] == name and e['kind'] == ek for e in test_snapshot['entities']):
            raise InvalidOntology('已有同名、同类型的实体。')
        clean = {'name': name, 'kind': ek, 'class_id': class_id if ek == 'instance' else None,
                 'description': str(payload.get('description', ''))[:1000]}
    elif kind == 'attribute':
        owner = payload.get('owner_id')
        name = str(payload.get('name', '')).strip()
        if owner not in es:
            raise InvalidOntology('属性所属实体不存在，请先确认实体。')
        if not 1 <= len(name) <= 90:
            raise InvalidOntology('属性名称长度必须为 1–90 字。')
        if instance_reason(name):
            raise InvalidOntology('属性名称不能是具体金额、日期或人物实例；请填写通用属性名称。')
        if es[owner]['kind']!='class':
            raise InvalidOntology('属性只能定义在本体类上，不能定义到具体实例。')
        dtype = payload.get('datatype', 'string')
        if dtype not in DATATYPES:
            raise InvalidOntology('属性类型仅支持 string/integer/decimal/double/boolean/date。')
        if any(a['owner_id'] == owner and a['name'] == name for a in test_snapshot['attributes']):
            raise InvalidOntology('该实体已存在同名属性。')
        clean = {'owner_id': owner, 'name': name, 'datatype': dtype,
                 'required': bool(payload.get('required', False)),
                 'multivalued': bool(payload.get('multivalued', False)),
                 'description': str(payload.get('description', ''))[:1000]}
    else:
        source = payload.get('source_id')
        target = payload.get('target_id')
        # For old clients target_id denotes the object when no operation metadata is present.
        if operation == 'add':
            target = payload.get('target_id')
        pred = str(payload.get('predicate', '')).strip()
        if source not in es or target not in es:
            raise InvalidOntology('关系两端的实体必须先通过审批。')
        if es[source]['kind']!='class' or es[target]['kind']!='class':
            raise InvalidOntology('关系两端只能使用本体实体类型，不能连接具体实例。')
        if not re.fullmatch(r'[A-Za-z][\w-]{0,60}', pred):
            raise InvalidOntology('关系标识必须为英文字母开头且只包含字母、数字、下划线或连字符。')
        if pred in ('subClassOf', 'partOf', 'hasPart') and source == target:
            raise InvalidOntology('层级/包含关系不能指向自身。')
        if pred == 'subClassOf' and (es[source]['kind'] != 'class' or es[target]['kind'] != 'class'):
            raise InvalidOntology('subClassOf 必须连接两个本体类。')
        relations = test_snapshot['relations']
        if any(r['source_id'] == source and r['target_id'] == target and r['predicate'] == pred for r in relations):
            raise InvalidOntology('该关系已经存在。')
        if pred == 'subClassOf' and _forms_cycle(source, target, [(r['source_id'], r['target_id']) for r in relations if r['predicate'] == 'subClassOf']):
            raise InvalidOntology('该关系会使类层级形成循环。')
        if pred in ('partOf', 'hasPart'):
            edges = []
            for r in relations:
                if r['predicate'] == 'partOf':
                    edges.append((r['source_id'], r['target_id']))
                elif r['predicate'] == 'hasPart':
                    edges.append((r['target_id'], r['source_id']))
            new = (source, target) if pred == 'partOf' else (target, source)
            if _forms_cycle(*new, edges):
                raise InvalidOntology('该关系会使部件层级形成循环。')
        clean = {'source_id': source, 'target_id': target, 'predicate': pred,
                 'description': str(payload.get('description', ''))[:1000]}
    if payload.get('code') is not None:
        code=str(payload['code']).strip()
        if code_error(kind,code):raise InvalidOntology(code_error(kind,code))
        clean['code']=code
    if kind=='relation':
        clean['name']=str(payload.get('name') or relation_name(payload) or '').strip()
        if len(clean['name'])>255:raise InvalidOntology('关系名称最长255字符。')
    if operation == 'update':
        clean.update({'operation': 'update', 'record_id' if kind == 'relation' else 'target_id': target_id})
    return clean

def _forms_cycle(source,target,edges):
    reachable={source}; stack=[target]
    children={}
    for s,t in edges: children.setdefault(s,[]).append(t)
    seen=set()
    while stack:
        node=stack.pop()
        if node in reachable: return True
        if node in seen: continue
        seen.add(node);stack.extend(children.get(node,[]))
    return False


def build_graph(snapshot):
    """Class-level object links become OWL existential restrictions, not invalid individual assertions."""
    project=snapshot['project']; base=f'urn:ontology:{project["id"]}:'
    N=Namespace(base);g=Graph()
    g.bind('ont',N);g.bind('owl',OWL);g.bind('rdfs',RDFS);g.bind('xsd',XSD)
    uri=lambda item_id: URIRef(base+quote(item_id,safe=''))
    ontology=URIRef(base+'ontology');g.add((ontology,RDF.type,OWL.Ontology))
    g.add((ontology,RDFS.label,Literal(project['name'])))
    if project.get('scope'):g.add((ontology,RDFS.comment,Literal(project['scope'])))
    es={e['id']:e for e in snapshot['entities']}
    for e in es.values():
        subject=uri(e['id']);is_class=e['kind']=='class'
        g.add((subject,RDF.type,OWL.Class if is_class else OWL.NamedIndividual))
        g.add((subject,RDFS.label,Literal(e['name'])))
        if e['description']:g.add((subject,RDFS.comment,Literal(e['description'])))
        if not is_class and e.get('class_id') in es:
            g.add((subject,RDF.type,uri(e['class_id'])))
    for a in snapshot['attributes']:
        # Stable per-owner property URI; prevents unrelated attributes of same name being conflated.
        prop=URIRef(base+'attribute/'+quote(a['owner_id'],safe='')+'/'+quote(a['name'],safe=''))
        g.add((prop,RDF.type,OWL.DatatypeProperty))
        g.add((prop,RDFS.label,Literal(a['name'])))
        if es[a['owner_id']]['kind']=='class':
            g.add((prop,RDFS.domain,uri(a['owner_id'])))
        elif es[a['owner_id']].get('class_id') in es:
            g.add((prop,RDFS.domain,uri(es[a['owner_id']]['class_id'])))
        g.add((prop,RDFS.range,DATATYPES[a['datatype']]))
        if a['description']:g.add((prop,RDFS.comment,Literal(a['description'])))
        # required/multivalued are application-level constraints, not asserted as OWL axioms.
    for r in snapshot['relations']:
        source=uri(r['source_id']);target=uri(r['target_id']);pred=r['predicate']
        if pred=='subClassOf':g.add((source,RDFS.subClassOf,target));continue
        prop=URIRef(base+'relation/'+quote(pred,safe=''))
        g.add((prop,RDF.type,OWL.ObjectProperty))
        g.add((prop,RDFS.label,Literal(relation_name(r) or pred)))
        if es[r['source_id']]['kind']=='class' and es[r['target_id']]['kind']=='class':
            # OWL open-world semantics: a source class has some members in target class via property.
            restriction=URIRef(base+'restriction/'+r['id'])
            g.add((restriction,RDF.type,OWL.Restriction))
            g.add((restriction,OWL.onProperty,prop))
            g.add((restriction,OWL.someValuesFrom,target))
            g.add((source,RDFS.subClassOf,restriction))
        elif es[r['source_id']]['kind']=='instance' and es[r['target_id']]['kind']=='instance':
            g.add((source,prop,target))
        else:
            # Mixed class-instance assertions require separate semantic modeling; preserve as annotation.
            annotation=URIRef(base+'annotation/'+r['id'])
            g.add((annotation,RDF.type,RDF.Statement))
            g.add((annotation,RDF.subject,source));g.add((annotation,RDF.predicate,prop));g.add((annotation,RDF.object,target))
    return g


def serialize_ontology(snapshot, format='ttl'):
    formats = {'ttl': 'turtle', 'owl': 'xml', 'jsonld': 'json-ld'}
    if format not in formats:
        raise InvalidOntology('仅支持 ttl、owl、jsonld 导出。')
    return build_graph(snapshot).serialize(format=formats[format])


def turtle(snapshot):
    return serialize_ontology(snapshot, 'ttl')


def check_ontology(snapshot):
    issues=[]
    for kind,field in (('entity','entities'),('attribute','attributes'),('relation','relations')):
        for row in snapshot[field]:
            if 'code' not in row or row.get('origin')=='openmind':continue
            for reason in naming_errors(kind,row):issues.append(f'{row.get("name") or row.get("predicate") or row["id"]}：{reason}')
    es={e['id']:e for e in snapshot['entities']}
    for e in snapshot['entities']:
        if e['kind']!='class' or instance_reason(e.get('name','')):
            issues.append(f'实体「{e.get("name", "")[:45]}」不是合法的本体类型（存在实例或具体事实值），需人工清理。')
    for r in snapshot['relations']:
        if r['source_id'] not in es or r['target_id'] not in es:
            issues.append(f'关系 {r["id"]} 引用了不存在的实体。')
    for a in snapshot['attributes']:
        if a['owner_id'] not in es:issues.append(f'属性 {a["id"]} 所属实体不存在。')
    for pred in ('subClassOf','partOf'):
        edges=[]
        for r in snapshot['relations']:
            if r['predicate']==pred:
                edges.append((r['source_id'],r['target_id']))
            elif pred=='partOf' and r['predicate']=='hasPart':
                edges.append((r['target_id'],r['source_id']))
        for idx,(s,t) in enumerate(edges):
            if _forms_cycle(s,t,edges[:idx]+edges[idx+1:]):
                issues.append(pred+' 层级存在循环。');break
    return {'valid':not issues,'issues':issues,'counts':{
        'entities':len(snapshot['entities']),'attributes':len(snapshot['attributes']),'relations':len(snapshot['relations'])}}
