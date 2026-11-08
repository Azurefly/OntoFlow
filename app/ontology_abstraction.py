"""Ontology-schema-only abstraction from source narratives and definition documents.

Instance mentions are evidence, not entities stored in the ontology. The rules here
are deliberately conservative: ambiguous names/roles are held for clarification.
No external NLP model is needed for simple, explicit case facts.
"""
from __future__ import annotations

import re
from typing import Any

ABSTRACTION_VERSION = 'schema-only-abstraction-v2-attribution'

# Covers anonymized Chinese names commonly found in reports. It is NOT a general
# named-entity recognizer; an LLM may find further people, but the schema output
# still must pass a no-instance gate.
_SURNAMES = '赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄穆萧尹姚邵汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫房裘缪解应宗丁宣邓单洪包诸左石崔吉龚程嵇邢滑裴陆荣翁荀羊惠甄曲封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公'
_PERSON_ANON = re.compile(r'[' + _SURNAMES + r']某{1,2}(?:先生|女士)?|[' + _SURNAMES + r'](?:先生|女士)')
_EXAMPLE_NAMES = {'张三', '李四', '王五', '赵六'}
_GIVEN_NAME_END = set('伟磊明杰华丽芳强军刚敏勇雪鹏涛婷娜浩亮静颖鑫洋超倩慧博诚')
_PERSON_ROLE = re.compile(r'报警人|报案人|驾驶人|被害人|受害人|当事人|证人|嫌疑人|申请人|审核人|自然人|人员|公民')
_REPORT_ACTION = re.compile(r'报警|报案|拨打\s*110|拨打\s*报警电话|向(?:公安|警方|派出所)\w{0,6}(?:报警|报案)')
_NARRATIVE_ACTION = re.compile(r'发生争执|发生口角|争执|争吵|打架|报警|报案|拨打110|转账|汇款|驾驶|离开现场|案发|接警|受伤|盗窃')
_AMOUNT = re.compile(r'(?:人民币|金额|转账|汇款|支付|收到)?\s*(?:¥|￥)?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:元|万元|人民币|RMB|CNY)', re.I)
_DATE = re.compile(r'(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?(?:\s*\d{1,2}(?:[:：时]\d{1,2})?(?:分|许)?)?')
_CONCRETE_LOCATION = re.compile(r'^(?:某|该|本|这座|这家)[^，。]{1,16}?(?:商场|门口|小区|路口|医院|学校|公园|街道|广场|宾馆|车站)(?:门口|附近|内|前|旁)?$')
_CASE_ID = re.compile(r'^(?:(?:案件|警情|案号|编号|case\s*id|案件编号)\s*[:：#-]\s*\S{2,}|(?:案件编号|警情编号|案号)\s+[0-9A-Z_-]{3,})$', re.I)
_VEHICLE = re.compile(r'电动车|摩托车|自行车|小汽车|轿车|出租车|机动车|汽车|面包车|三轮车|货车')
_PLACE = re.compile(r'(?:在|于|位于|抵达)([^，。；;\n]{1,25}?(?:商场|小区|路口|门口|车站|学校|医院|公园|路段|街道|广场|派出所)(?:门口|附近|内|前|旁)?)')


def named_people(text: str) -> list[str]:
    """Conservative high-precision anonymized mentions, not an instance index."""
    text = str(text or '')
    names=set()
    for match in _PERSON_ANON.finditer(text):
        value=match.group();after=text[match.end():match.end()+1];before=text[max(0,match.start()-2):match.start()]
        # ‘属于某种流程’/‘向某些对象’ are not anonymized persons.
        if value.endswith('某') and (after in ('种','个','类','些','项','处','地') or
                (value.startswith('于') and before.endswith(('属','关','位','对')))):
            continue
        names.add(value)
    names.update(x for x in _EXAMPLE_NAMES if x in text)
    # Real-name hints are weaker than anonymized XX某. Require a common
    # given-name suffix and an adjacent action; never infer arbitrary Chinese
    # 2-3 character nouns are people.
    for index,char in enumerate(text):
        if char not in _SURNAMES or char in '向于在和同给被将':
            continue
        for length in (3,2):
            candidate=text[index:index+length]
            if (len(candidate)==length and candidate[-1] in _GIVEN_NAME_END and
                re.fullmatch(r'[\u4e00-\u9fff]{2,3}',candidate) and
                re.search(r'^(?:向|给|在|与|和|同|报警|报案|驾驶|转账|称|说|涉嫌|参与|发生)',
                          text[index+length:index+length+4])):
                names.add(candidate)
                break
    return sorted(names, key=lambda x: (text.find(x), x))


def instance_reason(value: Any, source: str = '') -> str:
    """Reject identifiable individual/value names from schema definitions.

    Uncertain real names should be left for user clarification; this function
    intentionally does not guess from every two-character Chinese class name.
    """
    value = str(value or '').strip()
    if not value:
        return ''
    if _PERSON_ANON.fullmatch(value) or value in _EXAMPLE_NAMES:
        return '具体人名不能作为本体实体类型'
    if (2 <= len(value) <= 3 and value[0] in _SURNAMES and value[-1] in _GIVEN_NAME_END
            and re.fullmatch(r'[\u4e00-\u9fff]{2,3}', value)):
        return '疑似具体人名，不能直接作为本体实体类型'
    if _CONCRETE_LOCATION.fullmatch(value):
        return '具体地点不能作为本体实体类型'
    if _DATE.fullmatch(value) or _AMOUNT.fullmatch(value):
        return '日期或金额属于具体事实值，不能作为本体定义名称'
    if _CASE_ID.fullmatch(value) or re.fullmatch(r'(?:CASE|ALARM|AJ|JQ)[-_]?[0-9A-Z]{4,}', value, re.I):
        return '具体案件、警情或记录编号不能作为本体实体类型'
    if re.fullmatch(r'(?:\d+(?:\.\d+)?\s*(?:元|万元|人|次|辆)|[\d-]{8,})', value):
        return '数量或标识符是实例值，不是本体概念'
    return ''


def abstract_reference(value: Any) -> str:
    """Turn definite person references into a class reference, never an ID."""
    value = str(value or '').strip()
    if (_PERSON_ANON.fullmatch(value) or value in _EXAMPLE_NAMES or
        (2 <= len(value) <= 3 and value[0] in _SURNAMES and value[-1] in _GIVEN_NAME_END
         and re.fullmatch(r'[\u4e00-\u9fff]{2,3}', value))):
        return '自然人'
    if _CONCRETE_LOCATION.fullmatch(value):
        return '地点'
    return value


def _snippet(text: str, match: re.Match, pad: int = 22) -> str:
    start, end = match.span()
    left = max(0, text.rfind('。', 0, start) + 1, text.rfind('；', 0, start) + 1, start-pad)
    right = min(len(text), end+pad)
    for character in '。；\n':
        pos = text.find(character, end, right)
        if pos >= 0:
            right = min(right, pos+1)
    return text[left:right].strip()


def _clue_for(entities: list, name: str, quote: str, description: str):
    if quote and not any(x.get('name') == name for x in entities):
        entities.append({'name': name, 'description': description, 'quote': quote})


# The clause parser operates only on the transient source; the resulting schema
# candidates never contain a person's record ID, identity, or a case instance.
_CLAUSE_BOUNDARY = re.compile(r'[。！？；;\n，,]')
_ACTION = re.compile(r'报警(?!人|电话)|报案(?!人)|拨打\s*(?:110|报警电话)|驾驶(?!人|员|证)|驾车|开车')
_NEGATION = re.compile(r'(?:未曾|并未|没有|尚未|未|没|不曾|否认|拒绝|不是|并非|不打算|不准备|未向|没有向)(?:[^，。！？；;\n]{0,10})$')
_ROLE_DESCRIPTION = {
    '报警人': '发起报警行为的自然人业务角色（待确定是否单独建类）。',
    '报案人': '实施报案行为的自然人业务角色（待确定是否单独建类）。',
    '驾驶人': '实施驾驶行为的自然人业务角色（待确定是否单独建类）。',
}
_ROLE_BY_ACTION = {'报警': '报警人', '报案': '报案人', '拨打': '报警人',
                   '驾驶': '驾驶人', '驾车': '驾驶人', '开车': '驾驶人'}


def _clauses(text: str) -> list[dict]:
    """Exact source spans; never fabricate or normalize a quoted sentence."""
    out=[];start=0
    for boundary in _CLAUSE_BOUNDARY.finditer(text):
        end=boundary.end()
        if text[start:end].strip():
            out.append({'text':text[start:end], 'start':start, 'end':end})
        start=end
    if text[start:].strip():
        out.append({'text':text[start:], 'start':start, 'end':len(text)})
    return out


def _negated_action(clause: str, action_start: int) -> bool:
    # The context is bounded by the nearest clause separator. A denial cannot
    # be evidence of an affirmative event or business role.
    before=clause[:action_start].rstrip()
    return bool(_NEGATION.search(before))


def _is_subject_link(between: str) -> bool:
    """Permit short adverbial/prepositional bridges, not reported speech."""
    between=re.sub(r'\s+', '', between)
    if len(between)>22:
        return False
    if re.search(r'称|说|表示|反映|指认|听说|猜测|推测|通知|告诉|告知|要求|建议|让|请|否认|拒绝|未|没|不|被|怀疑|准备|计划|想要|欲', between):
        return False
    if not between:
        return True
    # "李某随后报警", "李某向派出所报警" etc. If the span has an
    # unrecognised actor/verb, ask instead of attaching a role to 李某.
    return bool(re.fullmatch(
        r'(?:随后|之后|随即|立即|主动|当即|直接|已经|再次|又|先|独自|就|紧接着|'
        r'向(?:警方|公安|公安机关|派出所|民警)|在(?:当地)?(?:派出所|警局)|'
        r'到(?:了)?(?:派出所|警局)|来到(?:派出所|警局))*',between))


def _subject_for_action(clause: str, action_start: int, people: list[str]):
    """Return (person, explicit) for an attributable subject, else (None, False)."""
    before=clause[:action_start]
    matches=[]
    for person in people:
        for found in re.finditer(re.escape(person),before):
            if _is_subject_link(before[found.end():]):
                matches.append((person,found.start()))
    # Another named person in the bridge means the leading person cannot be
    # assumed to be the actor (张某称李某... is especially dangerous).
    if matches:
        matches.sort(key=lambda item:item[1],reverse=True)
        person,pos=matches[0]
        if any(other!=person and other in before[pos+len(person):] for other in people):
            return None,False
        return person,True
    if re.search(r'(?:^|\s)(?:随后|之后|随即|于是|当即|紧接着|又)?\s*'
                 r'(?:其|该人|本人|他|她)(?:立即|主动|随后|就|又|直接)?\s*$',before):
        return 'PRONOUN',False
    return None,False


def _attributed_actions(text: str, people: list[str]):
    """Ground explicit or uniquely co-referenced actions, flag ambiguous pronouns.

    Cross-clause coreference only uses the *immediately previous* clause with
    exactly one explicitly detected person. This deliberately favors abstention.
    """
    clauses=_clauses(text);attributed=[];questions=[]
    for index,clause in enumerate(clauses):
        fragment=clause['text']
        for action in _ACTION.finditer(fragment):
            if _negated_action(fragment,action.start()):
                continue
            subject,explicit=_subject_for_action(fragment,action.start(),people)
            if subject=='PRONOUN':
                previous=clauses[index-1] if index else None
                previous_people=([p for p in people if p in previous['text']]
                                 if previous else [])
                if len(previous_people)==1:
                    subject=previous_people[0]
                    quote=text[previous['start']:clause['end']].strip()
                    by_coreference=True
                else:
                    questions.append({'source_excerpt':fragment.strip()[:160],
                        'reason':'跨句「其/他/她」指代不明确，无法确定业务角色承担者；请确认指向哪位人员，当前不生成该角色', 'category':'coreference_ambiguity'})
                    continue
            elif subject:
                quote=fragment.strip()
                by_coreference=False
            else:
                continue
            attributed.append({'subject':subject,'action':action.group(),
                               'action_start':clause['start']+action.start(),
                               'quote':quote,'coreference':by_coreference,
                               'clause':fragment,'clause_start':clause['start']})
    return attributed,questions


def _affirmative_hits(text: str, pattern: str) -> list[re.Match]:
    hits=[]
    for match in re.finditer(pattern,text):
        previous=max(text.rfind(mark,0,match.start()) for mark in '，,。！？；;\n')
        if not _negated_action(text[previous+1:match.end()],match.start()-previous-1):
            hits.append(match)
    return hits


def discover_narrative_types(text: str) -> dict[str, Any]:
    """Evidence-based *schema* discovery from case prose; never emit instances.

    Named actors and pronouns are used only as temporary evidence anchors.
    Business roles are optional type proposals, with explicit questions about
    subclass vs event-role modeling. Ambiguous references never pick a person.
    """
    result={'entities':[], 'attributes':[], 'relations':[], '_unresolved':[],
            '_coverage':{'narrative_type_clues':0,'case_facts_not_stored':0,
                         'attributed_roles':0,'ambiguous_coreferences':0}}
    text=str(text or '')
    people=named_people(text)
    if not people and not _NARRATIVE_ACTION.search(text):
        return result
    if people:
        first=re.search('|'.join(re.escape(p) for p in people),text)
        _clue_for(result['entities'],'自然人',first.group(),
                  '能够参与业务事件、承担业务角色的自然人类型。')
        result['_coverage']['case_facts_not_stored']+=len(people)
    for pattern,name,description in [
        (r'发生争执|争执|发生口角|争吵','争执事件','自然人之间发生争执或口角的事件类型。'),
        (r'转账(?!金额|字段|属性)|汇款(?!金额|字段|属性)','转账事件','资金转移行为的事件类型。'),
        (r'报警(?!人|电话)|报案(?!人)|拨打\s*110','报警事件','向警方报警或报案的事件类型。')]:
        hits=_affirmative_hits(text,pattern)
        if hits:
            _clue_for(result['entities'],name,hits[0].group(),description)
    vehicle=_VEHICLE.search(text)
    if vehicle:
        _clue_for(result['entities'],'交通工具',vehicle.group(),'用于出行或运输的工具类型。')
    place=_PLACE.search(text)
    if place:
        _clue_for(result['entities'],'地点',place.group(),'业务事件发生或关联的空间位置类型。')

    actions,questions=_attributed_actions(text,people)
    # Some business narratives explicitly declare the role instead of using a
    # verb: "李某是报警人". This supports the role TYPE but does not by itself
    # prove the occurrence of a separate incident event.
    explicit_roles=[]
    if people:
        person_names='|'.join(re.escape(person) for person in people)
        for regex in (
            r'(?P<person>'+person_names+r')\s*(?:系|是|为|被确认为)\s*(?P<role>报警人|报案人|驾驶人)',
            r'(?P<role>报警人|报案人|驾驶人)\s*(?:为|是|[:：])\s*(?P<person>'+person_names+r')',
        ):
            for match in re.finditer(regex,text):
                explicit_roles.append((match.group('role'),match.group(),match.start()))
    result['_unresolved'].extend(questions)
    result['_coverage']['ambiguous_coreferences']=len(questions)
    known_relations=set()
    def relation(src,predicate,dst,quote,description):
        key=(src,predicate,dst)
        if key not in known_relations and quote and quote in text:
            result['relations'].append({'source':src,'predicate':predicate,
                'target':dst,'quote':quote,'description':description})
            known_relations.add(key)
    for role,quote,_ in explicit_roles:
        _clue_for(result['entities'],role,quote,_ROLE_DESCRIPTION[role])
        result['_unresolved'].append({'source_excerpt':quote[:160],
             'reason':f'原文明确出现「{role}」角色称谓。请决定建立自然人子类还是事件角色；不会自动确定法律身份。',
             'category':'modeling_choice','blocks_coverage':False})
    for action in actions:
        verb=action['action']
        role=next((v for key,v in _ROLE_BY_ACTION.items() if verb.startswith(key)),None)
        if not role:
            continue
        _clue_for(result['entities'],role,action['quote'],_ROLE_DESCRIPTION[role])
        result['_coverage']['attributed_roles']+=1
        question=f'「{role}」应单独建立自然人的子类，还是表示自然人在相应事件中的事件角色？请人工选择；不自动建立继承。'
        if action['coreference']:
            question='前后句通过唯一明确人员归属推定指代，仍需用户核查；'+question
        result['_unresolved'].append({'source_excerpt':action['quote'][:160],
                                      'reason':question, 'blocks_coverage':False, 'category':'modeling_choice'})
        if role in ('报警人','报案人'):
            relation('自然人','reportsIncident','报警事件',action['quote'],
                     '自然人发起报警或报案事件（参与角色待确认）。')
        elif role=='驾驶人' and _VEHICLE.search(action['clause']):
            relation('自然人','drives','交通工具',action['quote'],
                     '自然人在该类行为中驾驶交通工具。')

    # An amount elsewhere in the report is not evidence of a transfer amount.
    # Only a positive transfer and amount in the same clause can support it.
    for clause in _clauses(text):
        if not _affirmative_hits(clause['text'],r'转账(?!金额|字段|属性)|汇款(?!金额|字段|属性)'):
            continue
        amount=_AMOUNT.search(clause['text'])
        if amount and not re.search(r'手续费|赔偿费|罚款|利息', clause['text'][:amount.start()]):
            result['attributes'].append({'owner':'转账事件','name':'转账金额',
                'datatype':'double','description':'转账事件的金额数值，不保存原文具体数额。',
                'quote':amount.group()})
            result['_coverage']['case_facts_not_stored']+=1
            break
    # Relationship types require positive, attributable facts. Do not treat
    # "张某称李某向王某转账" as a transfer by 张某, or "否认转账" as a transfer.
    if people:
        names='|'.join(re.escape(p) for p in people)
        dispute=re.search(r'('+names+r')[^。；\n]{0,30}?(?:与|和|同)('+names+r')'
                          r'[^。；\n]{0,15}?(?:发生争执|争执|发生口角|争吵)',text)
        if dispute and _affirmative_hits(dispute.group(),r'发生争执|争执|发生口角|争吵'):
            relation('自然人','participatesIn','争执事件',dispute.group(),
                     '自然人参与争执事件。')
        for clause in _clauses(text):
            fragment=clause['text']
            send=re.search(r'('+names+r')\s*(?:直接|主动|随后|立即|当即|又)?'
                           r'\s*(?:向|给)('+names+r')\s*(?:转账|汇款)',fragment)
            if send and _affirmative_hits(fragment,r'转账|汇款') and not _negated_action(fragment,send.start()):
                if re.search(r'称|说|表示|反映|听说|据.{0,5}称',fragment[:send.start()]):
                    result['_unresolved'].append({'source_excerpt':fragment[:160],
                        'reason':'这段转账描述属于他人转述或陈述；它只能用于发现本体关系类型，不能据此认定具体案件事实。请人工核对语义和来源。',
                        'category':'reported_fact','blocks_coverage':False})
                relation('自然人','initiatesTransfer','转账事件',send.group(),
                         '自然人发起转账事件。')
                relation('转账事件','hasRecipient','自然人',send.group(),
                         '转账事件的接收方是自然人。')
    result['_coverage']['narrative_type_clues']=sum(len(result[k]) for k in ('entities','attributes','relations'))
    return result


def _role_grounded(role: str, quote: str) -> bool:
    """A quoted negation is not positive evidence for a case role."""
    patterns = {
        '报警人': r'报警人|报警|报案|拨打\s*110',
        '报案人': r'报案人|报案',
        '驾驶人': r'驾驶|驾车|开车',
        '嫌疑人': r'嫌疑人|犯罪嫌疑人',
        '受害人': r'被害人|受害人',
        '被害人': r'被害人|受害人',
        '证人': r'证人',
    }
    if role not in patterns:
        if _PERSON_ROLE.fullmatch(role) and role not in ('自然人','人员','公民'):
            patterns[role]=re.escape(role)
        else:
            return True
    for match in re.finditer(patterns[role], quote):
        previous=max(quote.rfind(mark,0,match.start()) for mark in '，,。！？；;\n')
        if not _negated_action(quote[previous+1:match.end()],match.start()-previous-1):
            return True
    return False


def normalize_clues(clues: dict, text: str) -> dict:
    """Last schema-only boundary for BOTH rules and model-generated clues.

    Reject specific values; safely generalize explicit anonymized person mentions.
    Nothing unsafe is silently presented as an approved ontology definition.
    """
    result = {k: [] for k in ('entities', 'attributes', 'relations')}
    result['_unresolved'] = list(clues.get('_unresolved', []))
    result['_coverage'] = dict(clues.get('_coverage', {}))
    result['_semantic_text'] = str(clues.get('_semantic_text', ''))
    seen = set()
    generalized = 0
    discarded = 0
    for category in ('entities', 'attributes', 'relations'):
        for raw in clues.get(category, []):
            if not isinstance(raw, dict):
                discarded += 1
                result['_unresolved'].append({'source_excerpt': str(raw)[:160],
                                             'reason': '模型返回了无效的本体线索'});
                continue
            item = dict(raw)
            quote = str(item.get('quote') or '').strip()
            if not quote or quote not in text:
                discarded += 1
                result['_unresolved'].append({'source_excerpt': quote[:160],
                                             'reason': '模型/规则线索无法在原文定位证据'});
                continue
            if category == 'entities':
                name = str(item.get('name') or '').strip()
                if instance_reason(name, text):
                    if abstract_reference(name) in ('自然人', '地点'):
                        item['name'] = abstract_reference(name)
                        item['description'] = ('由原文具体人员线索归纳的自然人类型。' if item['name']=='自然人'
                            else '由原文具体地点线索归纳的地点类型。')
                        generalized += 1
                    else:
                        discarded += 1
                        result['_unresolved'].append({'source_excerpt': quote[:160],
                              'reason': '候选实体是具体案件、金额、日期或编号；不生成实例，请人工决定对应本体类型'});
                        continue
                elif item.get('kind') not in (None, '', 'class'):
                    discarded += 1
                    result['_unresolved'].append({'source_excerpt': quote[:160],
                         'reason': '仅允许本体类定义，不允许实体实例；请归纳为类或角色'});
                    continue
                if not _role_grounded(name, quote):
                    discarded += 1
                    result['_unresolved'].append({'source_excerpt': quote[:160],
                           'reason': f'原文证据不能支持「{name}」业务角色，或存在否定描述；不得臆断角色，请人工确认'})
                    continue
                item['kind'] = 'class'
                identity = ('e', item.get('name', '').casefold().strip())
            elif category == 'attributes':
                name = str(item.get('name') or '').strip()
                if instance_reason(name, text):
                    discarded += 1
                    result['_unresolved'].append({'source_excerpt': quote[:160],
                         'reason': '属性名称是实例值；应归纳为通用属性定义（例如转账金额）'});
                    continue
                old_owner = str(item.get('owner') or '')
                item['owner'] = abstract_reference(old_owner)
                generalized += int(item['owner'] != old_owner)
                identity = ('a', item['owner'].casefold().strip(), name.casefold().strip())
            else:
                src, dst = str(item.get('source') or ''), str(item.get('target') or '')
                item['source'], item['target'] = abstract_reference(src), abstract_reference(dst)
                generalized += int(src != item['source']) + int(dst != item['target'])
                if instance_reason(item.get('predicate', ''), text):
                    discarded += 1
                    result['_unresolved'].append({'source_excerpt': quote[:160],
                                                 'reason': '关系标识不能使用具体实例值'});
                    continue
                identity = ('r', item['source'].casefold().strip(),
                            str(item.get('predicate') or '').casefold().strip(),
                            item['target'].casefold().strip())
            if not all(identity[1:]):
                discarded += 1
                result['_unresolved'].append({'source_excerpt': quote[:160], 'reason': '候选缺少类型、属性归属或关系端点'});
                continue
            if identity not in seen:
                # A literal person/location mention was transformed into a type.
                # Keep its quote, but do not call the transformed type an
                # explicit definition just because the input was a table.
                keys=('name',) if category=='entities' else ('owner',) if category=='attributes' else ('source','target')
                if any(str(raw.get(key) or '').strip()!=str(item.get(key) or '').strip() for key in keys):
                    item['_basis']={'kind':'inference','method':'instance_to_type_abstraction',
                        'rationale':'由原文具体对象归纳为本体类型；原文没有因此变成显式类型定义，仍需人工确认'}
                # Description is a schema explanation, never an unredacted case fact.
                description = str(item.get('description') or '')
                for person in named_people(description):
                    description = description.replace(person, '自然人')
                description = _AMOUNT.sub('具体金额', description)
                description = _DATE.sub('具体时间', description)
                item['description'] = description
                result[category].append(item)
                seen.add(identity)
    result['_coverage'].update({'generalized_instance_references': generalized,
                                 'discarded_instance_values': discarded})
    return result
