from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import HTTPException

from app.core.config import Settings
from app.domain.engine import AlertAnalyticsEngine
from app.schemas.engine import EngineReplayRequest
from app.services.common import parse_datetime, pick

RETIRED_ALERT_TYPES = {"RATE", "SPIKE"}
REMOVED_SAMPLING_GAP_TYPES = {"GAP_1000MS", "采样断流"}


class EngineControlService:
    def __init__(self, engine: AlertAnalyticsEngine, settings: Settings):
        self.engine = engine
        self.settings = settings

    def control(self, body: EngineReplayRequest) -> Dict[str, Any]:
        action = (body.action or "").strip().lower()
        if action in ("trigger_test_alert", "simulate_alert", "demo", "reset"):
            if not self.settings.demo_endpoints_enabled:
                raise HTTPException(status_code=404, detail="演示接口已在当前环境关闭")
        replay_time = pick(body.replayTime, body.replay_time)
        if replay_time:
            self.engine.current_watermark = parse_datetime(
                replay_time,
                self.engine.current_watermark,
            )
        if body.speed in (1, 2, 5, 10, 20):
            self.engine.speed_multiplier = int(body.speed)

        result_data: Dict[str, Any] = {}
        if action == "start":
            self.engine.mode = "REPLAY"
            self.engine.is_running = True
            step_result = self.engine.step_detection(
                self.engine.current_watermark,
                self.engine.current_watermark + timedelta(minutes=1),
            )
            result_data = {
                "message": "历史回放/实时流式检测引擎已启动",
                "stepResult": step_result,
            }
        elif action == "stop":
            self.engine.is_running = False
            result_data = {"message": "流式检测引擎已暂停"}
        elif action == "step":
            self._reject_retired_alert_type(pick(body.alertType, body.alert_type))
            step_result = self.engine.step_detection(
                self.engine.current_watermark,
                self.engine.current_watermark
                + timedelta(minutes=max(1, self.engine.speed_multiplier)),
            )
            if step_result["triggeredAlertsCount"] == 0:
                demo_alert = self.engine.trigger_demo_alert(
                    sensor_no=pick(body.sensorNo, body.sensor_no),
                    channel=body.channel,
                    alert_level=pick(body.alertLevel, body.alert_level),
                    alert_type=pick(body.alertType, body.alert_type),
                )
                step_result["triggeredAlerts"] = [demo_alert]
                step_result["triggeredAlertsCount"] = 1
            result_data = {
                "message": "单步检测窗口执行完成",
                "stepResult": step_result,
            }
        elif action in ("trigger_test_alert", "simulate_alert", "demo"):
            self._reject_retired_alert_type(pick(body.alertType, body.alert_type))
            demo_alert = self.engine.trigger_demo_alert(
                sensor_no=pick(body.sensorNo, body.sensor_no),
                channel=body.channel,
                alert_level=pick(body.alertLevel, body.alert_level),
                alert_type=pick(body.alertType, body.alert_type),
            )
            result_data = {
                "message": "已触发实时模拟报警并推送至前端弹窗流",
                "alert": demo_alert,
            }
        elif action == "reset":
            self.engine.is_running = False
            self.engine.current_watermark = datetime.strptime(
                self.settings.replay_start_watermark,
                "%Y-%m-%d %H:%M:%S",
            )
            result_data = {
                "message": (
                    "回放时钟水位线已重置至 "
                    f"{self.settings.replay_start_watermark}"
                )
            }
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

        return {
            "success": True,
            "action": action,
            "engineStatus": self.engine.get_status(),
            **result_data,
        }

    @staticmethod
    def _reject_retired_alert_type(alert_type: Any) -> None:
        normalized = str(alert_type).strip().upper() if alert_type else ""
        if normalized in REMOVED_SAMPLING_GAP_TYPES:
            raise HTTPException(
                status_code=400,
                detail="采样断流告警已删除，不再支持触发",
            )
        if normalized in RETIRED_ALERT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Alert type {normalized} is no longer supported",
            )
