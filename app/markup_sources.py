"""Ordered HTML, XML and safe YAML adapters with explicit source boundaries."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass,field
from html.parser import HTMLParser
import json
import re
from .structured_discovery import tabular_chunks
from .text_io import text_chunks,validate_tree

PARSER_VERSION='multi-text-v141.1'

@dataclass
class Node:
    tag:str
    attrs:dict=field(default_factory=dict)
    children:list=field(default_factory=list)
    line:int=1

class HTMLTree(HTMLParser):
    VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root=Node('document');self.stack=[self.root];self.count=0;self.repairs=0
    def handle_starttag(self,tag,attrs):
        self.count+=1
        if self.count>100000 or len(self.stack)>100:raise ValueError('HTML 结构过大或嵌套过深')
        # Handle common optional-end-tag HTML without swallowing the next row.
        if tag in {'p','li','tr','td','th'} and self.stack[-1].tag==tag:
            self.stack.pop();self.repairs+=1
        node=Node(tag,dict(attrs),line=self.getpos()[0]);self.stack[-1].children.append(node)
        if tag not in self.VOID:self.stack.append(node)
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:
                if i!=len(self.stack)-1:self.repairs+=1
                del self.stack[i:];return
    def handle_data(self,data):self.stack[-1].children.append(data)

_IGNORED={'script','style','template'}
_BLOCK={'p','li','div','section','article','main','aside','header','footer','ul','ol','dl','dt','dd','blockquote','table','pre','h1','h2','h3','h4','h5','h6'}
def _html_text(node):
    if isinstance(node,str):return node
    if node.tag in _IGNORED:return ''
    if node.tag=='br':return '\n'
    if node.tag=='img':return node.attrs.get('alt','')
    return ''.join(_html_text(x)+( '\n' if isinstance(x,Node) and x.tag in _BLOCK else '') for x in node.children)

def read_html(text):
    parser=HTMLTree();parser.feed(text);parser.close()
    chunks=[];warnings=[];headings={};prior='';order=0
    def section():return ' / '.join(headings[k] for k in sorted(headings))
    def emit(value,kind,node,**extra):
        nonlocal order,prior
        value=value.strip()
        if not value:return
        parts=[{'text':value,'kind':kind,'locator':f'HTML 行 {node.line}'}] if kind=='heading' else text_chunks(value,f'HTML 行 {node.line}',section())
        for ch in parts:
            order+=1;chunks.append({**ch,'kind':kind,'section':section(),'source_part':'html',
                'source_order':order,'parser':PARSER_VERSION,**extra})
        if kind=='prose':prior=value[-800:]
    def descendants(node,tag):
        for child in node.children:
            if not isinstance(child,Node):continue
            if child.tag==tag:yield child
            yield from descendants(child,tag)
    def extras(node):
        # Captions/alternative text are not image OCR.
        for image in descendants(node,'img'):
            warnings.append(f'HTML 行 {image.line} 含图片；只保留替代文字，未识别图片内文字或跟随网络地址')
        for frame in descendants(node,'iframe'):
            warnings.append(f'HTML 行 {frame.line} 含外部嵌入页，未抓取其内容')
    extras(parser.root)
    def walk(node):
        nonlocal prior,order
        if isinstance(node,str):return
        if node.tag in _IGNORED:return
        if re.fullmatch(r'h[1-6]',node.tag):
            level=int(node.tag[1]);value=_html_text(node).strip()
            for key in list(headings):
                if key>=level:del headings[key]
            headings[level]=value;emit('#'*level+' '+value,'heading',node);prior='';return
        if node.tag=='table':
            rows=[];ambiguous=False
            for ix,row in enumerate(descendants(node,'tr'),1):
                cells=[x for x in row.children if isinstance(x,Node) and x.tag in ('td','th')]
                if not cells:continue
                ambiguous|=any(x.attrs.get('rowspan','1')!='1' or x.attrs.get('colspan','1')!='1' for x in cells)
                rows.append((ix,[_html_text(x).strip() for x in cells]))
            ambiguous|=bool(list(descendants(node,'table')))
            ambiguous|=len({len(vals) for _,vals in rows})>1
            if not rows:
                emit(_html_text(node),'ambiguous_table',node,layout_warning='未还原表格单元格');warnings.append(f'HTML 行 {node.line} 的表格无法还原，已保留文字');return
            if ambiguous:
                emit('\n'.join(' | '.join(v) for _,v in rows),'ambiguous_table',node,layout_warning='合并/嵌套/不等列表格需人工核查',context_before=prior)
                warnings.append(f'HTML 行 {node.line} 含合并、嵌套或不等列表格；保留文字，不猜测属性归属')
            else:
                for ch in tabular_chunks(rows,f'HTML 行 {node.line} 表格'):
                    order+=1;chunks.append({**ch,'section':section(),'source_order':order,
                        'source_part':'html','parser':PARSER_VERSION,'context_before':prior})
            prior='';return
        if node.tag=='img':
            if node.attrs.get('alt'):emit(node.attrs['alt'],'prose',node,layout_warning='仅图片替代文字')
            return
        if node.tag in ('p','li','pre','blockquote','dt','dd') and not any(isinstance(c,Node) and (c.tag=='table' or re.fullmatch(r'h[1-6]',c.tag)) for c in node.children):
            emit(_html_text(node),'prose',node);return
        # Preserve mixed inline text and block order in containers, never flatten
        # the entire body before recognizing tables/headings.
        inline=[]
        def flush():
            if inline:emit(''.join(inline),'prose',node);inline.clear()
        for child in node.children:
            if isinstance(child,str):inline.append(child)
            elif child.tag in _IGNORED:continue
            elif child.tag in _BLOCK or child.tag in ('img','iframe'):
                flush();walk(child)
            elif child.tag in ('html','body') or any(isinstance(c,Node) and c.tag in _BLOCK for c in child.children):
                flush();walk(child)
            elif child.tag=='head':continue
            else:inline.append(_html_text(child))
        flush()
    walk(parser.root)
    if parser.repairs or len(parser.stack)>1:warnings.append('HTML 存在未闭合或需容错恢复的结构；已保留可读内容，请核查表格/标题边界')
    return {'chunks':chunks,'warnings':list(dict.fromkeys(warnings))}


def read_xml(text):
    from defusedxml import ElementTree as SafeET
    # Text is already decoded. Do not allow DTD, external entities, XInclude or
    # file/network expansion, even if a document only declares an innocuous DTD.
    root=SafeET.fromstring(text,forbid_dtd=True,forbid_entities=True,forbid_external=True)
    chunks=[];warnings=[];counter=0
    def emit(value,path,part):
        nonlocal counter
        if not value or not value.strip():return
        for chunk in text_chunks(value,'XPath '+path,path):
            counter+=1
            if counter>100000:raise ValueError('XML 来源单元超出安全上限，请拆分')
            chunks.append({**chunk,'source_part':part,'source_order':counter,'parser':PARSER_VERSION})
    def walk(node,path,depth=0):
        if depth>64:raise ValueError('XML 嵌套超过 64 层，请拆分')
        for key,value in node.attrib.items():emit(f'{key}="{value}"',path+'/@'+key,'xml_attribute')
        # The lexical text of the entire element is emitted when it is inline
        # mixed content, so “实体：<b>设备</b>” remains intact. Attribute/tail
        # locators are retained separately where elements are structured.
        children=list(node)
        inline={'b','i','em','strong','span','a','sub','sup','u'}
        mixed=bool(children and (node.text or '').strip() and
                   (any((c.tail or '').strip() for c in children) or all(str(c.tag).split('}')[-1] in inline for c in children)))
        if mixed:
            emit(''.join(node.itertext()),path+'/text()','xml_mixed')
            def nested_attrs(parent,prefix,level):
                if level>64:raise ValueError('XML 嵌套超过 64 层')
                counts=Counter()
                for child in parent:
                    counts[child.tag]+=1
                    sub=prefix+'/'+str(child.tag)+f'[{counts[child.tag]}]'
                    for key,value in child.attrib.items():emit(f'{key}="{value}"',sub+'/@'+key,'xml_attribute')
                    nested_attrs(child,sub,level+1)
            nested_attrs(node,path,depth)
            return
        emit(node.text,path+'/text()','xml_text')
        indexes=Counter()
        for child in children:
            indexes[child.tag]+=1
            sub=path+'/'+str(child.tag)+f'[{indexes[child.tag]}]'
            if str(child.tag)=='{http://www.w3.org/2001/XInclude}include':warnings.append('XML XInclude 仅保留引用信息，没有读取外部资源')
            walk(child,sub,depth+1);emit(child.tail,sub+'/tail()','xml_tail')
    walk(root,'/'+str(root.tag)+'[1]')
    return {'chunks':chunks,'warnings':warnings}


def load_yaml_documents(text):
    import yaml
    # Bound anchors before construction. Reject cyclic/alias-heavy inputs before
    # expanding them; safe tags only, duplicate keys must not override facts.
    aliases=0;depth=0
    for event in yaml.parse(text,Loader=yaml.SafeLoader):
        if isinstance(event,yaml.events.AliasEvent):
            aliases+=1
            if aliases>100:raise ValueError('YAML 别名引用超过安全上限，未导入')
        if isinstance(event,(yaml.events.MappingStartEvent,yaml.events.SequenceStartEvent)):
            depth+=1
            if depth>64:raise ValueError('YAML 嵌套超过 64 层')
        if isinstance(event,(yaml.events.MappingEndEvent,yaml.events.SequenceEndEvent)):depth-=1
    class StrictSafeLoader(yaml.SafeLoader):pass
    def mapping(loader,node,deep=False):
        out={}
        for key_node,value_node in node.value:
            key=loader.construct_object(key_node,deep=deep)
            if not isinstance(key,str):raise ValueError('YAML 键必须是字符串；请为数字/布尔形式的键加引号')
            if key in out:raise ValueError('YAML 字段重复：'+key[:80])
            out[key]=loader.construct_object(value_node,deep=deep)
        return out
    StrictSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,mapping)
    # Keep dates as source lexemes, not datetime objects that alter JSON schemas.
    StrictSafeLoader.add_constructor('tag:yaml.org,2002:timestamp',lambda loader,node:loader.construct_scalar(node))
    docs=[]
    for doc in yaml.load_all(text,Loader=StrictSafeLoader):
        if len(docs)>=100:raise ValueError('YAML 文档流超过 100 个文档，请拆分')
        docs.append(validate_tree(doc))
    return docs
