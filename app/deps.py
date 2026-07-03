from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_access_token
from app.db.mongodb import get_db
from app.models.common import serialize, to_object_id

bearer = HTTPBearer(auto_error=True)
optional_bearer = HTTPBearer(auto_error=False)


async def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(optional_bearer),
) -> dict | None:
    """Like get_current_user but returns None instead of raising when no/invalid token."""
    if not creds:
        return None
    try:
        payload = decode_access_token(creds.credentials)
    except Exception:
        return None
    user_id = payload.get("sub")
    db = get_db()
    doc = await db.users.find_one({"_id": to_object_id(user_id)})
    return serialize(doc) if doc else None


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> dict:
    try:
        payload = decode_access_token(creds.credentials)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    db = get_db()
    doc = await db.users.find_one({"_id": to_object_id(user_id)})
    if not doc:
        raise HTTPException(status_code=401, detail="User not found")
    return serialize(doc)


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
