from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id
from app.services import notifications, razorpay_service
from app.services.pricing import get_settings

router = APIRouter(prefix="/returns", tags=["returns"])

# Admin-driven lifecycle of a return request.
RETURN_STATUSES = ["requested", "approved", "rejected", "picked_up", "refunded", "exchanged"]

STATUS_MSG = {
    "approved": "Your return request was approved. We'll arrange a pickup.",
    "rejected": "Your return request was declined.",
    "picked_up": "We've picked up your return item.",
    "refunded": "Your refund has been processed for the returned item(s).",
    "exchanged": "Your exchange is on the way.",
}


class ReturnItemIn(BaseModel):
    product_id: str
    qty: int = Field(ge=1)
    color: str | None = None
    size: str | None = None


class ReturnCreate(BaseModel):
    order_id: str
    type: str = "refund"           # "refund" | "exchange"
    reason: str = ""
    note: str = ""                 # e.g. desired size/colour for an exchange
    items: list[ReturnItemIn]


def _deadline(order: dict, window_days: float) -> datetime:
    base = order.get("delivered_at") or order.get("created_at")
    if base and base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base or datetime.now(timezone.utc)) + timedelta(days=window_days)


@router.post("")
async def create_return(body: ReturnCreate, user: dict = Depends(get_current_user)):
    """Customer requests a return/exchange for delivered items within the window."""
    db = get_db()
    order = await db.orders.find_one({"_id": to_object_id(body.order_id)})
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("status") != "delivered":
        raise HTTPException(status_code=400, detail="Only delivered orders can be returned")

    settings = await get_settings(db)
    global_days = settings.return_window_days or 0

    if body.type not in ("refund", "exchange"):
        raise HTTPException(status_code=400, detail="Invalid return type")
    if not body.items:
        raise HTTPException(status_code=400, detail="Select at least one item to return")

    order_items = order.get("items", [])
    resolved = []
    amount = 0.0
    for req in body.items:
        match = next(
            (
                oi for oi in order_items
                if oi.get("product_id") == req.product_id
                and (oi.get("color") or None) == (req.color or None)
                and (oi.get("size") or None) == (req.size or None)
            ),
            None,
        )
        if not match:
            raise HTTPException(status_code=400, detail="Item is not part of this order")
        if req.qty > int(match.get("qty", 0)):
            raise HTTPException(status_code=400, detail="Return quantity exceeds the ordered quantity")

        # Per-product return policy governs eligibility.
        try:
            prod = await db.products.find_one({"_id": to_object_id(req.product_id)})
        except Exception:
            prod = None
        title = match.get("title")
        if prod is not None and not prod.get("returnable", True):
            raise HTTPException(status_code=400, detail=f"'{title}' is not eligible for return")
        item_days = (prod.get("return_days") if prod else 0) or global_days
        if item_days <= 0:
            raise HTTPException(status_code=400, detail=f"Returns aren't available for '{title}'")
        if datetime.now(timezone.utc) > _deadline(order, item_days):
            raise HTTPException(status_code=400, detail=f"The return window for '{title}' has passed")

        line = float(match.get("price", 0)) * req.qty
        amount += line
        resolved.append({
            "product_id": req.product_id,
            "title": match.get("title"),
            "qty": req.qty,
            "color": req.color,
            "size": req.size,
            "price": float(match.get("price", 0)),
            "image": match.get("image"),
        })

    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user["id"],
        "order_id": body.order_id,
        "order_short": str(order["_id"])[-6:].upper(),
        "type": body.type,
        "reason": body.reason.strip(),
        "note": body.note.strip(),
        "items": resolved,
        "amount": round(amount, 2),
        "payment_method": order.get("payment_method"),
        "razorpay_payment_id": order.get("razorpay_payment_id"),
        "status": "requested",
        "created_at": now,
        "updated_at": now,
    }
    res = await db.returns.insert_one(doc)
    doc["_id"] = res.inserted_id

    await notifications.notify_users(
        db, [user["id"]], "Return requested",
        f"We've received your {body.type} request for order #{doc['order_short']}.",
        {"type": "return", "return_id": str(res.inserted_id)}, kind="order",
    )
    return serialize(doc)


@router.get("")
async def my_returns(user: dict = Depends(get_current_user)):
    db = get_db()
    docs = await db.returns.find({"user_id": user["id"]}).sort("created_at", -1).to_list(length=100)
    return [serialize(d) for d in docs]


@router.get("/admin/all", dependencies=[Depends(require_admin)])
async def all_returns(status: str | None = None):
    db = get_db()
    q = {"status": status} if status else {}
    docs = await db.returns.find(q).sort("created_at", -1).to_list(length=500)
    # Enrich each request with the parent order's payment + coupon details so the
    # admin sees the full transaction (txn id, razorpay order id, paid at, coupon
    # + discount, customer) right here — same info as the Orders/Refunds screens.
    order_cache: dict = {}
    out = []
    for d in docs:
        oid = d.get("order_id")
        order = order_cache.get(oid) if oid else None
        if oid and oid not in order_cache:
            try:
                order = await db.orders.find_one({"_id": to_object_id(oid)})
            except Exception:
                order = None
            order_cache[oid] = order
        if order:
            d["razorpay_order_id"] = order.get("razorpay_order_id")
            d["paid_at"] = order.get("paid_at")
            d["coupon_code"] = order.get("coupon_code")
            d["coupon_discount"] = order.get("discount")
            d["order_amount"] = order.get("amount")
            d["order_created_at"] = order.get("created_at")
            d["address"] = order.get("address")
        out.append(serialize(d))
    return out


@router.patch("/{return_id}", dependencies=[Depends(require_admin)])
async def update_return(
    return_id: str,
    status: str | None = Body(None),
    admin_note: str | None = Body(None),
):
    db = get_db()
    ret = await db.returns.find_one({"_id": to_object_id(return_id)})
    if not ret:
        raise HTTPException(status_code=404, detail="Return not found")

    update: dict = {"updated_at": datetime.now(timezone.utc)}
    if admin_note is not None:
        update["admin_note"] = admin_note
    if status is not None:
        if status not in RETURN_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        update["status"] = status

    # Marking a refund-type return as refunded -> initiate a partial Razorpay
    # refund of the returned amount for an online payment.
    if status == "refunded" and ret.get("type") == "refund" and ret.get("status") != "refunded":
        if ret.get("payment_method") == "online" and ret.get("razorpay_payment_id"):
            try:
                r = razorpay_service.refund(ret["razorpay_payment_id"], int(round(ret["amount"] * 100)))
                update["refund_id"] = r.get("id")
                update["refund_status"] = "initiated"
            except Exception as e:  # pragma: no cover
                print(f"[returns] refund failed: {e}")
                update["refund_status"] = "failed"
                raise HTTPException(status_code=502, detail=f"Refund failed: {e}")

    res = await db.returns.find_one_and_update(
        {"_id": to_object_id(return_id)}, {"$set": update}, return_document=True
    )

    if status and status in STATUS_MSG:
        await notifications.notify_users(
            db, [ret["user_id"]], "Return update", STATUS_MSG[status],
            {"type": "return", "return_id": return_id}, kind="order",
        )
    return serialize(res)
