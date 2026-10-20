# 项目约束

## 项目定位

- 本项目的前端页面、REST API、弹窗和操作按钮仅用于本地开发、联调和人工测试。
- 生产环境只要求后端读取外部系统持续写入的 `t_monitor` 数据，执行预警检测和统计分析，并写入 `t_alert_record`、`t_stat_record`。
- 生产链路不得依赖前端页面、前端轮询、REST API 操作或 `continuous_simulator.py` 才能工作。

## 运行职责

- `AlertDetectionWorker` 是生产检测入口，随 FastAPI 生命周期启动，按数据库时间消费最近结束的平均窗口；默认窗口为 1 分钟，可通过 `DETECTION_WINDOW_MINUTES` 配置。
- 离线超时和死值判定时间独立配置，分别使用 `DETECTION_OFFLINE_TIMEOUT_SECONDS` 和 `DETECTION_STUCK_TIME_SECONDS`。
- 检测线程负责生成平均窗口统计、通道离线、采集仪离线、阈值超限、传感器死值和超物理量程预警；阈值和超量程判断使用窗口平均值。
- `continuous_simulator.py` 只负责持续向 `t_monitor` 写入测试数据，并周期性制造各类预警场景，不执行检测，不写入统计或预警表。
- 多个后端实例同时运行时，检测通过 PostgreSQL advisory lock 选主，避免重复检测。

## 修改原则

- 保持“外部数据写入”和“后端检测分析”解耦。
- 不得把前端或测试 API 引入生产检测的关键路径。
- 修改检测逻辑时，优先保证 `t_monitor` 无数据时仍能持续判定通道离线。
