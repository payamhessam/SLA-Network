from datetime import datetime, timedelta, timezone
from typing import Annotated
import hmac

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

bearer = HTTPBearer(auto_error=False)


def issue_token(username: str, role: str = "Administrator") -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": username, "role": role, "iat": now, "exp": now + timedelta(days=14)}, get_settings().signing_secret, algorithm="HS256")


def authenticate(username: str, password: str) -> str | None:
    settings = get_settings()
    if username == "admin" and hmac.compare_digest(password, settings.admin_password): return "Administrator"
    if username == "user" and hmac.compare_digest(password, settings.user_password): return "Read-Only Viewer"
    return None


def current_user(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not credentials: raise HTTPException(401, "Authentication required")
    try:
        return jwt.decode(credentials.credentials, get_settings().signing_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token") from None


def administrator(user=Depends(current_user)):
    if user.get("role") != "Administrator": raise HTTPException(403, "Administrator role required")
    return user
