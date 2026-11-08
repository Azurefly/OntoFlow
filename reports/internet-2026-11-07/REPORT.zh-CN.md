# 互联网新采样独立测试报告（2026-11-07）

## 结论

本次冻结 OntoFlow v1.4.1 的应用代码，以刚从公开上游下载的文件做独立测试。没有用 BC 文档、重点人员原生包或模型预设答案作为外部测试输入，也没有针对新失败修改解析算法。**“既有回归通过”与“新格式样本通过”分别报告。**

- 既有自动化回归：**419 passed / 5 skipped / 0 failed**，45.85 秒；5 项跳过是未配置的真实 PostgreSQL 集成测试，不计入通过。
- 新采样：**29 份文件、17 个扩展名**；公开源原始字节合计 562,442。实际上传成功 **24 份**，拒绝 **5 份**。
- 读取状态：16 份返回可读文本且未报告解析警告；8 份有阅读边界警告；5 份拒绝。无警告并不等于语义内容完整。
- 独立文字保留对照覆盖18份成功读取文件，其中17份检查的文字单元全部保留，1份表单PDF为4/6；其余格式没有做全量独立文字保留判定。
- 外部样本实际 API：24×HTTP 200、4×HTTP 422、1×HTTP 415。每个样本使用新的临时SQLite工作空间。
- 规则抽取保留59项待核查；未调用真实模型。没有配置模型的路径返回0候选，**不能解读为原文不存在本体概念，也不是LLM准确率通过**。
- 应用及原包169个交付文件检查无改动；新增了独立核验脚本。源哈希和原ZIP摘要保留在证据目录。

## 方法、来源与计时

采集器预先选定上游仓库及路径，优先公开的样本文件名，没有匹配时取大小最小的合格文件；不使用 OntoFlow 提取数量选择样本。固定每个仓库当时的 commit，并记录原始路径、Git blob SHA、SHA256、字节数和来源URL。运行前分别核验两种摘要。CPython邮件文件仅将后缀从.txt改为.eml，字节未修改。其余原始字节不改动。

下载由本仓库 GitHub Actions 完成（首次脚本尚未提交的启动运行失败；后续采集运行成功），本地从该真实工作流artifact读取。公开样本不是用户原始业务文件，也不能声称它们全部是自然产生的业务材料：上游测试夹具中有合成样例。

规则抽取使用空数据库，禁止真实模型请求，不预装任何测试答案；摘要/原生读数/候选数量与语义精度分开。单文件解析时间不含下载、PG、OpenMind HTTP、LLM推理或人工审批。独立文字检查只判定可读文本保留，不判断图片内容、表格语义、关系正确性或整体本体召回率。

### 按文件明细

| 样本ID | 格式 | 字节 | 读取状态 | 单次解析毫秒 | 片段 | 上传HTTP | 独立文字保留 | 规则E/A/R（未审） | 待核查 |
|---|---|---:|---|---:|---:|---:|---|---|---:|
| docling_docx-1 | .docx | 29578 | partial | 35.077 | 22 | 200 | 21/21 | 0/0/0 | 0 |
| docling_docx-2 | .docx | 11923 | rejected | 0.000 | — | 422 | 未测/未进入 | —/—/— | — |
| docling_docx-3 | .docx | 11932 | read | 1.458 | 1 | 200 | 1/1 | 0/0/0 | 0 |
| docling_pptx-1 | .pptx | 45849 | partial | 41.922 | 13 | 200 | 25/25 | 0/0/0 | 3 |
| docling_pptx-2 | .pptx | 48107 | partial | 3.808 | 2 | 200 | 2/2 | 0/0/0 | — |
| docling_html-1 | .html | 680 | partial | 2.914 | 11 | 200 | 未测/未进入 | 0/0/0 | 1 |
| docling_html-2 | .html | 245014 | partial | 34.095 | 422 | 200 | 未测/未进入 | 0/0/0 | 2 |
| docling_xml-1 | .xml | 4778 | read | 1.723 | 228 | 200 | 79/79 | 0/0/0 | 0 |
| pdfminer_pdf-1 | .pdf | 849 | rejected | 0.000 | — | 422 | 未测/未进入 | —/—/— | — |
| pdfminer_pdf-2 | .pdf | 1174 | rejected | 0.000 | — | 422 | 未测/未进入 | —/—/— | — |
| pdfminer_pdf-3 | .pdf | 1051 | rejected | 0.000 | — | 422 | 未测/未进入 | —/—/— | — |
| pandas_csv-1 | .csv | 4600 | read | 1.859 | 2 | 200 | 82/82 | 0/0/0 | 2 |
| pandas_csv-2 | .csv | 7943 | read | 1.916 | 3 | 200 | 367/367 | 0/0/0 | 3 |
| pandas_xlsx-1 | .xlsx | 12074 | partial | 53.120 | 4 | 200 | 5/5 | 0/0/0 | 3 |
| pandas_xlsx-2 | .xlsx | 8067 | read | 4.535 | 1 | 200 | 4/4 | 0/0/0 | 1 |
| openapi_yaml-1 | .yaml | 2772 | read | 18.632 | 12 | 200 | 结构清点，非全文对照 | 1/1/0 | 0 |
| openapi_json-1 | .json | 385 | partial | 0.197 | 8 | 200 | 结构清点，非全文对照 | 0/0/0 | 0 |
| cpython_eml-1 | .eml | 459 | read | 1.417 | 2 | 200 | 3/3 | 0/0/0 | 0 |
| cpython_eml-2 | .eml | 2812 | read | 3.792 | 9 | 200 | 20/20 | 0/0/0 | 0 |
| cpython_rst-1 | .rst | 278 | read | 0.250 | 1 | 200 | 13/13 | 0/0/0 | 0 |
| cpython_xml-1 | .xml | 122 | read | 0.210 | 4 | 200 | 3/3 | 0/0/0 | 0 |
| jieba_chinese_md-1 | .md | 31879 | read | 2.335 | 55 | 200 | 413/413 | 0/0/0 | 31 |
| postgres_sql-1 | .sql | 6144 | read | 0.369 | 2 | 200 | 160/160 | 0/0/0 | 0 |
| pdfminer_text-1 | .txt | 2916 | read | 0.180 | 1 | 200 | 45/45 | 0/0/0 | 0 |
| vega_tsv-1 | .tsv | 34739 | read | 13.419 | 11 | 200 | 3427/3427 | 0/0/0 | 11 |
| jsonl_algebra-1 | .jsonl | 1631 | read | 0.602 | 7 | 200 | 结构清点，非全文对照 | 1/5/0 | 1 |
| jsonl_algebra-2 | .jsonl | 15495 | read | 2.140 | 6 | 200 | 结构清点，非全文对照 | 1/4/0 | 1 |
| docling_complex_pdf-1 | .pdf | 2055 | partial | 1.641 | 1 | 200 | 4/6 | 0/0/0 | 0 |
| docling_legacy_doc-1 | .doc | 27136 | rejected | 0.000 | — | 415 | 未测/未进入 | —/—/— | — |

### 状态解释

`read`仅表示当前读取器返回非空文字且无解析警告；`partial`表示有图片/图表/复杂表格等明确警告；`rejected`表示读取器拒绝该样本。E/A/R为规则模式下的去重线索数量，不是人工金标准。CSV/TSV普通数据表、英文说明和自然叙述可能没有显式中文本体定义，因此零候选不算成功抽取。

## 新发现：不能被“419项回归通过”掩盖的问题

### 1. Strict DOCX 兼容缺口

`docling_docx-2`是上游Strict.docx。python-docx读取路径报缺少officeDocument关系，API返回422；stdlib OOXML XML读取能找到正文。同一上游的Transitional.docx正文文本SHA256相同，当前应用读取成功。这是格式兼容缺口，不应归因于“Word没有内容”。

### 2. 两份PDF缺失startxref，当前读取器拒绝

`pdfminer_pdf-1/3`的原始字节没有startxref。OntoFlow/pypdf返回422；独立Poppler可以容错读取并得到70/36个字符。结果表明当前实现缺少此类修复/降级路径，不代表所有PDF失败，也不应为通过测试改写源文件。

### 3. 表单PDF有可见内容未进入解析结果

`docling_complex_pdf-1`实际选中的是上游acroform_sample.pdf，而非研究论文。独立Poppler和页面渲染可见填入的姓名及勾选符号；当前文字层结果保留4/6个文本单元，遗漏填写值/状态。只有笼统版式警告，没有表单字段级别的遗漏说明。不能将HTTP200说成该PDF完整支持。

### 4. 两类预期边界

`pdfminer_pdf-2`独立Poppler也没有提取到文字，当前返回“未读取到文字”是有效边界，不能冒充OCR成功。`docling_legacy_doc-1`是旧二进制DOC，默认未装可选转换组件，返回415，属于已声明的默认不支持路径。

### 5. 模型语义验证未执行

公开样本没有人工标注的本体金标准，本次不统计虚假的precision/recall。真实LLM、真实OpenMind写入和用户现场数据库均未调用；因此只能说明解析、证据保留、规则行为和API是否按预期拒绝/提示，不能说明任意业务材料的语义提取质量已经通过。

## 采集缺项

首次在pandas路径未找到TSV和JSONL，采集失败被保留；后续从Vega和jsonl-algebra下载了真实对应文件，不自行伪造替代样本。它们的下载来源和sha在manifest中可逐项复核。

## 可重复执行

```bash
python -m pip install -r requirements-dev.txt -c requirements-tested-constraints.txt
python -m pytest -q --junitxml=/tmp/ontoflow-unit.xml
node --check static/app.js
python scripts/fetch_external_corpus.py --manifest reports/internet-2026-11-07/manifest.json --output /tmp/ontoflow-public-corpus
python -m scripts.audit_external_documents --corpus /tmp/ontoflow-public-corpus --output /tmp/ontoflow-public-audit
```

下载脚本固定源commit及SHA，不重新随机抽样。重新选取新样本可运行scripts/collect_public_fixtures.py并把新清单作为独立批次。公网源未来可能移除文件；采集失败必须报告，不能悄悄换成别的文件。

## 逐份来源

仓库许可证信息只是定位线索，不保证每个嵌入第三方素材均可任意再分发。本仓库**只提交下载清单、复现脚本和结果，不提交原始外部样本**。原始互联网样本通过短期GitHub Actions artifact提供，第三方版权归各自权利人。

### docling_docx-1
- 原始路径：`docling-project/docling/tests/data/docx/sources/unit_test_formatting.docx`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/docx/sources/unit_test_formatting.docx
- SHA256：`23518f8c635264b038b3e1207292f99cb5d40be35dbcd85d56508d3226ad9a11`
- Git blob：`d4e193c3f27e5bc8dbdce4683c6ecb509972db09`；原仓库license标签：`MIT`

### docling_docx-2
- 原始路径：`docling-project/docling/tests/data/docx/sources/Strict.docx`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/docx/sources/Strict.docx
- SHA256：`87742da12fecf8301faa898fb457b9045de462128523011f0d0658f6b5e8fbe3`
- Git blob：`336c624ce87fde38166432c857fa384da54a4ff7`；原仓库license标签：`MIT`

### docling_docx-3
- 原始路径：`docling-project/docling/tests/data/docx/sources/Transitional.docx`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/docx/sources/Transitional.docx
- SHA256：`52a3f5d4866150709ed8d73cc31c6133abb83f2db4c7e93fec8cf9ea7459903d`
- Git blob：`7cb5aa0fe0e22e30a9b8a7d43cac2e1bc742e87c`；原仓库license标签：`MIT`

### docling_pptx-1
- 原始路径：`docling-project/docling/tests/data/pptx/sources/powerpoint_sample.pptx`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/pptx/sources/powerpoint_sample.pptx
- SHA256：`24191d47fda511e11984e875f8676506da5ae1fbcaefcc63d81be377d3a3950c`
- Git blob：`0818f283bed6ee82d09ceb98f07da9fe3f01cbb2`；原仓库license标签：`MIT`

### docling_pptx-2
- 原始路径：`docling-project/docling/tests/data/pptx/sources/pptx_chart.pptx`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/pptx/sources/pptx_chart.pptx
- SHA256：`c0bd75bbf8ac10380bc2c3bedd752564ef82030a587f85ca5235060edaf918dd`
- Git blob：`08ea13cafed72ab831867cc29b43295a15793299`；原仓库license标签：`MIT`

### docling_html-1
- 原始路径：`docling-project/docling/tests/data/html/sources/example_01.html`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/html/sources/example_01.html
- SHA256：`d83caa8e29ab48148ae323c5e96a8889c56d7b84fced8fd82d0bf6e577c9b72c`
- Git blob：`065c4f2543ae7794cbb2f819460587002263944b`；原仓库license标签：`MIT`

### docling_html-2
- 原始路径：`docling-project/docling/tests/data/html/sources/wiki_duck.html`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/html/sources/wiki_duck.html
- SHA256：`a41f163af380f1ebab4b514f8c62a990557347d7b154bc8e7151894431483e20`
- Git blob：`7d0896e746c643fae7ef300b1f8dc6362ce1ed9e`；原仓库license标签：`MIT`

### docling_xml-1
- 原始路径：`docling-project/docling/tests/data/uspto/sources/tables_ipa20180000016.xml`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/uspto/sources/tables_ipa20180000016.xml
- SHA256：`3531a9748d5a525e24e33639cdadd0b342694139a212f356059393c5dc5f1885`
- Git blob：`281336492780e5846b1ff4dc8812e0dafc326658`；原仓库license标签：`MIT`

### pdfminer_pdf-1
- 原始路径：`pdfminer/pdfminer.six/samples/simple1.pdf`
- 固定提交：`a18de2a9c479b4c847538500017b449ddaec177e`
- 来源：https://raw.githubusercontent.com/pdfminer/pdfminer.six/a18de2a9c479b4c847538500017b449ddaec177e/samples/simple1.pdf
- SHA256：`a21f0df056bbd133cbf987859d9934c6b37ec8c0cd484f93dd233e012e4ca320`
- Git blob：`77b8623bb637cbeaa79296e58c7fa2cc8eefa627`；原仓库license标签：`MIT`

### pdfminer_pdf-2
- 原始路径：`pdfminer/pdfminer.six/samples/simple2.pdf`
- 固定提交：`a18de2a9c479b4c847538500017b449ddaec177e`
- 来源：https://raw.githubusercontent.com/pdfminer/pdfminer.six/a18de2a9c479b4c847538500017b449ddaec177e/samples/simple2.pdf
- SHA256：`dfd271205e2be7421d265ef7974b54ac3bb4aeb9e040c0d644220bb3c7dedcd3`
- Git blob：`9619ffe6081e52d86b9af1cbb2e889ed3c210839`；原仓库license标签：`MIT`

### pdfminer_pdf-3
- 原始路径：`pdfminer/pdfminer.six/samples/simple3.pdf`
- 固定提交：`a18de2a9c479b4c847538500017b449ddaec177e`
- 来源：https://raw.githubusercontent.com/pdfminer/pdfminer.six/a18de2a9c479b4c847538500017b449ddaec177e/samples/simple3.pdf
- SHA256：`1a86f259208896a670930e65a01636b3c628619309d37a0f7f3ad1aa3417ded0`
- Git blob：`8c4f6487c808d2d9f867cbe0eeb57950dbdb0b1c`；原仓库license标签：`MIT`

### pandas_csv-1
- 原始路径：`pandas-dev/pandas/pandas/tests/io/data/csv/iris.csv`
- 固定提交：`831f55cdb3662a396587a0553c94a24bcefc9c6f`
- 来源：https://raw.githubusercontent.com/pandas-dev/pandas/831f55cdb3662a396587a0553c94a24bcefc9c6f/pandas/tests/io/data/csv/iris.csv
- SHA256：`904d791b308aa79bf4f06669f63be3f1ab331a23bd508051065e00f700f573f9`
- Git blob：`c19b9c36885151fc5a15b40577946f0c4352ad97`；原仓库license标签：`BSD-3-Clause`

### pandas_csv-2
- 原始路径：`pandas-dev/pandas/pandas/tests/io/data/csv/tips.csv`
- 固定提交：`831f55cdb3662a396587a0553c94a24bcefc9c6f`
- 来源：https://raw.githubusercontent.com/pandas-dev/pandas/831f55cdb3662a396587a0553c94a24bcefc9c6f/pandas/tests/io/data/csv/tips.csv
- SHA256：`22415aaf1e56e675b9a0983cb0d321697dad51f6060a44fb8ecaad7a00de9a09`
- Git blob：`856a65a69e64754130340a15e2b9c68d7ab8fa6f`；原仓库license标签：`BSD-3-Clause`

### pandas_xlsx-1
- 原始路径：`pandas-dev/pandas/pandas/tests/io/data/excel/test1.xlsx`
- 固定提交：`831f55cdb3662a396587a0553c94a24bcefc9c6f`
- 来源：https://raw.githubusercontent.com/pandas-dev/pandas/831f55cdb3662a396587a0553c94a24bcefc9c6f/pandas/tests/io/data/excel/test1.xlsx
- SHA256：`b6fe2d9ae5afd6d0bdfefbae9c0c1649d8bf883e183a153cb76f2a8a548cef11`
- Git blob：`862574e05a114240e403a653b7fe22d2feb458b3`；原仓库license标签：`BSD-3-Clause`

### pandas_xlsx-2
- 原始路径：`pandas-dev/pandas/pandas/tests/io/data/excel/test2.xlsx`
- 固定提交：`831f55cdb3662a396587a0553c94a24bcefc9c6f`
- 来源：https://raw.githubusercontent.com/pandas-dev/pandas/831f55cdb3662a396587a0553c94a24bcefc9c6f/pandas/tests/io/data/excel/test2.xlsx
- SHA256：`1910a60747bd1a52bfd046973ff5df2a83bcbf57199920f29aac0606e9f21cef`
- Git blob：`94dd951e0bb84dd566dc9aa58c89067771f49a8d`；原仓库license标签：`BSD-3-Clause`

### openapi_yaml-1
- 原始路径：`OAI/OpenAPI-Specification/_archive_/schemas/v3.0/pass/petstore.yaml`
- 固定提交：`4f86429f134f7e48b6351563549b4502fc69de52`
- 来源：https://raw.githubusercontent.com/OAI/OpenAPI-Specification/4f86429f134f7e48b6351563549b4502fc69de52/_archive_/schemas/v3.0/pass/petstore.yaml
- SHA256：`598136cb904e17e8eeead51ae33dd8d401fdff455d2d74f3869c4aa5f2742266`
- Git blob：`7ed987ff63e423bf492e005ffb59c07a0d2caab9`；原仓库license标签：`Apache-2.0`

### openapi_json-1
- 原始路径：`OAI/OpenAPI-Specification/_archive_/schemas/v1.2/resourceObject.json`
- 固定提交：`4f86429f134f7e48b6351563549b4502fc69de52`
- 来源：https://raw.githubusercontent.com/OAI/OpenAPI-Specification/4f86429f134f7e48b6351563549b4502fc69de52/_archive_/schemas/v1.2/resourceObject.json
- SHA256：`29dab26d6553ee7f4f2c3eb03521fa71302d1f512d5204cb7028564b0c78e925`
- Git blob：`a88e83f562d8ae38086c8dc9f50b34aba83a12c3`；原仓库license标签：`Apache-2.0`

### cpython_eml-1
- 原始路径：`python/cpython/Lib/test/test_email/data/msg_01.txt`
- 固定提交：`cbaba022cf2778c5f02445ae231a8af1f1db53a1`
- 来源：https://raw.githubusercontent.com/python/cpython/cbaba022cf2778c5f02445ae231a8af1f1db53a1/Lib/test/test_email/data/msg_01.txt
- SHA256：`c15a3a17f6b65e9c51c58ed3a79d12bc517f867321ed118e5dc7b5c3a1ed7d4b`
- Git blob：`7e33bcf96afc57f23fdffd2024468799d10663e7`；原仓库license标签：`NOASSERTION`

### cpython_eml-2
- 原始路径：`python/cpython/Lib/test/test_email/data/msg_02.txt`
- 固定提交：`cbaba022cf2778c5f02445ae231a8af1f1db53a1`
- 来源：https://raw.githubusercontent.com/python/cpython/cbaba022cf2778c5f02445ae231a8af1f1db53a1/Lib/test/test_email/data/msg_02.txt
- SHA256：`05d5e533f5e590d9ee2c7692d26dc87ccbf381f4831cca3362baf596691a55bb`
- Git blob：`5d0a7e16c8251f3de3c980ed453da5e26bb6be2d`；原仓库license标签：`NOASSERTION`

### cpython_rst-1
- 原始路径：`python/cpython/Doc/faq/index.rst`
- 固定提交：`cbaba022cf2778c5f02445ae231a8af1f1db53a1`
- 来源：https://raw.githubusercontent.com/python/cpython/cbaba022cf2778c5f02445ae231a8af1f1db53a1/Doc/faq/index.rst
- SHA256：`3cf75d195de125769cc55b8b9c3d0a8cf1653d0aa3538fd4e0738f7eea04e56c`
- Git blob：`46ed3db2203d3a4f8b071281d058723336f2645c`；原仓库license标签：`NOASSERTION`

### cpython_xml-1
- 原始路径：`python/cpython/Lib/test/xmltestdata/simple.xml`
- 固定提交：`cbaba022cf2778c5f02445ae231a8af1f1db53a1`
- 来源：https://raw.githubusercontent.com/python/cpython/cbaba022cf2778c5f02445ae231a8af1f1db53a1/Lib/test/xmltestdata/simple.xml
- SHA256：`24b3c8e971a76d61cbf6a1bc9f9806c2b8e651a3aae2978c21a5654e98a3ff33`
- Git blob：`b88c2c7e69a0887eff79d18026b62bfaff4c5fd4`；原仓库license标签：`NOASSERTION`

### jieba_chinese_md-1
- 原始路径：`fxsjy/jieba/README.md`
- 固定提交：`67fa2e36e72f69d9134b8a1037b83fbb070b9775`
- 来源：https://raw.githubusercontent.com/fxsjy/jieba/67fa2e36e72f69d9134b8a1037b83fbb070b9775/README.md
- SHA256：`d6754b68da8fae70d2fb93b8100f41d55e2c0c262230c1017a053ad8154d85c1`
- Git blob：`ee2ed8aac14264134918eb5abebdd811a6bd63ca`；原仓库license标签：`MIT`

### postgres_sql-1
- 原始路径：`postgres/postgres/src/test/regress/sql/create_misc.sql`
- 固定提交：`0607aa61090ac1b574f8bafe854c684195b31e16`
- 来源：https://raw.githubusercontent.com/postgres/postgres/0607aa61090ac1b574f8bafe854c684195b31e16/src/test/regress/sql/create_misc.sql
- SHA256：`c80d56c083aafad268a8c896d9a8f086dc59f875e4ab7fd5ab5e1ec112731f01`
- Git blob：`6fb9fdab4c1d820c5a8985dde8bfb1360acfc4a1`；原仓库license标签：`NOASSERTION`

### pdfminer_text-1
- 原始路径：`pdfminer/pdfminer.six/cmaprsrc/README.txt`
- 固定提交：`a18de2a9c479b4c847538500017b449ddaec177e`
- 来源：https://raw.githubusercontent.com/pdfminer/pdfminer.six/a18de2a9c479b4c847538500017b449ddaec177e/cmaprsrc/README.txt
- SHA256：`fa4f600f27b26ba68df5a392058c9f0c00bd39a726953e03bca8223a12da98be`
- Git blob：`ae63479f4309b94abf9c556a1bcb1b9a810843eb`；原仓库license标签：`MIT`

### vega_tsv-1
- 原始路径：`vega/vega-datasets/data/unemployment.tsv`
- 固定提交：`5448e25297c8f705be78e879d5423d55d04dc6f1`
- 来源：https://raw.githubusercontent.com/vega/vega-datasets/5448e25297c8f705be78e879d5423d55d04dc6f1/data/unemployment.tsv
- SHA256：`f82bff0a9745cc9e9997c0b83a02ecc77cea7b1d6acbbc4b404bff293e95bb6e`
- Git blob：`d1aca19c4821fdc3b4270989661a1787d38588d0`；原仓库license标签：`未明确`

### jsonl_algebra-1
- 原始路径：`queelius/jsonl-algebra/examples/companies.jsonl`
- 固定提交：`382b83bafa8cc83506c48eafe81e3d0903a60c74`
- 来源：https://raw.githubusercontent.com/queelius/jsonl-algebra/382b83bafa8cc83506c48eafe81e3d0903a60c74/examples/companies.jsonl
- SHA256：`5d8399cbe0258bac6012468acab1d3ece02e1b4645377349ef282820dec162da`
- Git blob：`ac376babdbf257a049c4a26be8eff649994f7dfd`；原仓库license标签：`MIT`

### jsonl_algebra-2
- 原始路径：`queelius/jsonl-algebra/examples/people.jsonl`
- 固定提交：`382b83bafa8cc83506c48eafe81e3d0903a60c74`
- 来源：https://raw.githubusercontent.com/queelius/jsonl-algebra/382b83bafa8cc83506c48eafe81e3d0903a60c74/examples/people.jsonl
- SHA256：`ea1bade4e270c62cfe1b6cc75d6beb44ed00ab90e7a2d3355dd2d9e504bab3b3`
- Git blob：`e3853e9615c1eb627e0b9c20ab037abd13ddd0a4`；原仓库license标签：`MIT`

### docling_complex_pdf-1
- 原始路径：`docling-project/docling/tests/data/pdf/sources/acroform_sample.pdf`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/pdf/sources/acroform_sample.pdf
- SHA256：`a4c3afbd5f7bf5d9ba7c13b84772e072f451b275bf4a7f05f6750b9a46548ddc`
- Git blob：`1c1e5d1c7a16ce8cc7be947136824f3ac463d816`；原仓库license标签：`MIT`

### docling_legacy_doc-1
- 原始路径：`docling-project/docling/tests/data/doc/sources/greek_heading_styles.doc`
- 固定提交：`bcb3678fad7426549643f54c5eceab77925ac5c1`
- 来源：https://raw.githubusercontent.com/docling-project/docling/bcb3678fad7426549643f54c5eceab77925ac5c1/tests/data/doc/sources/greek_heading_styles.doc
- SHA256：`c1ed32d5b26155c6d612e2149e7bcaa74c1f1c8093f31777b6a508c92c0cc950`
- Git blob：`f7b312f19985c173414644bf768e2712b07c4ca3`；原仓库license标签：`MIT`

