"""Ordered DOCX source units with explicit coverage boundaries (no OCR, no LLM).

Heading styles/outline levels are context, not a mandate to create an ontology
class. Appearance-only headings stay prose. Paragraphs/tables are read in body
order; evidence locators do not pretend OOXML contains stable page numbers.
"""
from __future__ import annotations
import re
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.table import Table
from lxml import etree
from .structured_discovery import tabular_chunks

PARSER_VERSION='docx-ordered-strict-v2'


def _text(element):
    pieces=[]
    for node in element.iter():
        # Do not count text from a drawing/textbox as ordinary body text.
        ancestors=set()
        for parent in node.iterancestors():
            if parent is element:break
            ancestors.add(parent.tag)
        if qn('w:txbxContent') in ancestors or qn('w:del') in ancestors:
            continue
        if node.tag==qn('w:t') and node.text:pieces.append(node.text)
        elif node.tag==qn('w:tab'):pieces.append('\t')
        elif node.tag in (qn('w:br'),qn('w:cr')):pieces.append('\n')
    return ''.join(pieces).strip()


def _heading_level(paragraph):
    ppr=paragraph._p.pPr
    if ppr is not None:
        val=ppr.find(qn('w:outlineLvl'))
        if val is not None:
            n=int(val.get(qn('w:val'),'9'))
            if 0<=n<9:return n+1
    style=paragraph.style
    visited=set()
    while style is not None and style.style_id not in visited:
        visited.add(style.style_id)
        if style.name and re.fullmatch(r'(?:Heading|标题)\s*([1-9])',style.name,re.I):
            return int(re.search(r'([1-9])$',style.name).group(1))
        ppr=style.element.pPr
        if ppr is not None:
            level=ppr.find(qn('w:outlineLvl'))
            if level is not None and 0<=int(level.get(qn('w:val'),'9'))<9:
                return int(level.get(qn('w:val')))+1
        style=style.base_style
    return None


def read_docx(path:Path, budget=5000):
    warnings=[]
    # Only read OOXML members in memory. Do not extract or follow external links.
    with ZipFile(path) as archive:
        entries=archive.infolist()
        if len(entries)>4000 or sum(e.file_size for e in entries)>180_000_000:
            raise ValueError('DOCX 解压后内容超出安全解析上限，未进行部分成功导入')
        if any(e.file_size>35_000_000 for e in entries):
            raise ValueError('DOCX 存在过大的内部数据项，请拆分原文件后再试')
        if any(e.flag_bits&1 for e in entries):
            raise ValueError('不支持加密的 DOCX')
        if 'word/document.xml' not in archive.namelist():
            raise ValueError('文件不是包含正文的 DOCX；请核对真实格式，而非只修改扩展名')
        note_parts={name:archive.read(name) for name in ('word/footnotes.xml','word/endnotes.xml') if name in archive.namelist()}
    from .ooxml_compat import compatible_docx_stream
    stream,strict_projected=compatible_docx_stream(path)
    if strict_projected:
        warnings.append('Strict OOXML 按结构命名空间在内存中兼容读取；原文件未修改，扩展版式需核查')
        with ZipFile(stream) as compatible:
            note_parts={name:compatible.read(name) for name in note_parts}
        stream.seek(0)
    document=Document(stream)
    output=[];headings={};counts={'paragraphs':0,'tables':0,'images':0,'textboxes':0,'tracked_changes':0}
    visited_parts=set();source_order=0

    def section():return ' / '.join(headings[k] for k in sorted(headings))
    def add(text,kind,locator,*,context='',source_part='body',**extra):
        nonlocal source_order
        if not text.strip():return
        # Retain all paragraph text in bounded chunks, preferably at sentences.
        parts=[]
        while len(text)>budget:
            boundary=max(text.rfind(mark,0,budget) for mark in ('。','；','\n','!','?'))
            boundary=boundary+1 if boundary>=budget//2 else budget
            parts.append(text[:boundary]);text=text[boundary:]
        if text:parts.append(text)
        for i,part in enumerate(parts,1):
            source_order+=1
            output.append({'text':part,'kind':kind,'section':context,'source_part':source_part,
                'locator':locator+(f' / 片段 {i}' if len(parts)>1 else ''),
                'source_order':source_order,'parser':PARSER_VERSION,**extra})

    def read_table(element,parent,locator,source_part='body',prior=''):
        nonlocal source_order
        counts['tables']+=1
        table=Table(element,parent)
        rows=[];ambiguous=bool(element.findall('.//'+qn('w:tbl')))
        for i,row in enumerate(table.rows,1):
            cells=row.cells
            if len({id(cell._tc) for cell in cells})!=len(cells):ambiguous=True
            if any(tc.find('.//'+qn('w:vMerge')) is not None for tc in row._tr.tc_lst):ambiguous=True
            rows.append((i,[_text(cell._tc) for cell in cells]))
        widths={len(values) for _,values in rows}
        if len(widths)>1:ambiguous=True
        if ambiguous:
            warnings.append(f'{locator} 含合并或不等列单元格：保留原始行文本，但不按猜测列位自动生成本体定义。')
            add('\n'.join('| '+' | '.join(vals)+' |' for _,vals in rows),
                'ambiguous_table',locator,context=section(),source_part=source_part,
                context_before=prior,layout_warning='merged_or_uneven_cells')
        else:
            for chunk in tabular_chunks(rows,locator,max_chars=budget):
                source_order+=1
                output.append({**chunk,'section':section(),'source_part':source_part,
                    'source_order':source_order,'parser':PARSER_VERSION,'context_before':prior})
        # Parent cell text excludes nested tables only in standard docx.cell.text;
        # our XML text includes it. Keep explicit boundary warning rather than
        # claiming all nested content was structurally interpreted.
        nested=element.findall('.//'+qn('w:tbl'))
        if nested:
            warnings.append(f'{locator} 含嵌套表格；已保留可读单元格文本，嵌套列结构需人工核查。')
        extras(element,locator,source_part)

    def extras(element,locator,source_part):
        images=list(element.iter(qn('a:blip')))+list(element.iter('{urn:schemas-microsoft-com:vml}imagedata'))
        if images:
            counts['images']+=len(images)
            warnings.append(f'{locator} 有 {len(images)} 个图片引用：未识别图片中文字，不能视为全文已覆盖。')
        for i,box in enumerate(element.iter(qn('w:txbxContent')),1):
            counts['textboxes']+=1
            # Reading textboxes as an explicitly separate source prevents their
            # floating layout from changing table ownership.
            txt='\n'.join(_text(p) for p in box.iter(qn('w:p')) if _text(p))
            add(txt,'textbox',f'{locator} / 文本框{i}',context=section(),source_part=source_part)
            warnings.append(f'{locator} 文本框文字已提取；浮动位置不用于推断归属。')
        revisions=list(element.iter(qn('w:del')))+list(element.iter(qn('w:ins')))
        if revisions:
            counts['tracked_changes']+=len(revisions)
            warnings.append(f'{locator} 存在修订标记：正文按可见新增文字读取，删除内容不采纳；须人工核对修订状态。')

    def walk(container,parent,prefix,part='body'):
        last_prose='';block=0
        for element in container:
            tag=element.tag
            if tag==qn('w:p'):
                block+=1;counts['paragraphs']+=1
                para=Paragraph(element,parent);text=_text(element)
                locator=f'{prefix} / 块{block} / 段落'
                level=_heading_level(para)
                if level is not None and text:
                    headings_copy={k:v for k,v in headings.items() if k<level}
                    headings.clear();headings.update(headings_copy);headings[level]=text
                    add('#'*min(level,6)+' '+text,'heading',locator,context=section(),source_part=part)
                    last_prose=''
                else:
                    add(text,'prose',locator,context=section(),source_part=part)
                    last_prose=text[-800:]
                extras(element,locator,part)
            elif tag==qn('w:tbl'):
                block+=1
                read_table(element,parent,f'{prefix} / 块{block} / 表格',part,last_prose)
                last_prose=''
            elif tag==qn('w:sdt'):
                content=element.find(qn('w:sdtContent'))
                if content is not None:
                    block+=1;walk(content,parent,f'{prefix} / 块{block} / 内容控件',part)
            elif tag in (qn('w:ins'),qn('w:del')):
                warnings.append(f'{prefix} 有块级修订，未自动判定修订后生效内容，需人工核查。')
            elif tag not in (qn('w:sectPr'),qn('w:bookmarkStart'),qn('w:bookmarkEnd')):
                if _text(element):
                    block+=1
                    add(_text(element),'prose',f'{prefix} / 未分类块{block}',context=section(),source_part=part)
                    warnings.append(f'{prefix} 含非标准正文块，保留文字但未推断结构。')
    walk(document.element.body,document,'正文')
    # Header/footer parts can be shared across sections. Read each once and
    # keep them separate from body headings and semantic ownership.
    for section_index,sec in enumerate(document.sections,1):
        for label,attribute in (('页眉','header'),('页脚','footer'),('首页页眉','first_page_header'),('首页页脚','first_page_footer'),('偶数页眉','even_page_header'),('偶数页脚','even_page_footer')):
            area=getattr(sec,attribute)
            if area.is_linked_to_previous:continue
            key=str(area.part.partname)
            if key in visited_parts:continue
            visited_parts.add(key);headings.clear()
            walk(area._element,area,f'节{section_index} / {label}',attribute)
    for name,data in note_parts.items():
        root=etree.fromstring(data,parser=etree.XMLParser(resolve_entities=False,no_network=True))
        label='脚注' if 'footnotes' in name else '尾注'
        for note in root:
            nid=note.get(qn('w:id'),'')
            if not nid.isdigit():continue
            text='\n'.join(_text(p) for p in note.iter(qn('w:p')) if _text(p))
            add(text,'note',f'{label} {nid}',source_part=label)
    if not output:
        raise ValueError('DOCX 未读取到正文文字。'+(' 检测到图片，当前未执行 OCR。' if counts['images'] else ' 请核对文件是否为空。'))
    return {'chunks':output,'warnings':list(dict.fromkeys(warnings)),
            'coverage':{'format':'docx','counts':counts,
                        'complete':not warnings,'ocr_performed':False,
                        'body_order_preserved':True,'page_numbers_available':False}}
