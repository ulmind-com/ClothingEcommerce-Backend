from fastapi import APIRouter, Body, Depends, Header, HTTPException

from app.core.config import settings
from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id
from app.services import notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def my_notifications(
    limit: int = 10, skip: int = 0, user: dict = Depends(get_current_user)
):
    """In-app inbox: this user's notifications, newest first (paginated)."""
    db = get_db()
    docs = (
        await db.notifications.find({"user_id": user["id"]})
        .sort("created_at", -1)
        .skip(max(skip, 0))
        .limit(min(max(limit, 1), 50))
        .to_list(length=min(max(limit, 1), 50))
    )
    return [serialize(d) for d in docs]


@router.get("/health")
async def notif_health():
    """Public: is FCM push configured on the server? (boolean only, no secrets)."""
    from app.services import fcm_service
    return {"fcm_ready": fcm_service._ensure_app()}


@router.get("/unread-count")
async def unread_count(user: dict = Depends(get_current_user)):
    db = get_db()
    n = await db.notifications.count_documents({"user_id": user["id"], "read": False})
    return {"count": n}


@router.post("/read-all")
async def read_all(user: dict = Depends(get_current_user)):
    db = get_db()
    await db.notifications.update_many(
        {"user_id": user["id"], "read": False}, {"$set": {"read": True}}
    )
    return {"ok": True}


@router.patch("/{notif_id}/read")
async def read_one(notif_id: str, user: dict = Depends(get_current_user)):
    db = get_db()
    await db.notifications.update_one(
        {"_id": to_object_id(notif_id), "user_id": user["id"]}, {"$set": {"read": True}}
    )
    return {"ok": True}


@router.delete("")
async def clear_all(user: dict = Depends(get_current_user)):
    db = get_db()
    await db.notifications.delete_many({"user_id": user["id"]})
    return {"ok": True}


@router.delete("/{notif_id}")
async def delete_one(notif_id: str, user: dict = Depends(get_current_user)):
    db = get_db()
    await db.notifications.delete_one({"_id": to_object_id(notif_id), "user_id": user["id"]})
    return {"ok": True}


@router.post("/send", dependencies=[Depends(require_admin)])
async def admin_send(
    title: str = Body(...),
    body: str = Body(...),
    target: str = Body("all"),          # "all" | "first_order" | "user"
    user_id: str | None = Body(None),
    data: dict | None = Body(None),
):
    """Admin broadcast: to everyone, first-order customers, or a single user."""
    db = get_db()
    if target == "user":
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required for a single-user send")
        await notifications.notify_users(db, [user_id], title, body, data, kind="admin")
    elif target == "first_order":
        await notifications.notify_all(db, title, body, data, kind="admin", only_first_order=True)
    else:
        await notifications.notify_all(db, title, body, data, kind="admin")
    return {"ok": True}


@router.post("/run-due")
async def run_due(x_cron_secret: str | None = Header(None)):
    """Trigger the scheduled-notification sweep (for an external cron).

    Guarded by CRON_SECRET when set; if it's empty the endpoint is disabled.
    """
    if not settings.CRON_SECRET or x_cron_secret != settings.CRON_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    sent = await notifications.run_due(get_db())
    return {"ran": sent}
