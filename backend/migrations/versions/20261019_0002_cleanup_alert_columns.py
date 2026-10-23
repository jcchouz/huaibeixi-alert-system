"""Remove unused alert columns and document every retained column.

Revision ID: 20261019_0002
Revises: 20261019_0001
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0002"
down_revision = "20261019_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS pic_urls
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP COLUMN IF EXISTS duration_sec,
          DROP COLUMN IF EXISTS enable,
          DROP COLUMN IF EXISTS alert_level,
          DROP COLUMN IF EXISTS upper_limit,
          DROP COLUMN IF EXISTS lower_limit
        """
    )

    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.id IS
          '告警记录主键，32位UUID';
        COMMENT ON COLUMN public.t_alert_record.sensor_no IS
          '传感器编号，关联t_sensor.sensor_no';
        COMMENT ON COLUMN public.t_alert_record.threshold_id IS
          '触发告警的阈值规则ID，关联t_alert_threshold.id；非阈值类告警可为空';
        COMMENT ON COLUMN public.t_alert_record.trigger_val IS
          '告警触发时的实际监测值';
        COMMENT ON COLUMN public.t_alert_record.create_time IS
          '告警首次产生时间';
        COMMENT ON COLUMN public.t_alert_record.end_ts IS
          '告警恢复或结束时间，NULL表示告警仍活跃';
        COMMENT ON COLUMN public.t_alert_record.alert_status IS
          '告警状态：0未处理，1已确认，2已恢复，3已关闭，4已抑制';
        COMMENT ON COLUMN public.t_alert_record.remark IS
          '告警诊断摘要及处置备注';
        COMMENT ON COLUMN public.t_alert_record.is_delete IS
          '逻辑删除标记：false正常，true已删除';
        COMMENT ON COLUMN public.t_alert_record.channel IS
          '监测通道，空字符串表示默认通道';
        COMMENT ON COLUMN public.t_alert_record.seq IS
          '告警增量推送序列号，用于SSE推送和游标轮询';
        COMMENT ON COLUMN public.t_alert_record.dedup_key IS
          '活跃告警去重键，格式为sensor_no:channel:alert_type:direction';
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
          '告警类型：THRESHOLD、OFFLINE、GAP_1000MS、RATE、STUCK、OOR、SPIKE、DAQ_OFFLINE';
        COMMENT ON COLUMN public.t_alert_record.alert_level IS
          '告警等级：1一级红色，2二级橙色，3三级黄色';
        COMMENT ON COLUMN public.t_alert_record.direction IS
          '越限方向：UPPER超上限，LOWER超下限，NONE无方向';
        COMMENT ON COLUMN public.t_alert_record.peak_val IS
          '告警持续期间的最危险极值';
        COMMENT ON COLUMN public.t_alert_record.peak_time IS
          '最危险极值出现时间';
        COMMENT ON COLUMN public.t_alert_record.last_fire_time IS
          '最近一次命中或升级告警的时间';
        COMMENT ON COLUMN public.t_alert_record.trigger_count IS
          '本次告警累计触发次数';
        COMMENT ON COLUMN public.t_alert_record.rule_version IS
          '告警触发时生效的阈值规则版本号';
        COMMENT ON COLUMN public.t_alert_record.threshold_snapshot IS
          '告警触发时的阈值及算法参数快照';
        COMMENT ON COLUMN public.t_alert_record.timeline_log IS
          '告警生命周期日志数组，记录触发、升级、确认、恢复和抑制事件';
        COMMENT ON COLUMN public.t_alert_record.ack_by IS
          '告警确认人';
        COMMENT ON COLUMN public.t_alert_record.ack_time IS
          '告警确认时间';
        COMMENT ON COLUMN public.t_alert_record.update_time IS
          '告警记录最后更新时间';
        """
    )

    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.id IS
          '阈值规则主键，32位UUID';
        COMMENT ON COLUMN public.t_alert_threshold.sensor_no IS
          '传感器编号，关联t_sensor.sensor_no';
        COMMENT ON COLUMN public.t_alert_threshold.physical_quantity IS
          '监测物理量名称，例如应变、温度、湿度、风速';
        COMMENT ON COLUMN public.t_alert_threshold.create_time IS
          '阈值规则创建时间';
        COMMENT ON COLUMN public.t_alert_threshold.update_time IS
          '阈值规则最后修改时间';
        COMMENT ON COLUMN public.t_alert_threshold.is_delete IS
          '逻辑删除标记：false正常，true已删除';
        COMMENT ON COLUMN public.t_alert_threshold.channel IS
          '监测通道，空字符串表示默认通道';
        COMMENT ON COLUMN public.t_alert_threshold.rule_type IS
          '规则类型：THRESHOLD阈值、OFFLINE离线、RATE变化率、STUCK死值、OOR超量程、SPIKE毛刺';
        COMMENT ON COLUMN public.t_alert_threshold.monitor_enabled IS
          '通道监控开关：true参与监测，false暂停监测';
        COMMENT ON COLUMN public.t_alert_threshold.expected_interval_ms IS
          '期望采样间隔，单位毫秒；100Hz为10，10Hz为100，1Hz为1000';
        COMMENT ON COLUMN public.t_alert_threshold.offline_timeout_ms IS
          '离线判定超时时间，单位毫秒';
        COMMENT ON COLUMN public.t_alert_threshold.level1_upper IS
          '一级告警上限，监测值大于该值时触发一级红色告警';
        COMMENT ON COLUMN public.t_alert_threshold.level1_lower IS
          '一级告警下限，监测值小于该值时触发一级红色告警';
        COMMENT ON COLUMN public.t_alert_threshold.level2_upper IS
          '二级告警上限，监测值大于该值时触发二级橙色告警';
        COMMENT ON COLUMN public.t_alert_threshold.level2_lower IS
          '二级告警下限，监测值小于该值时触发二级橙色告警';
        COMMENT ON COLUMN public.t_alert_threshold.level3_upper IS
          '三级告警上限，监测值大于该值时触发三级黄色告警';
        COMMENT ON COLUMN public.t_alert_threshold.level3_lower IS
          '三级告警下限，监测值小于该值时触发三级黄色告警';
        COMMENT ON COLUMN public.t_alert_threshold.confirm_points IS
          '连续超限确认点数，达到该点数后确认并触发告警';
        COMMENT ON COLUMN public.t_alert_threshold.recover_points IS
          '连续正常恢复点数，达到该点数后确认告警恢复';
        COMMENT ON COLUMN public.t_alert_threshold.hysteresis IS
          '迟滞回差，恢复到阈值加减回差范围内才判定为正常';
        COMMENT ON COLUMN public.t_alert_threshold.ext_params IS
          '算法扩展参数JSON，例如物理量程、严格采样间隔和变化率参数';
        COMMENT ON COLUMN public.t_alert_threshold.version IS
          '阈值规则版本号，每次修改自动加一';
        COMMENT ON COLUMN public.t_alert_threshold.update_by IS
          '最近一次修改人';
        COMMENT ON COLUMN public.t_alert_threshold.change_reason IS
          '最近一次调参原因';
        COMMENT ON COLUMN public.t_alert_threshold.history_log IS
          '阈值规则历史修改快照数组，用于版本追踪和一键回滚';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS pic_urls VARCHAR(4000)
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ADD COLUMN IF NOT EXISTS duration_sec INTEGER DEFAULT 0,
          ADD COLUMN IF NOT EXISTS enable BOOLEAN DEFAULT true,
          ADD COLUMN IF NOT EXISTS alert_level SMALLINT,
          ADD COLUMN IF NOT EXISTS upper_limit NUMERIC,
          ADD COLUMN IF NOT EXISTS lower_limit NUMERIC
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.pic_urls IS
          '部分视频监控类告警附带的图片URL，多个URL用逗号分隔';
        COMMENT ON COLUMN public.t_alert_threshold.duration_sec IS
          '持续秒数，满足条件持续指定秒数后告警，0表示立即触发';
        COMMENT ON COLUMN public.t_alert_threshold.enable IS
          '旧版规则启用标记，新逻辑统一使用monitor_enabled';
        COMMENT ON COLUMN public.t_alert_threshold.alert_level IS
          '旧版单级告警等级，新逻辑使用level1/level2/level3三级阈值';
        COMMENT ON COLUMN public.t_alert_threshold.upper_limit IS
          '旧版一级上限阈值，新逻辑使用level1_upper';
        COMMENT ON COLUMN public.t_alert_threshold.lower_limit IS
          '旧版一级下限阈值，新逻辑使用level1_lower';
        """
    )
