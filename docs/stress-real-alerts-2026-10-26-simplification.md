# 代码简化后真实规则全预警压测报告

## 测试信息

- 测试时间：2026-10-26 21:27:06.949 至 21:33:07.733
- 数据库：`huaibeixi_db_test`
- 站点：`YHZ`
- 检测入口：单实例 `AlertDetectionWorker`
- 测试工具：`backend/tools/stress_real_alerts.py --mode all`
- 完整机器可读结果：
  [stress-real-alerts-2026-10-26-simplification.json](stress-real-alerts-2026-10-26-simplification.json)
- 实时预警线程和一分钟统计线程同时运行，五类预警开关全部开启。
- 测试数据保留在 `t_monitor`、`t_alert_record` 和 `t_stat_record`，未自动清理。

## 简化内容

- 频率解析统一到 `parse_frequency_hz()`，删除引擎内重复实现。
- 删除时间解析和序列化的无行为包装，直接复用规则基础工具。
- 合并通道离线时关闭阈值、死值和超量程事件的重复逻辑。
- 实时扫描直接复用 `channel_states` 索引，不再每轮重建通道映射。
- 活动预警缓存改为浅拷贝，不再每轮复制每个预警字典。
- 统计积压改为逐窗口推进，不再一次性生成全部待处理窗口列表。
- 删除 `app.config`、`app.engine` 兼容入口，工具直接导入正式模块。
- 保留 Worker 双线程、规则协议与注册表、单实例检测游标及扫描/统计分离架构。

## 结果

| 指标 | 结果 |
| --- | ---: |
| 真实规则数 | 137 |
| 真实通道数 | 137 |
| 采集仪数 | 7 |
| 理论预警键 | 555 |
| 写入并读回样本 | 138,978 / 138,978 |
| 命中预警 | 555 / 555 |
| 生成一分钟统计的通道 | 137 / 137 |
| 最大单轮扫描耗时 | 137.89 ms |
| 最大预警积压 | 1.014 s |
| 最终预警积压 | 0 s |
| Worker 错误 | 0 |
| 最终结果 | 通过 |

预警类型覆盖：

| 预警类型 | 命中数量 |
| --- | ---: |
| 阈值超限 | 137 |
| 超物理量程 | 137 |
| 传感器死值 | 137 |
| 传感器离线 | 137 |
| 采集仪离线 | 7 |

与 2026-10-26 19:02 的同规格拆分路径压测相比，最大单轮扫描耗时从
178.95 ms 降至 137.89 ms，约下降 23%；最大积压从 1.011 s 变为
1.014 s，仍为单个扫描周期量级且测试结束后归零。

## 使用的真实传感器和通道

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

完整 137 个 `sensor_no:channel` 明细见 JSON 报告中的 `channels` 字段。

## 复现命令

```powershell
cd backend
$env:DEBUG = "false"
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode preflight
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode all `
  --allow-write --confirm-database huaibeixi_db_test `
  --report-json ..\docs\stress-real-alerts-2026-10-26-simplification.json
```

## 后续查询

```sql
SELECT *
FROM public.t_monitor
WHERE time BETWEEN '2026-10-26 21:27:06.949'
               AND '2026-10-26 21:33:07.733'
ORDER BY time;

SELECT *
FROM public.t_alert_record
WHERE create_time BETWEEN '2026-10-26 21:27:06.949'
                      AND '2026-10-26 21:33:07.733'
ORDER BY seq;

SELECT *
FROM public.t_stat_record
WHERE stat_time BETWEEN '2026-10-26 21:27:00'
                    AND '2026-10-26 21:34:00'
ORDER BY stat_time, sensor_no, channel;
```
