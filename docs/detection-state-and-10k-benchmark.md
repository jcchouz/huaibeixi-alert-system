# 检测状态持久化与 1 万行/秒压测

## 本次改造

生产检测链路新增三张内部表：

- `t_detection_channel_state`：保存每个通道的连续命中计数、方向、开始时间、在线状态和条件是否活跃。
- `t_detection_worker_state`：保存实时预警和统计任务的处理水位。
- `t_detection_rule_version`：保存阈值规则版本，由 `t_alert_threshold` 语句级触发器递增。

状态更新和 `t_alert_record` / `t_stat_record` 写入位于同一个数据库事务中。事务提交后才更新进程内存，避免“数据库回滚但内存已经前进”。

## 生命周期语义

- 正常条件第一次成立：创建未处理告警。
- 活跃条件下等级升高：更新同一条告警并增加 `trigger_count`。
- 活跃条件恢复：持久化状态改为不活跃，人工处理状态仍由现有业务流程管理。
- 恢复后再次成立：即使等级相同，也会刷新已有未处理记录；如果旧记录已经被处理，则创建新记录。
- 进程重启或主备切换：从 `t_detection_channel_state` 恢复连续命中和条件状态。
- 所有阈值、量程和统计判定直接使用 `t_monitor.value` 原始值，不再计算或同步直流分量。

## 规则同步

每次新增、修改或删除 `t_alert_threshold` 都会触发规则版本递增。检测主节点默认每 5 秒检查一次版本：

```dotenv
DETECTION_RULE_REFRESH_SECONDS=5
```

这样，即使阈值修改请求落在非检测主节点，检测主节点也会在刷新周期内重新加载阈值。

## 水位与追赶

实时预警默认最多追赶 300 秒历史，并按 5 秒窗口处理：

```dotenv
DETECTION_MAX_ALERT_CATCHUP_SECONDS=300
DETECTION_ALERT_CATCHUP_CHUNK_SECONDS=5
```

统计窗口也从持久化水位继续处理，并继续受 `DETECTION_MAX_CATCHUP_WINDOWS` 限制。

## 集成测试

集成测试会直接连接 `TEST_DATABASE_URL`，使用 `itest-det-` 前缀创建临时规则和采样，结束后按时间范围和前缀清理。

```powershell
cd backend
$env:DEBUG = "false"
$env:TEST_DATABASE_URL = "host=... port=5432 dbname=... user=... password=..."
.\\.venv\\Scripts\\python.exe -m pytest -o addopts="" -m integration tests/test_detection_integration.py -q
```

当前覆盖：

- 状态持久化和新引擎恢复。
- 阈值触发、恢复和再次发生。
- 阈值变更后的规则版本同步。
- Worker 水位写入和读取。

## 1 万行/秒压测

```powershell
cd backend
$env:DEBUG = "false"
.\\.venv\\Scripts\\python.exe tools/benchmark_10k.py `
  --rows-per-second 10000 `
  --seconds 10 `
  --sensors 10 `
  --channels 4 `
  --batch-size 10000
```

压测写入 `bench10k-` 前缀数据，结束后自动清理。

| 环境 | 检测周期 | 回看 | 写入速率 | 唯一数据吞吐 | SQL 读取行数 | 最大稳态耗时 | 结论 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 本机 loopback | 1 秒 | 0 秒 | 9,910 行/秒 | 137,820 行/秒 | 100,000 | 0.102 秒 | 通过 |
| 本机 loopback | 1 秒 | 1 秒 | 9,746 行/秒 | 104,404 行/秒 | 190,000 | 0.098 秒 | 通过 |

本机 loopback 等价于低延迟局域网数据库。10 万条数据在不到 1 秒内完成
全部实时扫描，写入也达到 1 万行/秒目标。1 秒回看会读取 19 万行，但仍然
能在单个 1 秒周期内完成。

公网数据库测试不作为代码容量结论：服务端执行 1 万行查询约 18ms，但当前
公网链路客户端接收 1 万行需要数十秒，结果会随外网质量剧烈变化。

如果采集端也必须稳定达到 1 万行/秒，应继续做以下测试：

- Java 采集端使用批量写入或 PostgreSQL `COPY`。
- 采集端与分析端部署在同一内网或同机房。
- 使用连接池并减少每批提交次数。
- 对 `t_monitor(time)` 和生产常用查询建立/确认索引。
- 当前数据写入约定按时间顺序提交采样，默认使用
  `DETECTION_ALERT_OVERLAP_SECONDS=0`。如果后续必须容忍迟到事务，需要
  重新配置回看窗口，或增加独立的迟到数据通道。
