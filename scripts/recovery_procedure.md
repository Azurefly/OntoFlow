# 故障恢复演练（仅在测试环境执行）

1. 完成 SQLite → PG 迁移，保留 `/app/data/ontology.sqlite3` 和密钥文件备份。
2. 上传一份较大文件，观察 `/api/system/status`、当前工作空间任务进度、PG `extraction_cache` 记录数。
3. 运行 `docker compose restart worker`，模拟 Worker 进程重启。请等待过期租约（默认 300 秒）被领取。
4. 查看任务恢复后已完成片段缓存是否复用，确认未新增重复待审核对象、不会把未确认候选写入正式本体。
5. 在测试 PG 数据库上模拟网络中断并恢复，确认 API 连接恢复、PG 任务状态不丢失。
6. 以 20 / 50 / 100 / 200 并发执行 `python -m scripts.bench_load --path /api/ready`，记录 P95、错误率和 PG 连接数。
7. 针对文档抽取分别记录实际 LLM tokens/s、HTTP 429 次数、任务数和 cache hit；模型服务是独立瓶颈，不应以 API QPS 推断 AI QPS。

注意：压测结果必须根据实际执行填写，未运行不能填“通过”。在单台宿主机上，重启可恢复不代表节点级高可用。
