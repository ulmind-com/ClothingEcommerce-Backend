"""Notification service — stores an in-app copy (bell/inbox) and sends a push.

All sending goes through here so every channel behaves the same: persist to the
`notifications` collection, fan out via FCM, and prune dead device tokens. Push
is a no-op until Firebase is configured (see fcm_service), but the in-app inbox
still works, so the whole system is testable before Firebase is wired.
"""
from datetime import datetime, timedelta, timezone

from app.models.common import to_object_id
from app.services import fcm_service

# Statuses that mean a customer has actually ordered (used to target the
# first-order audience — mirrors the coupon gating).
_ORDERED = ["confirmed", "shipped", "out_for_delivery", "delivered"]

# How long after signing up we send the first-order welcome nudge.
WELCOME_DELAY_MIN = 10


async def _store(db, user_ids: list[str], title: str, body: str, data: dict | None, kind: str):
    now = datetime.now(timezone.utc)
    docs = [
        {
            "user_id": uid,
            "title": title,
            "body": body,
            "data": data or {},
            "kind": kind,
            "read": False,
            "created_at": now,
        }
        for uid in user_ids
    ]
    if docs:
        await db.notifications.insert_many(docs)


async def notify_users(db, user_ids, title, body, data=None, kind="general", store=True):
    """Deliver to specific users: save an inbox copy and push to their devices."""
    ids = list({str(u) for u in user_ids if u})
    if not ids:
        return
    if store:
        await _store(db, ids, title, body, data, kind)

    owner: dict[str, object] = {}
    tokens: list[str] = []
    cursor = db.users.find({"_id": {"$in": [to_object_id(u) for u in ids]}}, {"fcm_tokens": 1})
    async for u in cursor:
        for t in (u.get("fcm_tokens") or []):
            owner[t] = u["_id"]
            tokens.append(t)

    dead = await fcm_service.send_to_tokens(tokens, title, body, data)
    for t in dead:
        await db.users.update_one({"_id": owner[t]}, {"$pull": {"fcm_tokens": t}})


async def _first_order_user_ids(db) -> list[str]:
    ordered = set(str(x) for x in await db.orders.distinct("user_id", {"status": {"$in": _ORDERED}}))
    ids = []
    async for u in db.users.find({}, {"_id": 1}):
        if str(u["_id"]) not in ordered:
            ids.append(str(u["_id"]))
    return ids


async def notify_all(db, title, body, data=None, kind="general", only_first_order=False):
    """Broadcast to everyone, or only to customers still on their first order."""
    if only_first_order:
        ids = await _first_order_user_ids(db)
    else:
        ids = [str(u["_id"]) async for u in db.users.find({}, {"_id": 1})]
    await notify_users(db, ids, title, body, data, kind)


# ---------------------------------------------------------------------------
# Scheduled (delayed) notifications — a tiny durable queue swept every minute.
# ---------------------------------------------------------------------------

async def schedule(db, user_id: str, kind: str, delay_minutes: int, data: dict | None = None):
    await db.scheduled_notifications.insert_one({
        "user_id": str(user_id),
        "kind": kind,
        "data": data or {},
        "due_at": datetime.now(timezone.utc) + timedelta(minutes=delay_minutes),
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })


async def _active_first_order_coupon(db):
    from app.routers.coupons import _in_window
    async for c in db.coupons.find({"active": True, "first_order_only": True}):
        if _in_window(c):
            return c
    return None


async def _run_job(db, job: dict):
    kind = job.get("kind")
    uid = job.get("user_id")

    if kind == "first_order_welcome":
        # Only nudge if the customer is still on their first order AND a
        # first-order coupon is actually live right now.
        from app.routers.coupons import is_first_order
        if not await is_first_order(db, uid):
            return
        coupon = await _active_first_order_coupon(db)
        if not coupon:
            return
        code = coupon.get("code", "")
        if coupon.get("type") == "flat":
            off = f"₹{int(coupon.get('value', 0))} OFF"
        else:
            off = f"{int(coupon.get('value', 0))}% OFF"
        await notify_users(
            db, [uid],
            "🎁 A gift for your first order",
            f"Get {off} on your first order — use code {code}. Happy shopping!",
            {"type": "coupon", "code": code},
            kind="coupon",
        )


async def run_due(db) -> int:
    """Send every pending scheduled notification whose time has come."""
    now = datetime.now(timezone.utc)
    ran = 0
    cursor = db.scheduled_notifications.find({"status": "pending", "due_at": {"$lte": now}})
    async for job in cursor:
        # Claim it first so overlapping sweeps never double-send.
        claimed = await db.scheduled_notifications.find_one_and_update(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "running"}},
        )
        if not claimed:
            continue
        try:
            await _run_job(db, claimed)
            await db.scheduled_notifications.update_one(
                {"_id": job["_id"]}, {"$set": {"status": "done", "ran_at": now}}
            )
            ran += 1
        except Exception as e:  # pragma: no cover
            print(f"[notif] scheduled job failed: {e}")
            await db.scheduled_notifications.update_one(
                {"_id": job["_id"]}, {"$set": {"status": "error", "error": str(e)}}
            )
    return ran
