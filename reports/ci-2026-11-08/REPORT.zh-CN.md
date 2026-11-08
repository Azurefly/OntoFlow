# 完整源码发布与 GitHub CI 实测记录（2026-11-08）

## 1. 已完成的仓库提交

这次按“完整源码先提交，测试报告随后提交”的顺序，直接写入 `Azurefly/OntoFlow` 的 `main`，不是只提交压缩包或采集脚本。

| 内容 | 提交 |
|---|---|
| 完整应用源码、前端、测试、部署配置、说明与核对脚本（132 个文件） | [`1fe03dc4a5a3c055eb687d2300a49dd1d08f1b10`](https://github.com/Azurefly/OntoFlow/commit/1fe03dc4a5a3c055eb687d2300a49dd1d08f1b10) |
| 互联网样本详细报告、清单、JSON 结果、测试日志等（10 个文件） | [`47967c96df82f9fbdbeeaa6843d225412e7ec27f`](https://github.com/Azurefly/OntoFlow/commit/47967c96df82f9fbdbeeaa6843d225412e7ec27f) |
| 可运行的三项 CI 作业 | [`d6533a59b6f356b9bd02975a54e300f616a7a651`](https://github.com/Azurefly/OntoFlow/commit/d6533a59b6f356b9bd02975a54e300f616a7a651) |

应用源码版本保持 `1.4.1-demo`。传输档案 SHA256 为 `c0cc8ec3b6a6337744e168557d7fe42fb39a8d03940161646cf5573a453a7381`（287,724 字节）；档案有 143 个文件，包括单独提交的 CI 工作流。源码及报告 142 个文件在提交前、Git 对象中和回读证据中逐一核验；应用逻辑没有为得到测试通过而修改。完整清单见根目录 `SOURCE_MANIFEST.json`。

[发布校验运行](https://github.com/Azurefly/OntoFlow/actions/runs/42446591967)保留各文件 SHA256 与两次提交 SHA 的证据。一次性传输与导入工作流已在成功后删除，避免后续误触发覆盖；实际业务文件直接存在于 `app/`、`static/`、`tests/`、`scripts/` 等目录，不依赖传输分片或启动时解压。

## 2. 本次真实 GitHub Actions 结果

运行：[42446652005](https://github.com/Azurefly/OntoFlow/actions/runs/42446652005)。测试提交为 `d6533a59b6f356b9bd02975a54e300f616a7a651`，时间为 2026-11-08 04:23–04:25 UTC，Python 3.13.18，GitHub Ubuntu runner。三项作业均已完成，结论为 success。后续仅删除一次性传输工作流、补充报告，没有修改应用代码或测试预期。

| 作业 | 实测结果 | 边界 |
|---|---|---|
| `local-regression` | **415 passed、9 skipped、0 failed**，45.41 秒 | 5 项 PG 在这个作业中跳过，另 4 项需要用户原始业务文件；公开仓库没有提交那些文件 |
| `real-postgresql` | **5 passed、0 skipped、0 failed**，1.74 秒 | 通过独立 PostgreSQL 16 容器和随机临时测试库实跑，不是 SQLite 模拟 |
| `frozen-internet-corpus` | **29 份、17 扩展名**；16 read、8 partial、5 rejected | 再下载固定上游提交与原始字节；失败和部分读取完整保留，作业 success 不代表所有样本成功解析 |

不能直接把本次 415 与原 419 作比较而说回归退步：原本地报告的 4 项私有样本测试有对应资料，公共 CI 没有这些资料，明确 skip；此前未执行的 5 项 PG 门禁在本次独立作业实际通过。两项自动化作业合看有 420 个不同测试通过，仍有 4 个原始业务样本测试未在公共 CI 运行。

### 真实 PostgreSQL 的五项检查

- 增量数据库结构初始化与不同文件的解析任务唯一键。
- 多个消费者并发领取时，同一任务仅被一个消费者领取。
- 租约被接管后的旧 Worker 提交限制和心跳行为。
- 租约过期后的提交回滚。
- 独立同步客户端之间的数据库同步锁。

存在 psycopg_pool 默认 open 参数将变化的弃用警告，不是本次失败。这五项通过不表示真实网络分区、生产高可用、吞吐和全部数据库故障均已验收，也未操作用户现有 PostgreSQL 数据库。

### 互联网样本复跑

本轮样本清单 SHA256：`5c623c5ba4eaf8eec66813c0e1652d6aef65592f1ee8d96bc64723dec9f8afbe`。29 份文件全部核对下载内容 SHA256 和 Git blob SHA，没有依据输出结果替换样本。API 结果保持 24×HTTP 200、4×HTTP 422、1×HTTP 415；规则路径没有真实模型请求，应用代码运行前后哈希相同。整个外部审计作业的 Python 测量为 14.366 秒，不是模型推理或生产端到端耗时。

原始报告中的 Strict DOCX、缺 startxref 的 PDF、表单 PDF 填写值/勾选内容读取不足，以及旧 DOC 默认不支持等边界仍然存在，本次没有通过删测试或换输入掩盖。详见[逐文件互联网测试报告](../internet-2026-11-07/REPORT.zh-CN.md)。

## 3. 证据产物

运行页面可下载：

- `regression-results`，artifact ID `13603344336`：完整测试日志与 JUnit XML。
- `real-postgresql-results`，artifact ID `13603687141`：真实 PG 五项结果及 JUnit XML。
- `external-corpus-audit`，artifact ID `13603826862`：下载清单、29 项逐文件结果、源代码哈希和 summary。

核验摘要：

| 文件 | SHA256 |
|---|---|
| regression.log | `10ef3fb97393ba896058b24af1ffe469853e833a0c761107bd81197fb639df97` |
| regression.xml | `f44bdff801026f11f4fbd6edef2e56de5e642404a67219c3d5f64260b0595b6e` |
| postgresql.log | `ea6b8befa0d22b233b2afe3bcb57bf78baf57a513a277e2d982cc534492c8f33` |
| postgresql.xml | `13f1b6cb8ab874f7a64b0ba98caf1a50dcb7f3afaff0094ca279403b88221e8a` |
| public results.json | `cabea744b245a2da960b70a1f9bef4b04f072aa2e9682b3dc186ed319df9d3eb` |
| public summary.json | `fb27ff1b10093d77be1ea9116d5bd4fa6b39f8556da136bd120251ae159cd221` |

GitHub 产物有保留期限，原 2026-11-07 完整报告、清单和结果已永久提交到仓库，后续也可通过固定清单重新下载复测。

## 4. 未做的事

没有将用户 `.env`、密码、API Key、运行数据库、上传文件、私有业务文档或第三方原始测试素材放进公共仓库。没有真实 LLM 语义精确率/召回率测试，没有向用户 OpenMind 服务执行写入；真实 PG 测试仅限 GitHub runner 临时容器。源码发布完成与任意格式语义正确性是不同结论。
