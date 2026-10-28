# 2026-10-28 真实规则全预警压测

- 测试数据库：`huaibeixi_db_test`
- 测试时间范围：`2026-10-28 11:46:22.851` 至 `2026-10-28 11:54:24.710`
- 检测周期：1 秒
- 统计周期：1 分钟
- 运行方式：完整 `AlertDetectionWorker`，实时预警线程与统计线程同时运行
- 数据来源：`t_sensor`、`t_sensor_channel`、`t_alert_threshold` 中已有的真实配置，
  未创建临时传感器
- 真实规则与通道：137 个通道，7 个采集仪；完整传感器和通道清单见
  `stress-real-alerts-2026-10-28.json` 的 `channels` 字段
- 最长离线确认时间：300 秒，来自真实低频通道配置
- 测试数据：保留，未清理 `t_monitor`、`t_alert_record` 或 `t_stat_record`

## 覆盖范围

137 个真实通道同时参与阈值超限、超物理量程、传感器死值和通道离线场景，
并验证 7 个采集仪离线。预期最大活动预警数为 `137 × 4 + 7 = 555`。

| 预警类型 | 预期数量 | 实际命中 |
| --- | ---: | ---: |
| 阈值超限 | 137 | 137 |
| 超物理量程 | 137 | 137 |
| 传感器死值 | 137 | 137 |
| 传感器离线 | 137 | 137 |
| 采集仪离线 | 7 | 7 |
| 合计 | 555 | 555 |

本次同时验证新的通道在线语义：当前扫描存在任一样本时通道保持在线，只有当前
扫描完全没有该通道样本并达到配置超时后才生成离线预警。正常基线和恢复样本阶段
均未产生额外通道离线预警。

## 写入与检测

| 阶段 | 写入行数 |
| --- | ---: |
| 正常基线 | 137 |
| 阈值连续命中 | 3,975 |
| 超物理量程连续命中 | 3,975 |
| 传感器死值连续命中 | 130,754 |
| 恢复样本 | 137 |
| 合计 | 138,978 |

- 写入后回读：138,978 行
- 统计结果：137/137 个真实通道均生成 `1m` 统计记录
- 最大单次扫描耗时：184.63 ms
- 最大 `alertBacklogSeconds`：1.014 秒，属于 1 秒轮询节奏，无持续积压
- Worker 错误：无
- 最终结果：`passed=true`

## 执行命令

```powershell
cd backend
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode preflight
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode all `
  --allow-write --confirm-database huaibeixi_db_test `
  --report-json ..\docs\stress-real-alerts-2026-10-28.json
```

完整机器可读结果见 `stress-real-alerts-2026-10-28.json`。
