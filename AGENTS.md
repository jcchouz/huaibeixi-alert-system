# 项目约束

## 项目定位

- 本项目的前端页面、REST API、弹窗和操作按钮仅用于本地开发、联调和人工测试。
- 生产环境只要求后端读取外部系统持续写入的 `t_monitor` 数据，执行预警检测和统计分析，并写入 `t_alert_record`、`t_stat_record`。
- 生产链路不得依赖前端页面、前端轮询、REST API 操作或 `continuous_simulator.py` 才能工作。

## 运行职责

- `AlertDetectionWorker` 是生产检测入口，随 FastAPI 生命周期启动，负责按数据库时间持续检测 `t_monitor` 原始采样点，并周期性生成统计记录。
- 服务启动时以数据库当前时间为检测边界，不恢复历史水位、通道状态或启动前数据。
- `t_stat_record` 按完整窗口生成，默认窗口为 1 分钟，可通过 `DETECTION_WINDOW_MINUTES` 配置。
- 通道离线、采集仪离线、阈值超限、传感器死值和超物理量程预警按原始采样点连续命中判定，不等待统计窗口结束。
- 连续命中条件只使用秒制，通过 `DETECTION_CONFIRM_SECONDS`、`DETECTION_OFFLINE_SECONDS` 和 `DETECTION_STUCK_SECONDS` 配置。
- 实时预警扫描周期由 `DETECTION_INTERVAL_SECONDS` 配置，统计窗口只影响 `t_stat_record`，不得阻塞实时预警。
- 实时扫描每轮最多推进一个检测周期；单轮耗时超过周期时，通过 `alertBacklogSeconds` 暴露积压。
- 实时扫描通过 `DETECTION_ALERT_OVERLAP_SECONDS` 短回看以覆盖外部事务提交延迟，重复样本必须依据 `last_ts` 去重。
- 各类预警可通过 `DETECTION_ALERT_THRESHOLD_ENABLED`、`DETECTION_ALERT_OFFLINE_ENABLED`、`DETECTION_ALERT_DAQ_OFFLINE_ENABLED`、`DETECTION_ALERT_STUCK_ENABLED` 和 `DETECTION_ALERT_OOR_ENABLED` 独立开关，默认全部开启；关闭某类预警只停止写入该类预警，采样和统计不受影响。
- `continuous_simulator.py` 只负责持续向 `t_monitor` 写入测试数据，并周期性制造各类预警场景，不执行检测，不写入统计或预警表。
- 项目按单实例部署，不使用多实例 advisory lock 选主。

## 修改原则

- 保持“外部数据写入”和“后端检测分析”解耦。
- 不得把前端或测试 API 引入生产检测的关键路径。
- 修改检测逻辑时，优先保证 `t_monitor` 无数据时仍能持续判定通道离线。
- 所有压测和模拟必须使用 `t_sensor`、`t_sensor_channel`、`t_alert_threshold` 中已有的真实传感器编号和通道，不得创建 `benchall-*`、`bench10k-*` 或其他临时传感器写入 `t_monitor`、`t_alert_record`、`t_stat_record`。
- 每次测试完成后保留测试数据，不自动清理 `t_monitor`、`t_alert_record` 或 `t_stat_record`；必须记录测试时间范围、使用的真实传感器和通道，便于后续查询与人工处理。

## 备注
- git提交注释用中文
