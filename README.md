# OntoFlow

渐进式、证据约束的人机协商本体构建工具。当前应用版本 **v1.4.1-demo**。

导入材料 → 形成文档理解与主题 → 实体类型/属性定义/关系定义 → 人工协商与审批 → 对齐OpenMind既有分支 → 明确授权后写回。

**仅生成本体类型，不生成具体人员、案件或业务实例。三类对象采用中文名称和英文CODE。** 原文事实、模型推断和用户建模决定分别保存；文件格式决定读取方式，不等于内容语义已经理解。

## 本次独立互联网测试：已发现兼容缺口

[详细测试报告（29份新样本、17个扩展名）](reports/internet-2026-11-07/REPORT.zh-CN.md) · [固定来源与校验清单](reports/internet-2026-11-07/manifest.json) · [逐文件机器结果](reports/internet-2026-11-07/results.json)

- 本地既有回归 **419通过、5项真实PG测试跳过**，不是跳过后仍声称全环境通过。
- 新下载公开样本：24份返回文字（其中8份有阅读边界警告）；5份拒绝。Strict DOCX和缺startxref的PDF兼容性存在缺口；表单PDF填写值/勾选状态未完整读取。
- 未调用真实LLM，不宣称陌生领域语义抽取准确率；未向OpenMind写入。原业务样本不作为本轮新样本，也不包含在仓库。
- 应用代码保持交付版，公开测试没有通过修改算法或测试答案掩盖失败。

## 功能与边界

- TXT/MD/RST/LOG/SQL，JSON/JSONL/NDJSON，YAML，XML，HTML，CSV/TSV，EML，DOCX/XLSX/PPTX，PDF文字层，OpenMind原生ZIP。扩展名可接收不代表任意内部格式完整支持；具体边界见报告。
- 正文顺序、标题、表格和来源位置；读取图片、合并单元格、复杂版式不充分时有警告。**无自动OCR**，旧DOC/XLS/PPT需额外转换。
- 用户建模意图、来源分类、分层缓存、后台Worker、右侧协商进度；候选跨页全部同意、单项失败继续、零采纳继续。
- OpenMind原生UUID/CODE、已有定义复用、共享属性挂载及写入冲突核对；轻量编辑器不是全部原生配置的无损恢复工具。

## 全新本机Demo部署

```bash
python3 -m scripts.init_fresh_env
docker compose up -d --build
curl -fsS http://127.0.0.1:8001/api/health
```

默认绑定127.0.0.1:8001。通过页面配置模型和OpenMind账号；外部服务自身的凭据/权限仍然需要。已有工程**不要重新初始化.env或更换PG密码**。升级先备份，在原Compose目录更新源码，保留.env、上传目录和数据库卷；不要执行`docker compose down -v`。

## 安全说明

Demo模式关闭内部管理员Key及服务主机白名单，允许HTTP，因此**只能运行在可信本机/隔离网络，不能直接暴露公网**。SQL等上传内容只读取，不执行。原始材料可能含敏感信息，系统不是源文件自动脱敏服务。

本公开仓库不含真实.env、Token、密码、用户上传业务资料、运行数据库或历史业务报告。保留的测试资料为合成测试代码；新互联网原始样本只在操作者下载目录/短期CI产物中存在，不写入Git。

## 复现

```bash
python -m pip install -r requirements-dev.txt -c requirements-tested-constraints.txt
python -m pytest -q
node --check static/app.js
python scripts/fetch_external_corpus.py --manifest reports/internet-2026-11-07/manifest.json --output /tmp/ontoflow-corpus
python -m scripts.audit_external_documents --corpus /tmp/ontoflow-corpus --output /tmp/ontoflow-audit
```

真实PG门禁仅使用独立测试库：`docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from regression`。模拟远端测试不能代替用户OpenMind现场验收。

[当前发布说明](docs/RELEASE-v1.4.1.md) · [建模意图与Word上下文](docs/RELEASE-v1.4.0.md)

本次仅按用户授权公开源码；未擅自添加新的项目许可证。第三方组件和外部样本的许可证以其原始项目为准。
