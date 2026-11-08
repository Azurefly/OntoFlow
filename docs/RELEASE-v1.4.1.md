# OntoFlow v1.4.1 · 多格式文本读取与来源边界统一

日期：2026-11-07。基于用户收到的 `ontoflow-v140-demo.zip` 实施。**文件格式决定读取方法，不决定本体建模含义**；保留建模意图、人机协商、中文名称/英文 CODE、只构建类型而不创建实例、OpenMind 基线复用和跨页全部同意。

## 一、实际支持范围

默认依赖安装完整后，共有 **22 个内置可读扩展名**（部分是同一种格式的别名），不是承诺 22 种不同语义推理能力。

| 类别 | 扩展名 | 实际读取方式 / 边界 |
|---|---|---|
| 普通文本 | `.txt .log .sql .rst` | 全文、分段、行定位；不执行 SQL 或 RST include/raw，不依赖预定义业务目录 |
| Markdown | `.md .markdown` | 原有章节/表格/来源证据路径；保留前置标题与整行表格 |
| JSON | `.json .jsonl .ndjson` | 原生本体、显式定义、业务记录分别处理；全部叙述，不只读前几条；JSONL 指明实际错误行 |
| YAML | `.yaml .yml` | 新增安全结构化读取，支持多文档流；进入与 JSON 相同的定义/记录分流，不执行 Python 标签 |
| XML | `.xml` | 保留节点、属性、尾部文字、混合内容及 XPath；DTD/实体/外部引用不执行 |
| HTML | `.html .htm` | 保留标题、表格与正文顺序，合并/嵌套/不等列表格转待理解文本；不执行脚本或抓取网络 |
| 表格文本 | `.csv .tsv` | CSV 逗号/分号/Tab 和 TSV，支持引号、多行字段；不等列不截断；0/false 不变空 |
| 邮件 | `.eml` | 邮件头、正文、多版本正文选择；附件、图片和未读取内容明确警告，不自动递归展开附件 |
| Word | `.docx` | 保留 v1.4.0 的正文顺序、标题层级、表格、备注区域与图片覆盖警告 |
| Excel | `.xlsx` | 保留表头和单元格；公式缓存缺失时保留公式原文，不计算公式；合并/隐藏区域提示 |
| PowerPoint | `.pptx` | 新增组合形状文字、表格和讲者备注读取；图片/图表/嵌入对象及浮动阅读顺序提示 |
| PDF | `.pdf` | 读取文字层，中文文字层也测试；无字页、图片页和版式边界明确列出，不把部分读取当全文完成 |
| 原生导出 | `.zip` | 只支持经过校验的 OpenMind 原生导出包，**不是任意文件压缩包** |

`.doc/.xls/.ppt/.rtf/.odt` 以及单独图片等仍属可选转换范围。页面不会因扩展名在代码里就声称默认可用；Docling 已安装也不等于其中每种格式已通过本次验证。推荐另存 DOCX/XLSX/PPTX；扫描图片需独立 OCR。**本次没有实现自动 OCR，没有声称任意文档都能无遗漏识别。**

## 二、实际修复

1. **编码**：UTF-8/BOM、带 BOM 的 UTF-16/32、GB18030；HTML/XML 读取明确声明的受支持编码。取消“随便按无 BOM UTF-16 试解码”的行为，避免偶数字节中文被转换成无关文字。不进行有损 replacement 解码。
2. **HTML**：不再先将全部内容压成文本；标题与表格相邻关系保留。图片替代文字是替代文字，不被当成 OCR；外部页面不被请求。
3. **XML**：不再只读取 elem.text，属性值、嵌套属性、混合文字和 tail 有来源定位。实体扩展/外部文件和网络读取均被拒绝。
4. **YAML/JSON**：重复键、无限值、危险标签、循环别名、过深结构和超过限制的记录明确报错，不让后一个值无声覆盖前一个。任意但语义不明确的结构保留给模型/人工，不编造实体含义。
5. **表格**：修复原函数 `str(x or '')` 导致 0/False 丢失，修复“数据列多于表头时被切掉”。表头重复、缺失、列数不等标为 ambiguous_table，保留全部文字后协商。
6. **来源分类**：普通 JSON/YAML 记录集合推导出来的类和属性标为“建模推断”，不再冒充“原文明示的本体定义”。显式本体 JSON 仍走原有可信结构校验。
7. **上传与 UI**：新增“格式与读取边界”弹窗，按实际安装依赖给出可读状态；缺依赖在入队前返回错误，不留下伪成功记录。多文件上传单项失败不阻断其余文件，汇总成功/排队/失败。
8. **异步状态**：读取到文字但有图片、空字页、合并结构等警告的任务返回 partial，在右侧协商助手汇报边界，不再宣称全部成功。后续仍允许先审核可读内容。
9. **终态统计时序**：最终包首轮独立复测捕获既有“任务先完成、metrics 后写入”的竞争条件。修复为在提交 succeeded/partial/failed 的同一事务内保存当前已测统计，再由任务结束钩子补齐最终耗时。新增首次终态提交观察测试，不通过延长等待或删除旧断言掩盖问题；原失败日志保留在证据包中。

## 三、修改范围与持久化

新增 `app/text_io.py`、`app/markup_sources.py`、`app/office_sources.py`、`app/format_registry.py`；统一由 `app/extraction.py` 调用。`/api/formats` 现在返回 available 的扩展名、全部已知扩展名及每项状态/边界。旧的可选转换扩展名仍在 `known_extensions` 中，不再放进“当前已可读取”的列表。

`/api/projects/{pid}/understanding` 显示每个文件读取器、编码与警告。元数据仍保存在已有 chunks JSON 字段；**从 v1.4.0 升级没有数据库 Schema 变更，SCHEMA_VERSION 仍为 1.4.0，应用版本为 1.4.1-demo**。

分层缓存的读取/解释版本升级，避免直接复用旧版漏字段的解释；原先已审核本体、UUID、CODE 和人工决定不自动改写。旧文件要使用新读取器，必须执行“重新解析原始文件”，重新核对主题、生成或复核候选。

新增标准依赖为 `PyYAML` 与 `defusedxml`。仅重启旧容器不足以安装新依赖，需要 `--build`。不恢复 Demo 白名单、管理员 Key 或强制 HTTPS 检查。

## 四、实际验证

- 冻结 v1.4.0 基线：354 项通过、5 项真实 PG 测试跳过。
- 完整回归：**419 项通过、5 项真实 PostgreSQL 测试跳过、0 失败**；新增 **65 项**读取/抽取/UI/异常路径检查。
- 同一套“馆藏品、入藏事件、保管状态、参与入藏”合成材料，在普通文本、MD、JSON/YAML、XML、HTML、EML 以及 DOCX/XLSX/PPTX 表格中对照核心类型/属性/关系；CSV/TSV 以三张同义表对照。不预设真实用户文件答案。
- 异常用例：GB18030 偶数字节、0/false、超长与多行 CSV 字段、重复键、YAML 循环/危险标签、XML 外部实体、HTML 合并表格、PDF 无字页、XLSX 公式缺缓存、PPTX 组合/备注/图片、缺依赖拒绝入队、异步 partial、批量混传单项失败继续。
- 模型理解路径另用可控返回验证：含歧义的 HTML 表格与用户目标确实传给语义调用，没有把结构不明的文字强制建为类。**这验证调用边界，不是验证真实模型理解准确率。**
- Chromium 加载真实前端 JS，通过临时 SQLite / 进程内 API 验证格式弹窗、读取器和编码、图片未核实提示；无页面 JS 错误；未连接外部服务。
- 原始 BC 文件保持 22 类实体、283 项共享属性目录、373 条挂载、48 条关系；10 项类型核查与 4 个未完整单元仍保留。原生重点人员 ZIP 保持 20 类实体、137 项独立属性、183 条挂载、10 条原生关系及 15 条继承；同分支新增 0，1 项未绑定属性待协商。两份原始文件的规则路径未调用模型。

这些结果是本地读取、候选发现和模拟 API 验证，不是用户现场 PostgreSQL、OpenMind、供应商模型或未知真实材料的准确率测试。扫描页和旧二进制格式不计为完全支持。本版没有加入运行时测试答案、数据库或用户原始资料。

## 五、升级命令（原目录覆盖，保留配置/数据）

先备份 PG 和上传目录。SRC 指向新包内 `ontology-workbench`，DST 指向当前运行目录；不要倒置，不要新建另一套 Compose 数据卷。

```bash
SRC="/新版解压目录/ontology-workbench"
DST="/当前运行目录/ontology-workbench"
test -f "$SRC/app/version.py" && test -f "$DST/.env" || exit 1
cd "$DST" || exit 1
docker compose config -q || exit 1
docker compose stop ontology worker

umask 077
mkdir -p "$HOME/ontoflow-backups"
BACKUP="$HOME/ontoflow-backups/pre-v1.4.1-$(date +%Y%m%d-%H%M%S).dump"
docker compose exec -T postgres sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP" || exit 1
test -s "$BACKUP" || exit 1
docker compose exec -T postgres pg_restore --list < "$BACKUP" >/dev/null || exit 1

rsync -av --exclude='.env' --exclude='data/' --exclude='uploads/' \
  --exclude='.git/' --exclude='__pycache__/' --exclude='*.dump' "$SRC/" "$DST/"
docker compose up -d --build --no-deps --force-recreate ontology worker
curl -fsS http://127.0.0.1:8001/api/health
```

预期版本 `1.4.1-demo`。强制刷新浏览器，再点击“格式与读取边界”查看当前容器实际依赖状态。对旧解析结果执行“重新解析原始文件”，然后重新确认主题。不要重新生成 `.env` 或 PG 密码，不要执行 `docker compose down -v`。

若当前版本早于 v1.4.0，仍可能执行以前版本的增量 Schema 升级，不能把“从 v1.4.0 无变更”理解成所有旧版本都无变更。

## 六、可重复命令

在隔离验收环境运行，不在业务容器里测试：

```bash
python -m pip install -r requirements-dev.txt -c requirements-tested-constraints.txt
python -m pytest -q
node --check static/app.js
python -m scripts.ui_formats_smoke --output /tmp/ontoflow-format-ui --chromium /usr/bin/chromium
```

测试用 Office/邮件/PDF 由源码生成，都是合成材料；模型采用模拟或禁用网络的路径。原始 BC / 原生 ZIP 通过显式文件路径复测，不放入部署包。

## 七、解析组件依据

新增安全读取参考 PyYAML SafeLoader 文档（https://pyyaml.org/wiki/PyYAMLDocumentation）、Python XML 安全说明（https://docs.python.org/3/library/xml.html）及 pypdf 的文字提取边界（https://pypdf.readthedocs.io/en/stable/user/extract-text.html）。这些是组件行为依据，不是项目验收结果；项目验收以本次测试日志为准。
