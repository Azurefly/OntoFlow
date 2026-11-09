"""One format registry for upload validation, runtime capability API and UI.

Optional conversion candidates are NOT advertised as verified built-in readers.
Installing a converter never proves it supports every legacy binary format.
"""
from importlib.util import find_spec

BUILTIN={
 '.txt':('文本',None,'完整文字；不按文件名推断业务类型'),
 '.md':('Markdown',None,'标题、表格与原文顺序'),
 '.markdown':('Markdown',None,'与 .md 相同'),
 '.rst':('reStructuredText',None,'保留标记原文供语义理解；不执行 include 等指令'),
 '.log':('日志',None,'完整文字；日志条目不是本体实例'),
 '.sql':('SQL 文本',None,'只读取，不连接数据库或执行脚本'),
 '.csv':('CSV',None,'表头与完整行；不等列保留并提示'),
 '.tsv':('TSV',None,'Tab 分隔；保留表头'),
 '.json':('JSON',None,'原生本体/显式定义/业务描述分流；重复键拒绝'),
 '.jsonl':('JSON Lines',None,'所有非空记录，错误给出原始行号'),
 '.ndjson':('NDJSON',None,'与 JSONL 相同'),
 '.yaml':('YAML','yaml','安全解析多文档流；不执行对象标签/外部引用'),
 '.yml':('YAML','yaml','与 .yaml 相同'),
 '.xml':('XML','defusedxml','保留节点/属性/tail；禁用 DTD/外部实体'),
 '.html':('HTML',None,'标题、表格与正文顺序；不执行脚本或访问 URL'),
 '.htm':('HTML',None,'与 .html 相同'),
 '.eml':('邮件',None,'正文及邮件头；附件不自动展开，提示缺口'),
 '.docx':('Word','docx','Strict/Transitional 正文顺序、标题与表格；图像不自动 OCR'),
 '.xlsx':('Excel','openpyxl','完整单元格及表头；不执行公式'),
 '.pptx':('PowerPoint','pptx','标题、组合文字、表格、备注；图像不自动 OCR'),
 '.pdf':('PDF 文字层','pypdf','文字层与标准表单；空打开口令只读兼容、损坏索引可用 Poppler；无 OCR'),
 '.zip':('OpenMind 原生 ZIP',None,'仅受校验的原生导出包，不是任意文件 ZIP'),
}
OPTIONAL={'.doc','.xls','.ppt','.odt','.ods','.odp','.rtf','.epub',
          '.png','.jpg','.jpeg','.webp','.bmp','.tiff','.gif','.mhtml','.mht','.msg'}
STANDARD=set(BUILTIN)
SUPPORTED=STANDARD|OPTIONAL

def _has(name):
    if name is None:return True
    try:return find_spec(name) is not None
    except (ImportError,ValueError,ModuleNotFoundError):return False

def capability(ext):
    ext=ext.lower()
    if ext in BUILTIN:
        label,module,note=BUILTIN[ext]
        installed=_has(module)
        return {'extension':ext,'label':label,'available':installed,'adapter':'builtin',
                'state':'available' if installed else 'missing_dependency',
                'requires':module,'note':note}
    if ext in OPTIONAL:
        return {'extension':ext,'label':ext[1:].upper(),'available':False,'adapter':'optional_converter',
                'state':'conversion_unverified' if _has('docling') else 'missing_converter',
                'converter_installed':_has('docling'),'requires':'docling',
                'note':'未纳入本次原生格式验收；扩展转换是否支持须单独验证，推荐另存对应开放格式。'}
    return {'extension':ext,'available':False,'state':'unsupported','note':'尚无适配器'}

def capabilities():
    rows=[capability(x) for x in sorted(SUPPORTED)]
    return {'extensions':[r['extension'] for r in rows if r['available']],
            'known_extensions':sorted(SUPPORTED),'formats':rows,
            'docling_extra_requires_install':True,
            'notice':'可读取文字不等于理解完整。模型调用和原文覆盖情况在工作空间单独显示；不按文件扩展名决定本体类型。'}

def require_reader(ext):
    row=capability(ext)
    if row['state']=='unsupported':raise ValueError('尚无适配器处理 '+(ext or '无扩展名'))
    if row['state']=='missing_dependency':raise ValueError('此格式缺少解析依赖 '+str(row['requires'])+'；请用新版 requirements.txt 重建应用与 Worker')
    if row['state']=='missing_converter':raise ValueError('该格式需要可选转换组件且尚未验证支持；优先将旧 Office 另存为 DOCX/XLSX/PPTX，图片需另行 OCR。')
    return row
