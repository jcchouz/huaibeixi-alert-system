"""Add creation metadata and comments to t_stat_record.

Revision ID: 20261019_0013
Revises: 20261019_0012
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0013"
down_revision = "20261019_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ADD COLUMN IF NOT EXISTS create_by VARCHAR(64) NOT NULL DEFAULT 'system',
          ADD COLUMN IF NOT EXISTS create_time TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.sensor_no IS
          '传感器编号';
        COMMENT ON COLUMN public.t_stat_record.channel IS
          '监测通道，空字符串表示默认通道';
        COMMENT ON COLUMN public.t_stat_record.stat_time IS
          '统计窗口起始时间';
        COMMENT ON COLUMN public.t_stat_record.val_max IS
          '统计窗口内监测数据最大值';
        COMMENT ON COLUMN public.t_stat_record.val_min IS
          '统计窗口内监测数据最小值';
        COMMENT ON COLUMN public.t_stat_record.period_type IS
          '统计周期粒度：1m、1h、1d、10m';
        COMMENT ON COLUMN public.t_stat_record.val_avg IS
          '统计窗口内监测数据算术平均值';
        COMMENT ON COLUMN public.t_stat_record.val_std IS
          '统计窗口内监测数据标准差';
        COMMENT ON COLUMN public.t_stat_record.max_time IS
          '统计窗口内最大值发生时间';
        COMMENT ON COLUMN public.t_stat_record.min_time IS
          '统计窗口内最小值发生时间';
        COMMENT ON COLUMN public.t_stat_record.sample_count IS
          '统计窗口内有效采样点数';
        COMMENT ON COLUMN public.t_stat_record.ext_json IS
          '扩展统计分析结果，使用JSON存储';
        COMMENT ON COLUMN public.t_stat_record.create_by IS
          '统计记录创建人或创建程序';
        COMMENT ON COLUMN public.t_stat_record.create_time IS
          '统计记录创建时间';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP COLUMN IF EXISTS create_by,
          DROP COLUMN IF EXISTS create_time
        """
    )
