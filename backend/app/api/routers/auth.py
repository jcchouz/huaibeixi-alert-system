from fastapi import APIRouter, Depends, HTTPException

from app.core.config import Settings, get_settings
from app.core.security import (
    Principal,
    SecurityService,
    TokenResponse,
    get_current_principal,
    get_security_service,
)
from app.schemas.auth import LoginRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    security: SecurityService = Depends(get_security_service),
) -> TokenResponse:
    if not settings.auth_enabled:
        raise HTTPException(status_code=400, detail="当前环境未启用认证")
    principal = security.authenticate(body.username, body.password)
    if principal is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return security.create_token(principal)


@router.get("/me", response_model=Principal)
def current_user(principal: Principal = Depends(get_current_principal)) -> Principal:
    return principal
