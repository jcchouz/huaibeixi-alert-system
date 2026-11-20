"""按远端数据库覆盖三张预警和统计表的表设计。

Revision ID: 20261120_0038
Revises: 20261030_0037
Create Date: 2026-11-20
"""

from alembic import op

revision = "20261120_0038"
down_revision = "20261030_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留现有数据后，按远端 DDL 重建三张表。"""
    op.execute(
        """
        CREATE TEMP TABLE _migration_alert_record ON COMMIT DROP AS
        SELECT
            id,
            sensor_no,
            threshold_id,
            trigger_val,
            COALESCE(create_time, CURRENT_TIMESTAMP) AS create_time,
            alert_status,
            COALESCE(channel, '') AS channel,
            seq,
            alert_type,
            alert_level,
            handle_by,
            handle_time,
            end_time,
            alert_category,
            handle_opinion,
            handle_phone,
            rule_snapshot
        FROM public.t_alert_record
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _migration_alert_threshold ON COMMIT DROP AS
        SELECT *
        FROM public.t_alert_threshold
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _migration_stat_record ON COMMIT DROP AS
        SELECT
            id,
            sensor_no,
            channel,
            stat_time,
            val_max,
            val_min,
            period_type,
            val_avg,
            max_time,
            min_time,
            sample_count,
            create_by,
            create_time
        FROM public.t_stat_record
        """
    )

    op.execute("DROP TABLE public.t_stat_record")
    op.execute("DROP TABLE public.t_alert_record")
    op.execute("DROP TABLE public.t_alert_threshold")
    op.execute("DROP SEQUENCE IF EXISTS public.t_alert_record_seq_seq")

    op.execute(
        """
        CREATE SEQUENCE public.t_alert_record_seq_seq
            START WITH 1
            INCREMENT BY 1
            NO MINVALUE
            NO MAXVALUE
            CACHE 1
        """
    )
    op.execute(
        """
        CREATE TABLE public.t_alert_record (
            id character varying(32) NOT NULL,
            sensor_no character varying(50) NOT NULL,
            threshold_id character varying(32),
            trigger_val numeric,
            create_time timestamp(6) without time zone
                DEFAULT CURRENT_TIMESTAMP NOT NULL,
            alert_status smallint DEFAULT 0 NOT NULL,
            channel character varying(50)
                DEFAULT ''::character varying NOT NULL,
            seq bigint DEFAULT nextval(
                'public.t_alert_record_seq_seq'::regclass
            ) NOT NULL,
            alert_type character varying(30)
                DEFAULT '阈值超限'::character varying NOT NULL,
            alert_level smallint DEFAULT 1 NOT NULL,
            handle_by character varying(64),
            handle_time timestamp(6) without time zone,
            end_time timestamp(6) without time zone,
            alert_category smallint DEFAULT 0 NOT NULL,
            handle_opinion text,
            handle_phone character varying(32),
            rule_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
            alert_function integer
        )
        """
    )
    op.execute(
        """
        ALTER SEQUENCE public.t_alert_record_seq_seq
        OWNED BY public.t_alert_record.seq
        """
    )
    op.execute(
        """
        CREATE TABLE public.t_alert_threshold (
            id character varying(32) NOT NULL,
            sensor_no character varying(50) NOT NULL,
            physical_quantity character varying(50) NOT NULL,
            create_time timestamp(6) without time zone
                DEFAULT CURRENT_TIMESTAMP,
            update_time timestamp(6) without time zone
                DEFAULT CURRENT_TIMESTAMP,
            channel character varying(50)
                DEFAULT ''::character varying NOT NULL,
            level1_upper numeric(15,5),
            level1_lower numeric(15,5),
            level2_upper numeric(15,5),
            level2_lower numeric(15,5),
            level3_upper numeric(15,5),
            level3_lower numeric(15,5),
            update_by character varying(64),
            create_by character varying(64)
                DEFAULT ''::character varying,
            range_min numeric(15,5),
            range_max numeric(15,5),
            threshold_basis character varying(1000)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.t_stat_record (
            id character varying(32) NOT NULL,
            sensor_no character varying(50) NOT NULL,
            channel character varying(50),
            stat_time timestamp(6) without time zone NOT NULL,
            val_max numeric,
            val_min numeric,
            period_type character varying(10),
            val_avg numeric,
            max_time timestamp(6) without time zone,
            min_time timestamp(6) without time zone,
            sample_count numeric,
            create_by character varying(64),
            create_time timestamp(6) without time zone
                DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    op.execute(
        """
        INSERT INTO public.t_alert_record (
            id, sensor_no, threshold_id, trigger_val, create_time,
            alert_status, channel, seq, alert_type, alert_level,
            handle_by, handle_time, end_time, alert_category,
            handle_opinion, handle_phone, rule_snapshot
        )
        SELECT
            id, sensor_no, threshold_id, trigger_val, create_time,
            alert_status, channel, seq, alert_type, alert_level,
            handle_by, handle_time, end_time, alert_category,
            handle_opinion, handle_phone, rule_snapshot
        FROM _migration_alert_record
        """
    )
    op.execute(
        """
        INSERT INTO public.t_alert_threshold (
            id, sensor_no, physical_quantity, create_time, update_time,
            channel, level1_upper, level1_lower, level2_upper,
            level2_lower, level3_upper, level3_lower, update_by,
            create_by, range_min, range_max, threshold_basis
        )
        SELECT
            id, sensor_no, physical_quantity, create_time, update_time,
            channel, level1_upper, level1_lower, level2_upper,
            level2_lower, level3_upper, level3_lower, update_by,
            create_by, range_min, range_max, threshold_basis
        FROM _migration_alert_threshold
        """
    )
    op.execute(
        """
        INSERT INTO public.t_stat_record (
            id, sensor_no, channel, stat_time, val_max, val_min,
            period_type, val_avg, max_time, min_time, sample_count,
            create_by, create_time
        )
        SELECT
            id, sensor_no, channel, stat_time, val_max, val_min,
            period_type, val_avg, max_time, min_time, sample_count,
            create_by, create_time
        FROM _migration_stat_record
        """
    )

    op.execute(
        """
        ALTER TABLE ONLY public.t_alert_threshold
            ADD CONSTRAINT t_alert_threshold_pkey PRIMARY KEY (id)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_alert_record_id
            ON public.t_alert_record USING btree (id)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_alert_record_query
            ON public.t_alert_record USING btree (
                sensor_no, channel, create_time DESC
            )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_alert_record_seq
            ON public.t_alert_record USING btree (seq)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_alert_record_status
            ON public.t_alert_record USING btree (
                alert_status, alert_level, create_time DESC
            )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_alert_threshold_1
            ON public.t_alert_threshold USING btree (sensor_no)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_stat_record_query
            ON public.t_stat_record USING btree (
                sensor_no, channel, stat_time
            )
        """
    )
    op.execute(
        """
        CREATE INDEX t_alert_record_create_time_idx
            ON public.t_alert_record USING btree (create_time DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX t_stat_record_stat_time_idx
            ON public.t_stat_record USING btree (stat_time DESC)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uk_alert_threshold_target
            ON public.t_alert_threshold USING btree (
                lower(sensor_no::text), channel
            )
        """
    )
    op.execute(
        """
        SELECT setval(
            'public.t_alert_record_seq_seq',
            GREATEST(COALESCE(MAX(seq), 1), 1),
            COALESCE(MAX(seq), 0) > 0
        )
        FROM public.t_alert_record
        """
    )

    op.execute(
        """
        COMMENT ON TABLE public.t_alert_threshold IS '告警阈值配置表'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.id IS
            '告警记录主键，32位UUID'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.sensor_no IS
            '传感器编号（关联t_sensor.sensor_no）'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.threshold_id IS
            '触发告警的阈值规则ID（关联t_alert_threshold.id）；非阈值类告警可为空'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.trigger_val IS
            '告警触发时的实际监测值'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.create_time IS
            '首次告警时间，默认当前时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_status IS
            '告警状态：0未处理，1已处理，默认0'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.channel IS
            '监测通道，空字符串表示默认通道'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.seq IS
            '告警增量推送序列号，用于SSE推送和游标轮询，默认自增'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
            '告警类型：阈值超限、传感器离线、采集仪离线、传感器死值、超物理量程，默认阈值超限'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_level IS
            '告警等级：阈值超限支持1/2/3级；其余告警类型固定为1级，默认1'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_by IS '处理人'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_time IS '处理时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.end_time IS '告警结束时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_category IS
            '预警分类：0=数据预警，1=设备预警，默认0'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_opinion IS '处理意见'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_phone IS '处理人电话'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.rule_snapshot IS
            '预警首次创建时保存的规则内容；活动预警后续更新不再修改，默认空JSON对象'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_function IS
            '预警功能分类：1=结构，2=环境'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.id IS
            '阈值规则主键，32位UUID'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.sensor_no IS
            '传感器编号，关联t_sensor.sensor_no'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.physical_quantity IS
            '监测物理量名称，例如应变、温度、湿度、风速'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.create_time IS '创建时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.update_time IS '修改时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.channel IS
            '监测通道，空字符串表示默认通道'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.level1_upper IS
            '一级告警上限，监测值大于该值时触发一级红色告警'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.level1_lower IS
            '一级告警下限，监测值小于该值时触发一级红色告警'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.level2_upper IS
            '二级告警上限，监测值大于该值时触发二级橙色告警'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.level2_lower IS
            '二级告警下限，监测值小于该值时触发二级橙色告警'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.level3_upper IS
            '三级告警上限，监测值大于该值时触发三级黄色告警'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.level3_lower IS
            '三级告警下限，监测值小于该值时触发三级黄色告警'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.update_by IS '修改人'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.create_by IS '创建人'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.range_min IS
            '传感器量程最小值，按监测物理量或通道从 t_sensor.range 匹配'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.range_max IS
            '传感器量程最大值，按监测物理量或通道从 t_sensor.range 匹配'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.threshold_basis IS
            '设置阈值等级和量程时采用的依据'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.sensor_no IS '传感器编号'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.channel IS '通道号'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.stat_time IS '统计时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.val_max IS '物理量最大值'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.val_min IS '物理量最小值'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.period_type IS
            '统计周期，格式为分钟数加m，例如10m；由DETECTION_WINDOW_MINUTES配置'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.val_avg IS
            '统计窗口内监测数据算术平均值'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.max_time IS
            '统计窗口内最大值发生时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.min_time IS
            '统计窗口内最小值发生时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.sample_count IS
            '统计窗口内有效采样点数，默认0'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.create_by IS
            '统计记录创建人，默认system'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.create_time IS '创建时间'
        """
    )


def downgrade() -> None:
    """该迁移会移除旧约束，不支持自动降级。"""
    raise NotImplementedError("三表已按远端设计重建，需使用迁移前备份恢复")
