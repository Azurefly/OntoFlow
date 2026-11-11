# OntoFlow v1.4.3 — 有界语义抽取、证据校验与可恢复失败

本版在 v1.4.2 的格式读取修复上继续处理真实模型输出。不是声称任何文档都能自动正确构建本体。只生成类型、中文名称及英文 CODE，人工审批和 OpenMind UUID/权限约束不变。

## 已核实的进度

v1.4.2 已发布：重复条款独立来源ID、相邻正文上下文合并、Strict DOCX、PDF只读容错/空口令读取/表单值、系统报警语境和 awaiting_model 状态。34份冻结输入中32份可读取上传，另外无文字PDF和旧DOC明确拒绝；HTTP200并不等于全文语义正确。

先前 v1.4.3 暂存源码的CI为458通过、4跳过（4项需要私有业务资料），包含真实 PostgreSQL 测试。7B及1.5B真实模型任务均被取消，最终保存结果均为0/3通过：长JSON截断、超时、未定位证据、关系端点未确认等失败均保留，不能以启动推理或CI基础作业成功代替语义验收。

## 本次代码变化

1. 语义模型响应明确每类最多6项，描述80字符、引文160字符、问题最多3项；容量不足必须声明 has_more，保留有效结果并继续拆分，不静默丢弃。
2. 语义请求只消耗一次HTTP请求预算，取消隐藏的4096→6000→6000整段重试；摘要/对话的既有通用纠错路径不变。
3. 输出截断不补括号伪造JSON。按原文边界拆分并保存检查点；最小单元仍失败时明确终止，已取得线索仍保留。
4. 无效JSON的重试次数持久化，任务重启不能无限重置预算。修复请求包含已通过校验的类型名称，只补错误/遗漏；已有候选和原文证据不删除。
5. 字段、编码、数据类型、逐字证据校验和中文排版空白对齐独立执行。模型说“已完成”不替代证据、依赖和人工审核。
6. 重复的建模澄清问题不无限追加。真实模型测试记录请求开始、返回/未返回、输入哈希、检查点和已得结果；超时不误计为0次请求。

结构化输出为明确选择的 off/json_object/json_schema，不假定每个供应商支持JSON Schema。schema中的字段边界是输出预算，不是跨领域正确答案或业务目录。英文code、quote和owner等非法字段不靠删除校验放行。

## 运行与升级

无数据库Schema变更。保留 .env、原始上传文件、PostgreSQL卷与已审核本体；先备份后在原Compose目录覆盖源码并 --build。不要重新初始化密码、不要 down -v。期望健康接口 1.4.3-demo。

`LLM_SEMANTIC_MAX_TOKENS` 默认4096，允许512–8192，是单次语义响应上限，不是文档处理时长承诺。未配置模型时正文继续保持 awaiting_model。新解释版本隔离旧的成功/失败缓存，证据仍可复用。

## 可重复验收

```bash
python -m pytest -q
node --check static/app.js
python -m scripts.repair_business_gate --inputs /path/to/frozen-inputs --output /tmp/reading-results
# 独立本机模型HTTP服务；不向用户OpenMind写入，失败结果也完整保存
python -m scripts.semantic_acceptance --inputs /path/to/frozen-inputs \
  --output /tmp/semantic-results --structured-output json_schema \
  --attempts 1 --request-timeout 180 --max-tokens 3072
```

本次模型输入沿用此前三个固定公共业务摘录，不更换为易通过的样本；摘录验证不是整份文档召回率。测试摘要、模型响应及最终是否通过以 reports/semantic-2026-11-11/ 与对应 CI artifact 为准。没有真实 OpenMind 写入授权或现场验证。
