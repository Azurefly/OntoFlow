"""Schema-table meaning from headers and heading *words*, never chapter numbers.

This is deliberately domain independent. A table's role comes from explicit
column semantics and its nearest heading, not its ordering within the document.
No category is allowed to fall back to a similarly named but different column.
"""
from __future__ import annotations
import re

_PREFIX=re.compile(r'^\s*(?:#{1,6}\s*)?(?:(?:第[一二三四五六七八九十百零0-9]+[章节]|\d+(?:[.．]\d+)*[.．]?)\s*)+',re.I)


def headings(section):
    parts=[]
    for part in re.split(r'\s+/\s+',str(section or '')):
        value=_PREFIX.sub('',part).strip(' ：:—-\t')
        if value:parts.append(value)
    return tuple(parts)


def _contains_any(section,*phrases):
    return any(any(phrase in part for phrase in phrases) for part in headings(section))



# Explicit semantics only: generic aliases are permitted, but competing columns
# for the same business field are ambiguous, not resolved by column position.
ATTRIBUTE_CODE=('属性编号','属性编码','属性CODE','字段编号','字段编码','attribute code')
ATTRIBUTE_NAME=('属性名称','属性名','字段名称','字段名','attribute name','attribute')
SHARED_OWNERS=('挂载实体','适用实体','挂载对象','适用对象','实体列表','关联实体')
OWNER_COUNT=('挂载实体数','适用实体数','实体数量','对象数量')
DIRECT_OWNER=('所属实体','归属实体','实体名称','归属对象','对象名称','owner')
ATTRIBUTE_TYPE=('数据类型','属性类型','字段类型','datatype','data type','type')


def column_index(header,*aliases):
    norm=lambda value:re.sub(r'[\s_\-（）()·.．：:]+','',str(value or '')).casefold()
    accepted={norm(value) for value in aliases}
    matches=[i for i,value in enumerate(header) if norm(value) in accepted]
    return matches[0] if len(matches)==1 else -1


def is_background(section):
    """Background remains searchable, but is not itself ontology schema prose.

    A *well-formed definition table* inside one of these chapters is still
    detected by the main parser before this suppression is consulted.
    """
    return _contains_any(section,'原子能力','能力域','能力说明','取值SQL','SQL取值',
                         '质量校验','修订说明','修订记录','版本变更','执行SQL')


def is_entity_attachment_index(section,header):
    """Count-only entity index, not a fresh class definition table."""
    h={str(x).strip() for x in header}
    if not {'实体名称','实体编号'} <=h:
        return False
    counts={'属性数','属性数量','属性个数','属性项数','字段数量','字段数','属性总数','字段总数'}
    if h & counts and ( _contains_any(section,'实体属性','实体字段','属性挂载','字段挂载') or
                        not h.intersection({'实体级别','实体类别','英文标识','描述','定义'}) ):
        return True
    return False


def is_shared_attribute_table(section,header):
    """Global attribute definitions declare compatible owners, not per-owner rows."""
    return (column_index(header,*ATTRIBUTE_NAME)>=0 and
            column_index(header,*ATTRIBUTE_CODE)>=0 and
            column_index(header,*SHARED_OWNERS)>=0)


def is_attribute_attachment(section,header):
    """Per-entity attribute declaration, inferred from local header/section."""
    if (column_index(header,*ATTRIBUTE_NAME)<0 or
            column_index(header,*ATTRIBUTE_CODE)<0 or is_shared_attribute_table(section,header)):
        return False
    if column_index(header,*DIRECT_OWNER)>=0:return True
    return _contains_any(section,'实体属性','实体字段','属性挂载','字段挂载','按实体','按对象')


def canonical_header_index(header):
    """Return unambiguous exact header indexes. Duplicate semantic headers are errors.

    Keys are normalized minimally; fuzzy matching that maps `实体类型` to
    `实体名称` is intentionally NOT used for catalog validation.
    """
    result={};duplicates=set()
    for idx,value in enumerate(header):
        key=re.sub(r'[\s_]+','',str(value or '')).casefold()
        if not key:continue
        if key in result:duplicates.add(key)
        else:result[key]=idx
    return result,duplicates
