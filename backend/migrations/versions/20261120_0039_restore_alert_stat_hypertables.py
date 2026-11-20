"""将告警和统计表恢复为远端 TimescaleDB 超表设计。

Revision ID: 20261120_0039
Revises: 20261120_0038
Create Date: 2026-11-20
"""

from alembic import op

revision = "20261120_0039"
down_revision = "20261120_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """按远端时间维度、分块间隔、压缩设置和列存储策略转换超表。"""
    op.execute(
        """
        SELECT create_hypertable(
            'public.t_alert_record',
            'create_time',
            chunk_time_interval => INTERVAL '7 days',
            create_default_indexes => FALSE,
            migrate_data => TRUE,
            if_not_exists => TRUE
        )
        """
    )
    op.execute(
        """
        SELECT create_hypertable(
            'public.t_stat_record',
            'stat_time',
            chunk_time_interval => INTERVAL '2 days',
            create_default_indexes => FALSE,
            migrate_data => TRUE,
            if_not_exists => TRUE
        )
        """
    )

    op.execute(
        """
        ALTER TABLE public.t_alert_record SET (
            timescaledb.compress,
            timescaledb.compress_segmentby =
                'sensor_no,alert_type,alert_level',
            timescaledb.compress_orderby = 'create_time DESC'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_stat_record SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'sensor_no,channel',
            timescaledb.compress_orderby = 'stat_time DESC'
        )
        """
    )

    op.execute(
        """
        SELECT add_compression_policy(
            'public.t_alert_record',
            INTERVAL '90 days',
            if_not_exists => TRUE,
            schedule_interval => INTERVAL '12 hours'
        )
        """
    )
    op.execute(
        """
        SELECT add_compression_policy(
            'public.t_stat_record',
            INTERVAL '15 days',
            if_not_exists => TRUE,
            schedule_interval => INTERVAL '12 hours'
        )
        """
    )


def downgrade() -> None:
    """超表迁移包含历史数据重分布，不支持自动降级。"""
    raise NotImplementedError("告警和统计超表需使用迁移前备份恢复")
