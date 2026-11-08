"""Spreadsheet/presentation/PDF reading with explicit unsupported-source warnings.

Never executes formulas, fetches hyperlinks, performs OCR, or claims inferred
reading order for floating objects. All source coordinates are local to input.
"""
from __future__ import annotations
from itertools import zip_longest
from zipfile import ZipFile
from pathlib import PurePosixPath
from .structured_discovery import tabular_chunks
from .text_io import text_chunks

PARSER_VERSION='multi-office-v141.1'


def guard_office_archive(path,required):
    with ZipFile(path) as archive:
        infos=archive.infolist()
        if len(infos)>5000 or sum(i.file_size for i in infos)>180_000_000:
            raise ValueError('Office 解压内容超出安全上限，未进行部分导入')
        for item in infos:
            p=PurePosixPath(item.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in item.filename or item.flag_bits&1:
                raise ValueError('Office 包含非法路径或加密条目')
            if item.file_size>40_000_000:raise ValueError('Office 内部单项过大，请拆分')
        if required not in archive.namelist():raise ValueError('扩展名与实际 Office 文件格式不一致')


def read_xlsx(path):
    from openpyxl import load_workbook
    guard_office_archive(path,'xl/workbook.xml')
    warnings=[];chunks=[];order=0
    with ZipFile(path) as archive:
        merges={name for name in archive.namelist() if name.startswith('xl/worksheets/') and name.endswith('.xml') and b'<mergeCell' in archive.read(name)}
        unsupported=[name for name in archive.namelist() if name.startswith(('xl/drawings/','xl/charts/','xl/media/')) and name.endswith(('.xml','.png','.jpg','.jpeg'))]
        if unsupported:warnings.append('工作簿含图片、图表或绘图对象；仅读取单元格文字，未识别其中的图形内容')
    wb=load_workbook(path,read_only=True,data_only=False,keep_links=False)
    cached=load_workbook(path,read_only=True,data_only=True,keep_links=False)
    try:
        for sheet,cached_sheet in zip(wb,cached):
            if sheet.max_row and sheet.max_column and sheet.max_row*sheet.max_column>2_000_000:
                raise ValueError('工作表声明区域超过 200 万单元格，请清理空白格式或拆分')
            if sheet.sheet_state!='visible':warnings.append(f'工作表「{sheet.title}」处于隐藏状态；已保留其单元格内容供人工决定是否使用')
            rows=[];formula_total=0;missing=0
            for row_no,(raw,values) in enumerate(zip_longest(sheet.iter_rows(),cached_sheet.iter_rows(values_only=True),fillvalue=()),1):
                display=[]
                for col,cell in enumerate(raw):
                    value=cell.value
                    if cell.data_type=='f':
                        formula_total+=1
                        value=values[col] if col<len(values) else None
                        if value is None:
                            missing+=1;value=cell.value  # formula source retained, never execute it
                    display.append('' if value is None else str(value))
                if any(display):rows.append((row_no,display))
            if formula_total:warnings.append(f'工作表「{sheet.title}」包含 {formula_total} 个公式；未计算公式，{missing} 个缺少缓存结果，已保留公式原文')
            merged=any(str(getattr(sheet,'_worksheet_path','')).lstrip('/')==x for x in merges)
            # Older reader versions may not expose a sheet path; conservatively
            # mark those sheets rather than silently reassign merged values.
            if merges and not getattr(sheet,'_worksheet_path',''):merged=True
            if merged:warnings.append(f'工作表「{sheet.title}」含合并单元格，值的归属需要核查')
            for item in tabular_chunks(rows,sheet.title):
                order+=1
                if merged or missing:
                    item['kind']='ambiguous_table';item['layout_warning']='合并单元格或公式缓存缺失，不自动推断字段归属/类型'
                chunks.append({**item,'section':sheet.title,'source_part':'worksheet',
                    'source_order':order,'parser':PARSER_VERSION})
    finally:
        wb.close();cached.close()
    return {'chunks':chunks,'warnings':warnings}


def read_pptx(path):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    guard_office_archive(path,'ppt/presentation.xml')
    deck=Presentation(str(path));chunks=[];warnings=[];order=0
    for page_no,slide in enumerate(deck.slides,1):
        if page_no>2000:raise ValueError('幻灯片超过 2000 页，请拆分')
        title_shape=slide.shapes.title
        title=title_shape.text.strip() if title_shape is not None and title_shape.has_text_frame else ''
        section=f'第{page_no}页'+(f' / {title}' if title else '')
        before_count=len(chunks)
        def emit(text,kind,loc,part='slide',**extra):
            nonlocal order
            for item in text_chunks(text,loc,section):
                order+=1;chunks.append({**item,'kind':kind,'source_part':part,'source_order':order,
                    'parser':PARSER_VERSION,**extra})
        def walk(shapes,prefix):
            nonlocal order
            for ix,shape in enumerate(shapes,1):
                loc=f'{prefix} / 对象{ix}'
                if shape.shape_type==MSO_SHAPE_TYPE.GROUP:
                    walk(shape.shapes,loc+' / 组合');continue
                if shape.has_text_frame:
                    is_title=title_shape is not None and shape.shape_id==title_shape.shape_id
                    emit(('# '+shape.text) if is_title else shape.text,'heading' if is_title else 'prose',loc)
                if shape.has_table:
                    rows=[];ambiguous=False
                    for r,row in enumerate(shape.table.rows,1):
                        values=[]
                        for cell in row.cells:
                            ambiguous|=bool(cell.is_merge_origin or cell.is_spanned)
                            values.append(cell.text)
                        rows.append((r,values))
                    for item in tabular_chunks(rows,loc+' / 表格'):
                        order+=1
                        if ambiguous:
                            item['kind']='ambiguous_table';item['layout_warning']='PPT 合并单元格需人工核查'
                        chunks.append({**item,'section':title or section,'source_part':'slide_table',
                            'source_order':order,'parser':PARSER_VERSION})
                    if ambiguous:warnings.append(f'{loc} 表格含合并单元格，不自动推断其归属')
                if shape.shape_type==MSO_SHAPE_TYPE.PICTURE:
                    warnings.append(f'{loc} 为图片；未执行图片文字识别')
                if getattr(shape,'has_chart',False):warnings.append(f'{loc} 为图表，图表数据和图形含义未完整读取')
                if shape.shape_type in (MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT,MSO_SHAPE_TYPE.LINKED_OLE_OBJECT):warnings.append(f'{loc} 为嵌入对象；未打开外部或嵌入文件')
        walk(slide.shapes,f'幻灯片 {page_no}')
        if slide.has_notes_slide:
            frame=slide.notes_slide.notes_text_frame
            if frame is not None and frame.text.strip():emit(frame.text,'note',f'幻灯片 {page_no} / 备注','speaker_notes')
        if len(chunks)==before_count:warnings.append(f'幻灯片 {page_no} 没有可读取文字，可能是空白或图像页')
    if any(len(slide.shapes)>1 for slide in deck.slides):
        warnings.append('幻灯片按文件对象顺序读取；自由布局、箭头和空间位置不被当作业务关系，需人工核查阅读顺序')
    return {'chunks':chunks,'warnings':list(dict.fromkeys(warnings))}


def read_pdf(path):
    from pypdf import PdfReader
    reader=PdfReader(str(path));warnings=[];chunks=[]
    if reader.is_encrypted:raise ValueError('PDF 已加密，请先提供可读取的解密副本')
    if len(reader.pages)>2000:raise ValueError('PDF 超过 2000 页，请拆分')
    for i,page in enumerate(reader.pages,1):
        text=page.extract_text() or ''
        # Only inspect resource metadata; do not decode images or apply OCR.
        resources=page.get('/Resources') or {}
        if hasattr(resources,'get_object'):resources=resources.get_object()
        objects=resources.get('/XObject') or {}
        if hasattr(objects,'get_object'):objects=objects.get_object()
        has_image=False
        for obj in objects.values():
            obj=obj.get_object() if hasattr(obj,'get_object') else obj
            if obj.get('/Subtype') in ('/Image','/Form'):has_image=True
        if not text.strip():warnings.append(f'PDF 第 {i} 页没有可读文字层；未执行 OCR，不能认定该页内容已覆盖')
        if has_image:warnings.append(f'PDF 第 {i} 页含图片或嵌入图形；只读取文字层，图像内文字与图表语义未核实')
        if '\ufffd' in text:warnings.append(f'PDF 第 {i} 页文字存在无法映射字符，请核对字体编码')
        for chunk in text_chunks(text,f'PDF 第 {i} 页'):
            chunks.append({**chunk,'source_part':'pdf_text','parser':PARSER_VERSION,
                'source_order':len(chunks)+1})
    if not chunks:raise ValueError('PDF 没有可读取的文字层，当前未执行 OCR；请提供可复制文字的 PDF 或文字版文件')
    # Text extraction alone never certifies the reading order or table geometry.
    warnings.append('PDF 按文字层读取；多栏阅读顺序、表格边界和图形关系未作完整版式验证')
    return {'chunks':chunks,'warnings':warnings}
