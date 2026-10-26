# 真实规则全预警压测报告

- 测试日期：2026-10-26
- 测试库：`huaibeixi_db_test`
- 站点：`YHZ`
- 检测入口：`AlertDetectionWorker`
- 测试工具：`backend/tools/stress_real_alerts.py`
- 完整机器可读结果：[stress-real-alerts-2026-10-26.json](stress-real-alerts-2026-10-26.json)

## 测试范围

- 137 条真实阈值规则，137 个真实 `sensor_no:channel`
- 7 台真实采集仪
- 阈值超限、超物理量程、传感器死值、传感器离线、采集仪离线
- 实时预警和 1 分钟统计线程同时运行
- 不创建临时传感器、通道、阈值或采集仪

预期独立预警键共 555 条：

| 预警类型 | 数量 |
| --- | ---: |
| 阈值超限 | 137 |
| 超物理量程 | 137 |
| 传感器死值 | 137 |
| 传感器离线 | 137 |
| 采集仪离线 | 7 |

## 最终通过结果

- 测试时间范围：`2026-10-26 10:12:26.772` 至 `2026-10-26 10:18:27.330`
- 监测数据时间范围：`2026-10-26 10:12:36.845` 至 `2026-10-26 10:15:25.808`
- 写入并回读：`138,978 / 138,978` 行
- 预警校验：`555 / 555`，无缺失、无额外预警键
- 统计校验：`137 / 137` 个通道
- 实时扫描最大耗时：`129.54 ms`
- 最大积压：`1.012 秒`
- `lastError`、`lastAlertError`、`lastStatisticsError`：均为空
- 结论：通过

同一测试窗口内产生了多次条件恢复和重新触发记录，因此 `t_alert_record` 的原始行数
大于 555；555 是当前真实规则可形成的独立活动预警键数量。

## 数据保留

按项目约束，测试数据未清理。本次调试和最终成功轮次的时间范围如下：

| 轮次 | 时间范围 | 监测行数 | 结果 |
| --- | --- | ---: | --- |
| 调试 1 | `2026-10-26 09:46:07.730` - `09:52:05.679` | 138,978 | 工具游标校验失败 |
| 手工复现 | `2026-10-26 09:54:03.949` - `09:54:07.139` | 320 | `accl-01:x` 阈值复现 |
| 调试 2 | `2026-10-26 09:56:39.972` - `10:02:38.753` | 138,978 | 事务快照校验失败 |
| 调试 3 | `2026-10-26 10:05:00.058` - `10:10:14.563` | 138,978 | 分页批次导致死值缺失 |
| 最终通过 | `2026-10-26 10:12:26.772` - `10:18:27.330` | 138,978 | 全部通过 |

所有轮次使用的真实通道清单见 JSON 报告的 `channels` 字段。手工复现额外使用了
`accl-01:x` 320 条样本。测试数据保留在 `t_monitor`、`t_alert_record` 和
`t_stat_record` 中，供后续查询和人工处理。

## 复现命令

```powershell
cd backend
$env:DEBUG = "false"
$env:DETECTION_ALERT_THRESHOLD_ENABLED = "true"
$env:DETECTION_ALERT_OFFLINE_ENABLED = "true"
$env:DETECTION_ALERT_DAQ_OFFLINE_ENABLED = "true"
$env:DETECTION_ALERT_STUCK_ENABLED = "true"
$env:DETECTION_ALERT_OOR_ENABLED = "true"

.\.venv\Scripts\python.exe tools/stress_real_alerts.py `
  --mode preflight `
  --health-url http://127.0.0.1:8090/api/health `
  --confirm-database huaibeixi_db_test

.\.venv\Scripts\python.exe tools/stress_real_alerts.py `
  --mode all `
  --allow-write `
  --confirm-database huaibeixi_db_test `
  --health-url http://127.0.0.1:8090/api/health `
  --report-json ..\docs\stress-real-alerts-2026-10-26.json
```
