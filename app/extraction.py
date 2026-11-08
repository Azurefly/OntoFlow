"""Versioned multi-format readers. No silent truncation, scripts or external IO."""
from pathlib import Path
import csv
import io
import json
import re
import threading
from .structured_discovery import markdown_chunks,tabular_chunks
from .json_discovery import extract_json_obj,extract_zip,JSONDiscoveryError
from .text_io import decode_text,load_json,text_chunks,MAX_TEXT_CHARS
from .format_registry import SUPPORTED,STANDARD,OPTIONAL as DOCLING_EXTRA,require_reader

PARSER_VERSION='multi-format-v141.1'
_CSV_LOCK=threading.Lock()

class ExtractionError(ValueError):pass

# Compatibility helpers still used by parser/coverage tests.
def _decode(buf):return decode_text(buf).text

def _split_field(text,locator,step=1000):
    return [{'locator':f'{locator} / 片段 {i//step+1}' if len(text)>step else locator,
             'text':text[i:i+step]} for i in range(0,len(text or ''),step) if text[i:i+step].strip()]

def _chunks(text,prefix='行',step=1000):
    return text_chunks(text,prefix,budget=step)

def _docling(path):
    try:from docling.document_converter import DocumentConverter
    except ImportError as exc:raise ExtractionError('该格式需要安装可选 Docling 组件；未验证时请转换为支持格式') from exc
    try:
        result=DocumentConverter().convert(str(path))
        text=result.document.export_to_markdown()
    except Exception as exc:raise ExtractionError('可选转换器未能读取该格式：'+str(exc)[:250]) from exc
    return markdown_chunks(text)


def _generic_json_values(data,source):
    """Fallback for valid, non-schema JSON: preserve leaves, do not invent classes."""
    out=[]
    def walk(value,path):
        if isinstance(value,dict):
            if not value:out.extend(text_chunks('{}',path,source))
            for key,item in value.items():walk(item,path+'['+json.dumps(key,ensure_ascii=False)+']')
        elif isinstance(value,list):
            if not value:out.extend(text_chunks('[]',path,source))
            for i,item in enumerate(value):walk(item,f'{path}[{i}]')
        else:
            # Include the key/path in the represented source so an arbitrary
            # scalar remains understandable without declaring it an ontology.
            text=json.dumps(value,ensure_ascii=False,allow_nan=False)
            out.extend(text_chunks(f'{path} = {text}',path,source))
    walk(data,'$')
    return out


def _structured(data,filename,warnings):
    try:return extract_json_obj(data,filename)
    except JSONDiscoveryError as exc:
        # Invalid UUID/schema errors must NOT be downgraded to a prose success.
        if not str(exc).startswith('JSON 未提供原生本体定义'):raise
        warnings.append('此结构未明确声明本体/集合语义；已保留字段与值供模型或用户理解，不直接把键名当实体类型')
        return _generic_json_values(data,filename)


def _read_email(path):
    from email import policy
    from email.parser import BytesParser
    from .markup_sources import read_html
    msg=BytesParser(policy=policy.default).parsebytes(path.read_bytes());chunks=[];warnings=[]
    header='\n'.join(f'{key}: {msg[key]}' for key in ('Subject','From','To','Cc','Date') if msg[key])
    if header:
        chunks.extend({**x,'source_part':'email_headers'} for x in text_chunks(header,'邮件头'))
    processed=[0]
    def walk(part,loc,depth=0):
        processed[0]+=1
        if depth>32 or processed[0]>1000:raise ValueError('邮件 MIME 结构过深或超过 1000 个片段，请拆分')
        if part.get_content_disposition()=='attachment':
            warnings.append(f'{loc} 包含附件「{part.get_filename() or "未命名"}」，未展开；请将附件单独上传');return
        if part.is_multipart():
            parts=list(part.iter_parts())
            if part.get_content_subtype()=='alternative':
                # Prefer one textual rendition of the same body, not both.
                chosen=next((p for p in parts if p.get_content_type()=='text/plain'),None)
                if chosen is None:chosen=next((p for p in parts if p.get_content_type()=='text/html'),None)
                if chosen is not None:
                    walk(chosen,loc+'/alternative',depth+1)
                    if len(parts)>1:warnings.append(f'{loc} 为多版本邮件正文，已选择一个可读文字版本；其他呈现未用于建模')
                    return
            for n,child in enumerate(parts,1):walk(child,f'{loc}/part[{n}]',depth+1)
            return
        typ=part.get_content_type()
        if typ not in ('text/plain','text/html'):
            warnings.append(f'{loc} 的 {typ} 内容未解读（可能是图片/签名/嵌入消息）');return
        raw=part.get_payload(decode=True) or b''
        decoded=decode_text(raw,part.get_content_charset())
        if typ=='text/html':
            data=read_html(decoded.text);warnings.extend(data['warnings']);units=data['chunks']
        else:units=text_chunks(decoded.text,loc)
        for unit in units:chunks.append({**unit,'locator':loc+' / '+unit['locator'],'source_part':'email_body','encoding':decoded.encoding})
    walk(msg,'邮件正文')
    if msg.defects:warnings.append('邮件包含格式缺陷，已保留可读正文，请核对来源完整性')
    return {'chunks':chunks,'warnings':warnings}


def extract(path:Path):
    path=Path(path);ext=path.suffix.lower()
    try:require_reader(ext)
    except ValueError as exc:raise ExtractionError(str(exc)) from exc
    result=[];warnings=[];encoding=None
    try:
        if ext in {'.txt','.md','.markdown','.rst','.log','.sql','.csv','.tsv','.json','.jsonl','.ndjson','.yaml','.yml','.xml','.html','.htm'}:
            raw=path.read_bytes()
            declared=None
            if ext in {'.html','.htm','.xml'}:
                prefix=raw[:4096].decode('ascii',errors='ignore')
                pattern=(r'<\?xml[^>]*encoding\s*=\s*[\"\x27]([^\"\x27]+)' if ext=='.xml' else r'<meta\b[^>]*charset\s*=\s*[\"\x27]?([\w-]+)')
                match=re.search(pattern,prefix,re.I)
                if match:declared=match.group(1)
            decoded=decode_text(raw,declared);text=decoded.text;encoding=decoded.encoding
            if ext in {'.md','.markdown'}:result=markdown_chunks(text)
            elif ext in {'.txt','.rst','.log','.sql'}:
                result=text_chunks(text,'原文')
                if ext=='.rst' and re.search(r'^\s*\.\.\s+(?:include|image|figure|raw)::',text,re.M):
                    warnings.append('RST 包含外部引用或嵌入指令，只保留指令原文，不执行 include/raw 或读取图片')
            elif ext in {'.csv','.tsv'}:
                delimiter='\t' if ext=='.tsv' else ','
                if ext=='.csv':
                    try:
                        detected=csv.Sniffer().sniff(text[:8192],delimiters=',;\t').delimiter
                        if detected!=',':delimiter=detected
                    except csv.Error:pass
                # Python's field limit is process-global. Protect temporary
                # changes with one lock and restore it even on parse failure.
                with _CSV_LOCK:
                    old=csv.field_size_limit()
                    try:
                        csv.field_size_limit(MAX_TEXT_CHARS)
                        reader=csv.reader(io.StringIO(text,newline=''),delimiter=delimiter,strict=True)
                        rows=[]
                        for row in reader:
                            rows.append((reader.line_num,row))
                            if len(rows)>100000:raise ValueError('CSV/TSV 超过 10 万条记录，请拆分')
                    finally:csv.field_size_limit(old)
                result=tabular_chunks(rows,path.name)
            elif ext=='.json':result=_structured(load_json(text),path.name,warnings)
            elif ext in {'.jsonl','.ndjson'}:
                records=[];physical=[]
                for line_no,line in enumerate(text.splitlines(),1):
                    if not line.strip():continue
                    try:records.append(load_json(line))
                    except (ValueError,RecursionError) as exc:raise ValueError(f'JSONL 第 {line_no} 行无效：{exc}') from exc
                    physical.append(line_no)
                if not records:raise ValueError('JSONL 没有非空记录')
                result=_structured(records,path.name,warnings)
                # Canonical JSONPath stays unchanged for native proof validation.
                for ch in result:
                    match=re.match(r'^\$\[(\d+)\]',ch['locator'])
                    if match and int(match.group(1))<len(physical):
                        ch['source_line']=physical[int(match.group(1))]
            elif ext in {'.yaml','.yml'}:
                from .markup_sources import load_yaml_documents
                for i,data in enumerate(load_yaml_documents(text),1):
                    for ch in _structured(data,path.stem+f'-document-{i}',warnings):
                        result.append({**ch,'locator':f'YAML 文档 {i} / '+ch['locator'],
                            'source_part':'yaml_projection'})
            elif ext=='.xml':
                from .markup_sources import read_xml
                parsed=read_xml(text);result=parsed['chunks'];warnings.extend(parsed['warnings'])
            else:
                from .markup_sources import read_html
                parsed=read_html(text);result=parsed['chunks'];warnings.extend(parsed['warnings'])
        elif ext=='.zip':result=extract_zip(path)
        elif ext=='.eml':
            parsed=_read_email(path);result=parsed['chunks'];warnings.extend(parsed['warnings'])
        elif ext=='.docx':
            from .docx_structure import read_docx
            parsed=read_docx(path);result=parsed['chunks'];warnings.extend(parsed['warnings'])
        elif ext in {'.pdf','.xlsx','.pptx'}:
            from .office_sources import read_pdf,read_xlsx,read_pptx
            parsed={'.pdf':read_pdf,'.xlsx':read_xlsx,'.pptx':read_pptx}[ext](path)
            result=parsed['chunks'];warnings.extend(parsed['warnings'])
        else:
            result=_docling(path)
            warnings.append('通过可选转换器读取，非内置已验收格式；原版式、图片、复杂结构需人工核查')
        for ch in result:
            ch.setdefault('parser',PARSER_VERSION)
            if encoding:ch.setdefault('encoding',encoding)
        return _result(result,warnings)
    except ExtractionError:raise
    except Exception as exc:
        raise ExtractionError(f'解析 {path.name} 失败：{str(exc)[:300]}') from exc


def _result(chunks,warning=None):
    warnings=warning if isinstance(warning,list) else [warning] if warning else []
    keys=('kind','section','line_start','line_end','source_part','source_order','parser','context_before','layout_warning','encoding','source_line')
    units=[]
    for index,c in enumerate(chunks):
        if not str(c.get('text','')).strip():continue
        unit={**{k:c[k] for k in keys if k in c},'locator':str(c['locator']),'text':str(c['text']).strip()}
        unit.setdefault('source_order',index+1);units.append(unit)
        if unit.get('layout_warning'):
            warnings.append(unit['locator']+'：'+str(unit['layout_warning']))
    if not units:raise ExtractionError('未提取到文字内容；空白/扫描文件需要人工提供文字或另行 OCR，不会显示为已完整解析')
    if len(units)>100000 or sum(len(c['text']) for c in units)>MAX_TEXT_CHARS:
        raise ExtractionError('抽取内容超过 100 万字符或 10 万单元，请拆分文件，未进行部分导入')
    return {'chunks':units,'content':'\n'.join(c['text'] for c in units),
            'warnings':list(dict.fromkeys(warnings)),
            'reading_status':'needs_review' if warnings else 'text_read',
            'parser_version':PARSER_VERSION}
