"""Strict text decoding and bounded structured inputs; no lossy replacement.

A format name selects the reader, not the business meaning of its contents.
Unsupported encodings are errors rather than successful mojibake extraction.
"""
from __future__ import annotations
import codecs
import math
import json
import re
from dataclasses import dataclass

MAX_TEXT_CHARS=1_000_000
MAX_NODES=100_000
MAX_DEPTH=64

@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str


def _check_text(text):
    if '\x00' in text or any(ord(c)<32 and c not in '\n\r\t\f' for c in text):
        raise ValueError('内容包含二进制控制字节；请核对实际格式/编码，不要仅修改扩展名')
    if len(text)>MAX_TEXT_CHARS:
        raise ValueError('文本超过 100 万字符，拒绝静默截断；请分文件上传')
    return text


def decode_text(buf:bytes, declared:str|None=None)->DecodedText:
    """BOM > explicit supported declaration > UTF-8 > GB18030.

    Never try arbitrary BOM-less UTF-16 before Chinese encodings: any even-size
    byte sequence could otherwise decode to unrelated Unicode successfully.
    """
    boms=((codecs.BOM_UTF32_LE,'utf-32'),(codecs.BOM_UTF32_BE,'utf-32'),
          (codecs.BOM_UTF8,'utf-8-sig'),(codecs.BOM_UTF16_LE,'utf-16'),
          (codecs.BOM_UTF16_BE,'utf-16'))
    for bom,enc in boms:
        if buf.startswith(bom):
            try:return DecodedText(_check_text(buf.decode(enc)),enc)
            except UnicodeError as exc:raise ValueError('BOM 与文件编码不一致或文本损坏') from exc
    if declared:
        try:enc=codecs.lookup(declared).name
        except LookupError as exc:raise ValueError('未识别声明的字符编码：'+declared[:50]) from exc
        if enc not in {'utf-8','utf-8-sig','gb18030','gbk','gb2312','big5','cp1252','iso8859-1','ascii','utf-16-le','utf-16-be','utf-32-le','utf-32-be'}:
            raise ValueError('不支持此声明编码：'+declared[:50]+'；请另存 UTF-8')
        try:return DecodedText(_check_text(buf.decode(enc)),enc)
        except UnicodeError as exc:raise ValueError('文字无法按声明编码读取：'+enc) from exc
    for enc in ('utf-8','gb18030'):
        try:return DecodedText(_check_text(buf.decode(enc)),enc)
        except UnicodeError:continue
    raise ValueError('无法无损识别编码；支持 UTF-8、带 BOM 的 UTF-16/32、GB18030。请另存 UTF-8，不会丢弃乱码字符')


def validate_tree(root):
    """Bound expanded nodes and recursion, including YAML alias cycles."""
    count=0;ancestors=set()
    def walk(value,depth):
        nonlocal count
        count+=1
        if count>MAX_NODES or depth>MAX_DEPTH:raise ValueError('结构超过 10 万节点或 64 层，拒绝部分导入；请拆分')
        if isinstance(value,float) and not math.isfinite(value):raise ValueError('非有限数值不能作为可靠的结构化来源')
        if value is not None and not isinstance(value,(str,bool,int,float,list,dict)):raise ValueError('结构包含不支持的值类型，不能无声转换')
        if isinstance(value,(list,dict)):
            ident=id(value)
            if ident in ancestors:raise ValueError('结构包含循环引用，不能作为有限来源文档读取')
            ancestors.add(ident)
            try:
                if isinstance(value,dict):
                    for key,item in value.items():
                        if not isinstance(key,str):raise ValueError('结构字段名必须是字符串，不能悄悄转换数字/布尔键')
                        walk(item,depth+1)
                else:
                    for item in value:walk(item,depth+1)
            finally:ancestors.remove(ident)
    walk(root,0)
    return root


def load_json(text):
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError('JSON 字段重复：'+key[:80]+'；不能让后一项覆盖前一项')
            out[key]=value
        return out
    def invalid(value):raise ValueError('JSON 不支持非有限数字 '+value)
    try:return validate_tree(json.loads(text,object_pairs_hook=pairs,parse_constant=invalid))
    except RecursionError as exc:raise ValueError('JSON 嵌套过深，请拆分') from exc


def text_chunks(text,locator='文本',section='',budget=5000):
    """Bounded line-aware source chunks, never trim a fixed prefix of the file."""
    lines=text.splitlines();out=[];group=[];begin=1;group_size=0
    def emit(end):
        if not group:return
        whole='\n'.join(group)
        for offset in range(0,len(whole),budget):
            part=whole[offset:offset+budget]
            if part.strip():out.append({'kind':'prose','section':section,'text':part,
                'locator':f'{locator} / 行 {begin}-{end} / 片段 {offset//budget+1}',
                'line_start':begin,'line_end':end})
    for number,line in enumerate(lines,1):
        if group and group_size+len(line)+1>budget:
            emit(number-1);group=[];group_size=0;begin=number
        group.append(line);group_size+=len(line)+1
    emit(len(lines))
    return out
