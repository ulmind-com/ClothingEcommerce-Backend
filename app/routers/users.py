from fastapi import APIRouter, Body, Depends

from app.db.mongodb import get_db
from app.deps import get_current_user
from app.models.common import to_object_id

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/fcm-token")
async def register_fcm_token(
    token: str = Body(..., embed=True), user: dict = Depends(get_current_user)
):
    db = get_db()
    await db.users.update_one(
        {"_id": to_object_id(user["id"])}, {"$addToSet": {"fcm_tokens": token}}
    )
    return {"ok": True}


@router.delete("/fcm-token")
async def remove_fcm_token(
    token: str = Body(..., embed=True), user: dict = Depends(get_current_user)
):
    db = get_db()
    await db.users.update_one(
        {"_id": to_object_id(user["id"])}, {"$pull": {"fcm_tokens": token}}
    )
    return {"ok": True}
