from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from pydantic import BaseModel

from app.core.config import Settings, get_settings

logger = logging.getLogger("huaibeixi.security")

bearer_scheme = HTTPBearer(auto_error=False)


class Principal(BaseModel):
    username: str
    role: str
    auth_mode: str = "jwt"

    def has_any_role(self, roles: Iterable[str]) -> bool:
        return self.role in set(roles)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: Principal


class SecurityService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.password_hash = PasswordHash.recommended()
        self.users = {
            str(item.get("username", "")).strip(): {
                "password_hash": str(item.get("password_hash", "")),
                "role": str(item.get("role", "viewer")).strip().lower(),
            }
            for item in settings.auth_users
            if str(item.get("username", "")).strip()
        }

    def authenticate(self, username: str, password: str) -> Principal | None:
        user = self.users.get(username.strip())
        if not user or not user["password_hash"]:
            return None
        try:
            valid = self.password_hash.verify(password, user["password_hash"])
        except Exception:
            logger.warning("Password verification failed for user=%s", username)
            return None
        if not valid:
            return None
        return Principal(username=username.strip(), role=user["role"])

    def create_token(self, principal: Principal) -> TokenResponse:
        if not self.settings.jwt_secret:
            raise RuntimeError("JWT_SECRET is required when authentication is enabled")

        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=self.settings.jwt_expire_minutes)
        token = jwt.encode(
            {
                "sub": principal.username,
                "role": principal.role,
                "iss": self.settings.jwt_issuer,
                "aud": self.settings.jwt_audience,
                "iat": now,
                "exp": expires,
            },
            self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
        )
        return TokenResponse(
            access_token=token,
            expires_in=self.settings.jwt_expire_minutes * 60,
            user=principal,
        )

    def decode_token(self, token: str) -> Principal:
        if not self.settings.jwt_secret:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="认证服务未配置")
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=[self.settings.jwt_algorithm],
                issuer=self.settings.jwt_issuer,
                audience=self.settings.jwt_audience,
            )
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录已过期",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的访问令牌",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        return Principal(
            username=str(payload.get("sub") or ""),
            role=str(payload.get("role") or "viewer"),
        )


def get_security_service(settings: Settings = Depends(get_settings)) -> SecurityService:
    return SecurityService(settings)


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    security: SecurityService = Depends(get_security_service),
) -> Principal:
    if not settings.auth_enabled:
        return Principal(username="local-admin", role="admin", auth_mode="disabled")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return security.decode_token(credentials.credentials)


def require_roles(*roles: str):
    allowed = set(roles)

    def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not principal.has_any_role(allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前账号无权执行该操作",
            )
        return principal

    return dependency
