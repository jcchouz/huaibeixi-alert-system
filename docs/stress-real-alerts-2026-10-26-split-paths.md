# 实时预警与统计拆分后真实规则全预警压测报告

## 测试信息

- 测试时间：2026-10-26 19:02:37.011 至 19:08:37.188
- 数据库：`huaibeixi_db_test`
- 站点：`YHZ`
- 测试工具：`backend/tools/stress_real_alerts.py`
- 测试模式：`--mode all`
- 完整机器可读结果：
  [stress-real-alerts-2026-10-26-split-paths.json](stress-real-alerts-2026-10-26-split-paths.json)
- 实时预警线程和统计线程同时运行，五类预警开关全部开启。
- 测试数据保留在 `t_monitor`、`t_alert_record` 和 `t_stat_record`，未自动清理。

## 结果

| 指标 | 结果 |
| --- | ---: |
| 真实规则数 | 137 |
| 真实通道数 | 137 |
| 采集仪数 | 7 |
| 写入并读回样本 | 138,978 |
| 预期/命中预警 | 555 / 555 |
| 生成一分钟统计的通道 | 137 / 137 |
| 最大单轮扫描耗时 | 178.95 ms |
| 最大预警积压 | 1.011 s |
| Worker 错误 | 0 |
| 最终结果 | 通过 |

预警类型覆盖：

| 预警类型 | 数量 |
| --- | ---: |
| 阈值超限 | 137 |
| 超物理量程 | 137 |
| 传感器死值 | 137 |
| 传感器离线 | 137 |
| 采集仪离线 | 7 |

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
- `temp-01` 至 `temp-03`：空通道名
- `wea-01`：`humidity`、`rainfall`、`temperature`、`windDirection`、`windSpeed`
- `wlg-01` 至 `wlg-10`：空通道名
- `wprs-01` 至 `wprs-03`：空通道名

完整 `sensor_no:channel` 列表、阶段时间和逐项校验结果见 JSON 报告。

## 复现命令

```powershell
cd backend
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode preflight
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode all `
  --allow-write --confirm-database huaibeixi_db_test `
  --report-json ..\docs\stress-real-alerts-2026-10-26-split-paths.json
```
