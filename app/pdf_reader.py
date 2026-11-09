"""Text and form-field reading; password boundaries and bounded fallback.

No OCR, active content, network links or password guessing. Empty-open-password
PDFs may be read in memory; a real required password is not bypassed. Poppler is
used only when the primary reader cannot recover a damaged structural index.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from .text_io import text_chunks, MAX_TEXT_CHARS

VERSION='pdf-text-form-v142'


def _object(obj):
    return obj.get_object() if hasattr(obj,'get_object') else obj


def _poppler(path, primary_error):
    executable=shutil.which('pdftotext')
    if not executable:
        raise ValueError('PDF 索引无法读取且缺少 pdftotext 兼容组件；请重建含 poppler-utils 的镜像') from primary_error
    # Disk-backed output keeps memory bounded; never interpolate paths into a shell.
    with tempfile.TemporaryDirectory(prefix='ontoflow-pdf-') as folder:
        target=Path(folder)/'text.txt'
        proc=subprocess.run([executable,'-upw','','-layout','-enc','UTF-8',str(Path(path).resolve()),str(target)],
                            capture_output=True,timeout=45,check=False)
        error=proc.stderr.decode('utf-8',errors='replace')[:500]
        if proc.returncode:
            if 'password' in error.casefold():raise ValueError('PDF 需要有效打开口令，未尝试猜测或绕过')
            raise ValueError('PDF 兼容读取失败：'+error)
        if not target.exists() or target.stat().st_size>MAX_TEXT_CHARS*4:
            raise ValueError('PDF 兼容读取结果为空或超出安全上限')
        text=target.read_text('utf-8')
    if len(text)>MAX_TEXT_CHARS:raise ValueError('PDF 文字超过 100 万字符，请拆分')
    pages=text.split('\f')
    if pages and not pages[-1].strip():pages.pop()
    if len(pages)>2000:raise ValueError('PDF 超过 2000 页，请拆分')
    chunks=[];warnings=['主读取器无法恢复 PDF 索引，使用 Poppler 只读兼容路径；原始文件未改写，表单和图形完整性未验证']
    for number,content in enumerate(pages,1):
        if not content.strip():warnings.append(f'PDF 第 {number} 页无可读文字；未执行 OCR')
        chunks.extend({**c,'source_part':'pdf_fallback','source_order':len(chunks)+i+1,
                       'parser':VERSION} for i,c in enumerate(text_chunks(content,f'PDF 第 {number} 页 / 兼容读取')))
    if not chunks:raise ValueError('PDF 没有可读取的文字层，未执行 OCR')
    return {'chunks':chunks,'warnings':warnings}


def _forms(reader, warnings):
    """Return exact stored field values; never execute XFA or JavaScript."""
    out=[]
    try:
        root=_object(reader.trailer.get('/Root')) or {}
        acro=_object(root.get('/AcroForm')) or {}
        if acro.get('/XFA'):
            warnings.append('PDF 包含 XFA 动态表单，未执行或解释 XFA；仅保留可读文字及标准字段')
        fields=reader.get_fields() or {}
        if len(fields)>10000:raise ValueError('PDF 表单超过 10000 个字段，请拆分')
        for field_name,field in fields.items():
            field=_object(field);typ=str(field.get('/FT',''))
            if typ=='/Sig':
                warnings.append('PDF 含数字签名，未验证签名有效性；不把签名证书作为业务文字');continue
            if typ not in ('/Tx','/Btn','/Ch',''):
                warnings.append(f'PDF 表单字段「{field_name}」类型 {typ} 未完整解释');continue
            value=field.get('/V')
            # Preserve the actual value type/state, not the visual checkbox glyph.
            if isinstance(value,list):value=[str(v) for v in value]
            elif value is not None:value=str(value)
            record={'字段':str(field_name),'类型':typ,'保存值':value}
            if typ=='/Btn':
                record['选中状态']=('未选中' if value=='/Off' else '选中' if value is not None else '未提供保存状态')
            if '/DV' in field:record['默认值']=str(field['/DV'])
            text=json.dumps(record,ensure_ascii=False)
            for chunk in text_chunks(text,'PDF 表单 / '+str(field_name)):
                out.append({**chunk,'source_part':'pdf_form','parser':VERSION})
        if out:
            warnings.append('PDF 标准表单的保存值与按钮状态已单独读取；未执行脚本，版面/未保存的可视状态仍需核对')
    except ValueError:raise
    except Exception as exc:
        warnings.append('PDF 表单字段未能完整读取：'+type(exc).__name__+'；请人工核查')
    return out


def read_pdf(path):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError, FileNotDecryptedError, WrongPasswordError
    try:
        reader=PdfReader(str(path),strict=False)
    except (WrongPasswordError,FileNotDecryptedError) as exc:
        raise ValueError('PDF 需要有效打开口令，未尝试猜测或绕过') from exc
    except PdfReadError as exc:
        return _poppler(path,exc)
    warnings=[];chunks=[]
    if reader.is_encrypted:
        try:opened=bool(reader.decrypt(''))
        except Exception as exc:raise ValueError('PDF 无法使用空打开口令读取；需要授权口令或可读副本') from exc
        if not opened:raise ValueError('PDF 需要有效打开口令，未尝试猜测或绕过')
        warnings.append('PDF 带加密标志，但空打开口令可读取；仅内存读取，原文件及权限标志未修改')
    if len(reader.pages)>2000:raise ValueError('PDF 超过 2000 页，请拆分')
    total=0
    for i,page in enumerate(reader.pages,1):
        text=page.extract_text() or '';total+=len(text)
        if total>MAX_TEXT_CHARS:raise ValueError('PDF 文字超过 100 万字符，请拆分')
        resources=_object(page.get('/Resources')) or {}
        objects=_object(resources.get('/XObject')) or {}
        has_image=any((_object(obj) or {}).get('/Subtype') in ('/Image','/Form') for obj in objects.values())
        if not text.strip():warnings.append(f'PDF 第 {i} 页没有可读文字层；未执行 OCR，不能认定该页内容已覆盖')
        if has_image:warnings.append(f'PDF 第 {i} 页含图片或嵌入图形；图像文字与关系未核实')
        if '\ufffd' in text:warnings.append(f'PDF 第 {i} 页文字有无法映射字符，请核对字体编码')
        for chunk in text_chunks(text,f'PDF 第 {i} 页'):
            chunks.append({**chunk,'source_part':'pdf_text','parser':VERSION,'source_order':len(chunks)+1})
    for field in _forms(reader,warnings):
        field['source_order']=len(chunks)+1;chunks.append(field)
    if not chunks:raise ValueError('PDF 没有可读取的文字层，当前未执行 OCR；请提供文字版文件')
    warnings.append('PDF 按文字层和标准表单读取；多栏顺序、表格边界和图形关系未作完整版式验证')
    return {'chunks':chunks,'warnings':list(dict.fromkeys(warnings))}
