"""增加阈值预警功能分类，并回填历史记录。

Revision ID: 20261124_0040
Revises: 20261120_0039
Create Date: 2026-11-24
"""

from alembic import op

revision = "20261124_0040"
down_revision = "20261120_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加阈值功能分类字段，并按传感器类型关系回填历史数据。"""
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
            ADD COLUMN IF NOT EXISTS alert_function integer
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_threshold AS threshold
        SET alert_function = sf.sort
        FROM public.t_sensor AS sensor
        JOIN public.t_sensor_type AS st ON st.id = sensor.type
        JOIN public.t_sensor_function AS sf ON sf.id = st.function_id
        WHERE LOWER(BTRIM(sensor.sensor_no)) =
              LOWER(BTRIM(threshold.sensor_no))
          AND sf.sort IN (1, 2)
          AND threshold.alert_function IS NULL
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record AS alert_record
        SET alert_function = sf.sort
        FROM public.t_sensor AS sensor
        JOIN public.t_sensor_type AS st ON st.id = sensor.type
        JOIN public.t_sensor_function AS sf ON sf.id = st.function_id
        WHERE LOWER(BTRIM(sensor.sensor_no)) =
              LOWER(BTRIM(alert_record.sensor_no))
          AND sf.sort IN (1, 2)
          AND alert_record.alert_function IS NULL
        """
    )
    # 采集仪可能包含多个功能分类的传感器，仅在分类唯一时回填。
    op.execute(
        """
        WITH instrument_functions AS (
            SELECT
                sensor.instrument_id,
                MIN(sf.sort) AS alert_function
            FROM public.t_sensor AS sensor
            JOIN public.t_sensor_type AS st ON st.id = sensor.type
            JOIN public.t_sensor_function AS sf ON sf.id = st.function_id
            WHERE sensor.instrument_id IS NOT NULL
              AND BTRIM(sensor.instrument_id) <> ''
              AND sf.sort IN (1, 2)
            GROUP BY sensor.instrument_id
            HAVING COUNT(DISTINCT sf.sort) = 1
        )
        UPDATE public.t_alert_record AS alert_record
        SET alert_function = instrument_functions.alert_function
        FROM instrument_functions
        WHERE alert_record.channel = '__DAQ__'
          AND alert_record.alert_function IS NULL
          AND LOWER(BTRIM(alert_record.sensor_no)) =
              LOWER(BTRIM(instrument_functions.instrument_id))
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.alert_function IS
            '预警功能分类：1=结构，2=环境'
        """
    )


def downgrade() -> None:
    """移除阈值表新增的预警功能分类字段。"""
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
            DROP COLUMN IF EXISTS alert_function
        """
    )
