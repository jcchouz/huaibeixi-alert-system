from typing import Optional

from pydantic import BaseModel, Field


class EngineReplayRequest(BaseModel):
    action: str = Field(..., description="start | stop | step | trigger_test_alert | reset")
    replayTime: Optional[str] = None
    replay_time: Optional[str] = None
    speed: Optional[int] = None
    sensorNo: Optional[str] = None
    sensor_no: Optional[str] = None
    channel: Optional[str] = None
    alertLevel: Optional[int] = None
    alert_level: Optional[int] = None
    alertType: Optional[str] = None
    alert_type: Optional[str] = None
