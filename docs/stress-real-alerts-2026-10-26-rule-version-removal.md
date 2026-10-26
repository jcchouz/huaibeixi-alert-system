# 规则版本移除后真实规则全预警压测报告

- 测试日期：2026-10-26
- 测试库：`huaibeixi_db_test`
- 站点：`YHZ`
- 检测入口：单实例 `AlertDetectionWorker`
- 测试工具：`backend/tools/stress_real_alerts.py`
- 规则版本迁移：`20261026_0035`

## 测试目标

- 验证删除规则版本机制后，阈值超限、超物理量程、传感器死值、传感器离线和
  采集仪离线仍全部正常。
- 验证实时预警线程和 1 分钟统计线程同时运行。
- 验证每条预警首次创建时写入非空 `rule_snapshot`。
- 使用现有真实传感器、通道和阈值，不创建任何临时规则或传感器。
- 保留测试数据，不执行自动清理。

## 数据保留

### 无效尝试

- 时间范围：`2026-10-26 17:44:29.012` 至 `2026-10-26 17:50:28.724`
- 报告：`docs/stress-real-alerts-2026-10-26-rule-version-removal.json`
- 结果：功能检查通过，但发现另一套旧调试后端仍连接数据库，违反单实例约束，
  因此该轮不作为有效压测结论。
- 数据状态：138,978 行 `t_monitor`、相关预警和统计全部保留。

### 有效单实例压测

- 时间范围：`2026-10-26 17:52:26.118` 至 `2026-10-26 17:58:26.329`
- 报告：`docs/stress-real-alerts-2026-10-26-rule-version-removal-single-instance.json`
- 数据状态：138,978 行 `t_monitor`、1,420 行预警记录、相关统计全部保留。
- 测试结束后未清理 `t_monitor`、`t_alert_record` 或 `t_stat_record`。

## 测试范围

- 真实通道：137 个
- 真实传感器：73 个
- 采集仪：7 个
- 理论预警键：555 个
- 各类型预警键：阈值超限 137、超物理量程 137、传感器死值 137、
  传感器离线 137、采集仪离线 7

采集仪 ID：

```text
1adda0075d334f3d93dc2317116205e9
5c7d196672964fc68f71a7fe139c62ed
af45e9d719774090aadb57325a5f0e3d
bf10e0feeddd42258eee9255baf80184
db9c060cba5f47c4b153bcf58bed81b0
ebcea3a999d343b5addd2fdb8112f5fe
ece8aedf309143cdbe2e5b9715221e20
```

真实传感器和通道分组：

- `accl-01` 至 `accl-04`：`x`、`y`、`z`
- `disp-01` 至 `disp-03`：`displacement`、`temperature`
- `phtar-01` 至 `phtar-06`：`x`、`y`
- `phtar-J01` 至 `phtar-J03`：`x`、`y`
- `stn-01-e` 至 `stn-06-e`：`stress`、`temperature`
- `stn-07-a` 至 `stn-07-d`：`stress`、`temperature`
- `stn-08-a` 至 `stn-08-d`：`stress`、`temperature`
- `stn-09-a` 至 `stn-09-c`：`stress`、`temperature`
- `stn-10-a` 至 `stn-10-c`：`stress`、`temperature`
- `stn-11-a` 至 `stn-11-c`：`stress`、`temperature`
- `stn-12-a` 至 `stn-12-c`：`stress`、`temperature`
- `stn-13-a` 至 `stn-13-c`：`stress`、`temperature`
- `stn-14-a` 至 `stn-14-c`：`stress`、`temperature`
- `stn-15-a` 至 `stn-15-d`：`stress`、`temperature`
- `stn-16-a` 至 `stn-16-d`：`stress`、`temperature`
- `temp-01` 至 `temp-03`：默认通道
- `wea-01`：`humidity`、`rainfall`、`temperature`、`windDirection`、`windSpeed`
- `wlg-01` 至 `wlg-10`：默认通道
- `wprs-01` 至 `wprs-03`：默认通道

完整 137 个 `sensor_no:channel` 明细和阶段时间记录在有效压测 JSON 报告中。

## 有效压测结果

| 指标 | 结果 |
| --- | ---: |
| 写入行数 | 138,978 |
| 回读行数 | 138,978 |
| 正常阶段 | 137 行 |
| 阈值阶段 | 3,975 行 |
| 超量程阶段 | 3,975 行 |
| 死值阶段 | 130,754 行 |
| 恢复阶段 | 137 行 |
| 预期预警键 | 555 |
| 命中预警键 | 555 |
| 统计通道 | 137 / 137 |
| 最大实时扫描耗时 | 136.04 ms |
| 最大积压 | 1.016 秒 |
| Worker 错误 | 无 |
| 结论 | 通过 |

有效时间窗内共写入 1,420 行预警。555 是不同
`sensor_no + channel + alert_type` 的数量，1,420 是多阶段测试中恢复后重新建
事件的累计行数。数据库检查结果：

- 同一预警键的事件时间没有重叠。
- 当前同一预警键没有多条未结束记录。
- 1,420 行预警的 `rule_snapshot` 均非空。
- `rule_snapshot.alert_type` 与实际 `alert_type` 全部一致。
- `rule_snapshot` 未包含 `last_ts`、`online_status` 等运行时状态字段。
- 规则版本表、函数和触发器均不存在。

## 其它验证

- `t_alert_threshold.version`、`history_log` 不存在。
- `t_alert_record.rule_snapshot` 为 `JSONB NOT NULL DEFAULT '{}'::jsonb`。
- Alembic 当前版本为 `20261026_0035 (head)`。
- 后端非集成测试全部通过。
- Ruff 检查通过。
- `/api/health`、`/api/overview`、`/api/alerts` 正常。
- 阈值列表、统计传感器列表和极值查询接口正常。
- 健康状态不再包含旧规则版本或规则刷新字段。
- 测试结束时只有一个监听 8090 的后端实例持有数据库连接。

## 后续查询

```sql
SELECT *
FROM public.t_monitor
WHERE time BETWEEN '2026-10-26 17:52:26.118'
               AND '2026-10-26 17:58:26.329'
ORDER BY time;

SELECT *
FROM public.t_alert_record
WHERE create_time BETWEEN '2026-10-26 17:52:26.118'
                      AND '2026-10-26 17:58:26.329'
ORDER BY seq;

SELECT *
FROM public.t_stat_record
WHERE stat_time BETWEEN '2026-10-26 17:52:00'
                    AND '2026-10-26 17:59:00'
ORDER BY stat_time, sensor_no, channel;
```
