# 2026-11-27 真实规则全预警与双周期统计压测

- 测试数据库：`huaibeixi_db_test@192.168.26.93:5432`
- 压测时间范围：`2026-11-27 11:40:01.003` 至 `2026-11-27 11:48:08.389`
- 统计窗口：`1m`、`10m`
- 检测周期：1 秒
- 运行方式：完整 `AlertDetectionWorker`，实时预警线程与统计线程同时运行
- 数据来源：`t_sensor`、`t_sensor_channel`、`t_alert_threshold` 中已有的真实配置
- 覆盖范围：137 个真实通道、7 个采集仪
- 最大预期活动预警数：`137 × 4 + 7 = 555`
- 测试数据：保留，未清理 `t_monitor`、`t_alert_record` 或 `t_stat_record`

## 压测结果

| 预警类型 | 预期数量 | 实际命中 |
| --- | ---: | ---: |
| 阈值超限 | 137 | 137 |
| 超物理量程 | 137 | 137 |
| 传感器死值 | 137 | 137 |
| 传感器离线 | 137 | 137 |
| 采集仪离线 | 7 | 7 |
| 合计 | 555 | 555 |

| 阶段 | 写入行数 |
| --- | ---: |
| 正常基线 | 137 |
| 阈值连续命中 | 3,975 |
| 超物理量程连续命中 | 3,975 |
| 传感器死值连续命中 | 130,754 |
| 恢复样本 | 137 |
| 合计 | 138,978 |

- 样本回读：138,978/138,978
- `1m` 统计：137/137 个真实通道生成记录
- `10m` 统计：137/137 个真实通道生成记录
- 本轮新增预警行数：1,543
- 本轮唯一预警键：555
- 新生成预警的 `alert_function` 空值数：0
- 最大单次实时扫描耗时：861.44 ms
- 最大 `alertBacklogSeconds`：1.004 秒，之后归零
- Worker 错误：无
- 最终结果：`passed=true`

完整机器可读结果见 `stress-real-alerts-2026-11-27.json`。

## 其他验证

- `ruff check app tests tools`：通过
- `pytest --cov=app`：144 个测试通过，覆盖率 72.22%
- Alembic 全量升级 SQL：可生成
- 前端 `npm run build`：生产构建成功
- 运行中 REST API 只读检查：10 个路径全部返回 200
- API 写接口验证：17/17 通过，覆盖阈值更新/批量更新/模拟、预警创建/删除/单条确认/全部确认、双周期统计查询
- 快照恢复校验：`t_alert_threshold` 208 行、`t_alert_record` 363 行，恢复前后一致

## 快照工具事故与恢复说明

第一次运行项目自带的 `tools/api_validation.py` 时，旧实现使用
`COPY public.t_alert_record TO STDOUT WITH BINARY` 对 TimescaleDB 压缩超表做快照。
该路径只输出了 21 字节空 `PGCOPY` 头，恢复阶段因此将表覆盖为空。事故前表的
`t_alert_record` 为 29,170 行。

已完成的恢复与修复：

- 从仓库 `2026-10-29` 数据库备份恢复 313 条预警。
- 从仍可访问的旧测试远端恢复 50 条 `2026-11-24` 预警。
- 修复 `api_validation.py`，改用查询式 CSV 快照
  `COPY (SELECT * FROM table) ... FORMAT csv`，并增加快照/恢复行数一致性校验。
- 新增 CSV 行数解析测试，避免字段内换行或空快照再次被误判。
- 修复后的 API 验证连续通过，恢复前后预警行数一致。
- 重新执行本次全量压测，重新写入本轮 1,543 行预警。

当前 `t_alert_record` 共 1,906 行：`2026-10-23` 313 行、`2026-11-24` 50 行、
`2026-11-27` 1,543 行。事故中未备份的其余历史预警无法从当前数据库、旧远端或
仓库备份完整还原，需要数据库管理员依据服务器级备份或 WAL/PITR 进一步恢复。

## 执行命令

```bash
cd backend
python tools/stress_real_alerts.py --mode preflight
python tools/stress_real_alerts.py --mode all \
  --allow-write --confirm-database huaibeixi_db_test \
  --report-json ../docs/stress-real-alerts-2026-11-27.json
```
