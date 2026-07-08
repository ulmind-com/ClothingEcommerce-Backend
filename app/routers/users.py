from fastapi import APIRouter, Body, Depends

from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import to_object_id

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/admin/list", dependencies=[Depends(require_admin)])
async def list_users(q: str | None = None, limit: int = 100):
    """Admin: search users (by name/email) to target a notification."""
    db = get_db()
    query: dict = {}
    if q:
        query = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
        ]}
    docs = await db.users.find(
        query, {"name": 1, "email": 1, "role": 1}
    ).sort("created_at", -1).to_list(length=min(max(limit, 1), 500))
    return [
        {"id": str(d["_id"]), "name": d.get("name", ""), "email": d.get("email", ""), "role": d.get("role", "user")}
        for d in docs
    ]


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
