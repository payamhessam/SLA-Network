"""Authentication and role-based authorization.

Issues and verifies short-lived signed JWT bearer tokens, verifies credentials with a
constant-time comparison, and exposes FastAPI dependencies used to gate endpoints:
`current_user` (any authenticated caller), `administrator` (Administrator only), and
role checks for collection. These dependencies are the enforcement points for the
app's least-privilege model.
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated
import hmac

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

bearer = HTTPBearer(auto_error=False)
_ph = PasswordHasher()


def issue_token(username: str, role: str = "Administrator") -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": username, "role": role, "iat": now, "exp": now + timedelta(days=14)}, get_settings().signing_secret, algorithm="HS256")


def _verify(password: str, stored: str) -> bool:
    """Verify a credential. If the stored secret is an argon2 hash (starts with $argon2),
    verify against the hash (passwords encrypted at rest); otherwise fall back to a
    constant-time comparison for a plaintext secret file."""
    if stored.startswith("$argon2"):
        try:
            return _ph.verify(stored, password)
        except (Argon2Error, Exception):
            return False
    return hmac.compare_digest(password, stored)


def authenticate(username: str, password: str) -> str | None:
    settings = get_settings()
    if username == "admin":
        return "Administrator" if _verify(password, settings.admin_password) else None
    if username == "user":
        return "Read-Only Viewer" if _verify(password, settings.user_password) else None
    # Unknown username: still pay the same Argon2/compare cost a real check would, so response
    # latency can't be used to distinguish "no such user" from "wrong password" (timing side-channel).
    _verify(password, settings.admin_password)
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
