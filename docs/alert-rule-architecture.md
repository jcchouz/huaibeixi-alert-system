# 预警规则架构

## 设计目标

- 每类预警只负责自己的纯判定逻辑，不直接管理数据库事务。
- 主扫描器负责准备上下文、执行规则、统一写库和提交内存状态。
- 新增预警类型时不修改主扫描循环，只新增规则类并注册。
- 保持离线类预警的依赖顺序和单事务一致性。

## 不按类别直接开线程

实时预警的耗时主要来自数据库读取和 `t_alert_record` 写入，纯 Python 规则计算占比很小。
Python 线程还受 GIL 限制，按类别开线程会增加调度和上下文切换成本。

本机使用 137 条真实规则、约 9,864 个样本窗口重复 100 次测试：

| 执行方式 | 耗时 |
| --- | ---: |
| 顺序执行 | 1.436 秒 |
| 按类别开 3 个线程 | 1.497 秒 |

线程方式没有提速，反而慢约 4%。因此当前默认使用顺序规则编排。若未来出现明确的
外部 IO 或 CPU 密集规则，可单独引入执行器，但数据库写入仍应集中在一个事务中。

## 重构前后扫描对比

使用本机真实传感器数据中的 10 个连续 1 秒窗口，每个窗口 10,000 行，加载相同的
137 条真实规则，每个窗口重复 5 次，共比较 50 次扫描。为单独比较扫描逻辑，测试时
关闭预警写库。

| 指标 | 重构前 | 规则化重构后 | 变化 |
| --- | ---: | ---: | ---: |
| 平均扫描耗时 | 159.556 ms | 157.384 ms | 快 1.36% |
| P95 扫描耗时 | 213.040 ms | 206.479 ms | 快 3.08% |
| 最大扫描耗时 | 316.721 ms | 299.379 ms | 快 5.47% |

性能提升属于小幅改善，主要收益是把预警判定从主扫描器解耦，使新增规则不需要修改
`_run_detection_window()`。当前数据库读取仍占主要耗时，后续性能优化应优先考虑
索引、SQL 返回量和事务写入，而不是增加线程。

## 规则分层

```text
AlertDetectionWorker
  -> AlertAnalyticsEngine._run_detection_window()
      -> 准备 RuleContext
      -> OfflineAlertRule
      -> AlertRuleRegistry
           -> ThresholdAlertRule
           -> OutOfRangeAlertRule
           -> StuckAlertRule
      -> DaqOfflineRule
      -> 统一事务写库
      -> 提交后发布 channel_states
```

依赖顺序：

1. 通道离线优先级最高。离线成立时关闭并重置阈值、超量程和死值状态。
2. 在线状态下，阈值、超量程和死值互不依赖，由注册表顺序执行。
3. 所有通道处理完成后，`DaqOfflineRule` 汇总采集仪级离线。

## 核心契约

每个独立规则实现：

```python
class AlertRule(Protocol):
    alert_type: str

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        ...
```

`RuleContext` 包含当前通道规则、样本、窗口边界、连续确认秒数和离线阈值。

`RuleEvaluation` 包含：

- 是否处于活动条件
- 上一轮是否活动
- 需要更新的运行状态
- 是否触发写预警
- 是否压制其他通道预警

规则本身不执行 SQL。主扫描器通过 `_apply_rule_evaluation()` 统一处理创建、升级和恢复。

## 新增预警规则

1. 在 `backend/app/domain/alert_rules/` 新增规则类。
2. 实现 `evaluate()` 并返回 `RuleEvaluation`。
3. 在 `default_alert_rules()` 中注册。
4. 如规则状态需要跨扫描保存，在 `RUNTIME_STATE_FIELDS` 中增加字段。
5. 增加单元测试和真实传感器场景测试。

示例：

```python
class MyAlertRule:
    alert_type = "我的预警"

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        active = False
        return RuleEvaluation(
            alert_type=self.alert_type,
            active=active,
            previous_active=bool(context.rule.get("my_condition_active")),
            state_updates={"my_condition_active": active},
        )
```

也可通过 `AlertAnalyticsEngine(channel_rules=[...])` 注入自定义规则集合，适合测试或
独立部署场景。
