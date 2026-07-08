"""Order actions the support agent can perform on the customer's behalf.

Scoped to a single order + user, so the AI can only act on the order the chat
is about. Each raises ValueError (with a customer-friendly message) on failure.
"""
from datetime import datetime, timedelta, timezone

from app.models.common import to_object_id
from app.services import notifications, razorpay_service
from app.services.pricing import get_settings

CANCELLABLE = ["placed", "confirmed"]


async def _restore_stock(db, items):
    for it in items:
        try:
            prod = await db.products.find_one({"_id": to_object_id(it["product_id"])})
        except Exception:
            prod = None
        if not prod:
            continue
        colors = prod.get("colors") or []
        if colors and it.get("color"):
            for c in colors:
                if c.get("name") == it["color"]:
                    sizes = c.get("sizes") or []
                    if sizes and it.get("size"):
                        for ss in sizes:
                            if ss.get("size") == it["size"]:
                                ss["stock"] = int(ss.get("stock", 0)) + it["qty"]
                    else:
                        c["stock"] = int(c.get("stock", 0)) + it["qty"]
            await db.products.update_one({"_id": prod["_id"]}, {"$set": {"colors": colors}})
        else:
            await db.products.update_one({"_id": prod["_id"]}, {"$inc": {"stock": it["qty"]}})


def _aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def can_cancel(db, order, settings) -> bool:
    if order["status"] not in CANCELLABLE:
        return False
    window = settings.cancel_window_hours or 0
    if window <= 0:
        return False
    created = _aware(order.get("created_at"))
    return not (created and datetime.now(timezone.utc) - created > timedelta(hours=window))


async def eligible_return_items(db, order, settings) -> tuple[list, float]:
    """Items of a delivered order still within their per-product return window."""
    if order.get("status") != "delivered":
        return [], 0.0
    global_days = settings.return_window_days or 0
    base = _aware(order.get("delivered_at") or order.get("created_at"))
    now = datetime.now(timezone.utc)
    items, amount = [], 0.0
    for it in order.get("items", []):
        try:
            prod = await db.products.find_one({"_id": to_object_id(it["product_id"])})
        except Exception:
            prod = None
        if prod is not None and not prod.get("returnable", True):
            continue
        days = (prod.get("return_days") if prod else 0) or global_days
        if days <= 0:
            continue
        if base and now > base + timedelta(days=days):
            continue
        items.append({
            "product_id": it["product_id"], "title": it.get("title"), "qty": int(it.get("qty", 1)),
            "color": it.get("color"), "size": it.get("size"), "price": float(it.get("price", 0)),
            "image": it.get("image"),
        })
        amount += float(it.get("price", 0)) * int(it.get("qty", 1))
    return items, round(amount, 2)


async def cancel(db, user_id: str, order_id: str) -> dict:
    order = await db.orders.find_one({"_id": to_object_id(order_id)})
    if not order or order["user_id"] != user_id:
        raise ValueError("I couldn't find that order on your account.")
    settings = await get_settings(db)
    if not await can_cancel(db, order, settings):
        raise ValueError("This order can no longer be cancelled (it may have shipped or the window has passed).")

    update: dict = {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}
    refunded = False
    if order.get("payment_method") == "online" and order.get("razorpay_payment_id"):
        try:
            r = razorpay_service.refund(order["razorpay_payment_id"], int(round(order["amount"] * 100)))
            update["refund_id"] = r.get("id")
            update["refund_status"] = "initiated"
            refunded = True
        except Exception:
            update["refund_status"] = "failed"
    await db.orders.update_one({"_id": order["_id"]}, {"$set": update})
    if order["status"] == "confirmed":
        await _restore_stock(db, order["items"])
    await notifications.notify_users(
        db, [user_id], "Order cancelled",
        "Your order has been cancelled." + (" Your refund has been initiated." if refunded else ""),
        {"type": "order", "order_id": order_id}, kind="order",
    )
    return {"ok": True, "refunded": refunded}


async def create_return(db, user_id: str, order_id: str, rtype: str, reason: str, note: str = "") -> dict:
    order = await db.orders.find_one({"_id": to_object_id(order_id)})
    if not order or order["user_id"] != user_id:
        raise ValueError("I couldn't find that order on your account.")
    settings = await get_settings(db)
    items, amount = await eligible_return_items(db, order, settings)
    if not items:
        raise ValueError("None of the items in this order are eligible for return.")
    rtype = rtype if rtype in ("refund", "exchange") else "refund"

    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "order_id": order_id,
        "order_short": str(order["_id"])[-6:].upper(),
        "type": rtype,
        "reason": (reason or "").strip(),
        "note": (note or "").strip(),
        "items": items,
        "amount": amount,
        "payment_method": order.get("payment_method"),
        "razorpay_payment_id": order.get("razorpay_payment_id"),
        "status": "requested",
        "created_at": now,
        "updated_at": now,
    }
    res = await db.returns.insert_one(doc)
    await notifications.notify_users(
        db, [user_id], "Return requested",
        f"We've received your {rtype} request for order #{doc['order_short']}.",
        {"type": "return", "return_id": str(res.inserted_id)}, kind="order",
    )
    return {"ok": True, "type": rtype, "amount": amount, "count": len(items)}
