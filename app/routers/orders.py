from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from app.core.config import settings as app_settings
from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id
from app.models.order import OrderCreate, OrderVerify
from app.services import fcm_service, razorpay_service
from app.services.pricing import (
    compute_delivery,
    get_settings,
    haversine_km,
    product_final_price,
)

router = APIRouter(prefix="/orders", tags=["orders"])

COUPONS = {"WELCOMEOFFER": 0.10}
STAGES = ["placed", "confirmed", "shipped", "out_for_delivery", "delivered"]

STATUS_MSG = {
    "confirmed": "Your order is confirmed and being packed 📦",
    "shipped": "Your order has been shipped 🚚",
    "out_for_delivery": "Your order is out for delivery 🛵",
    "delivered": "Your order has been delivered ✅",
    "cancelled": "Your order was cancelled",
}


async def _build_bill(db, items_in, address, coupon):
    settings = await get_settings(db)
    order_items = []
    subtotal = 0.0
    for it in items_in:
        prod = await db.products.find_one({"_id": to_object_id(it.product_id)})
        if not prod or not prod.get("is_active", True):
            raise HTTPException(status_code=400, detail="Product unavailable")
        unit = product_final_price(prod)["final_price"]
        subtotal += unit * it.qty
        order_items.append(
            {
                "product_id": it.product_id,
                "title": prod["title"],
                "price": unit,
                "qty": it.qty,
                "color": it.color,
                "size": it.size,
                "image": (prod.get("images") or [None])[0],
            }
        )

    rate = COUPONS.get((coupon or "").upper(), 0.0)
    discount = round(subtotal * rate, 2)

    # distance-based delivery
    distance = None
    if settings.shop.lat is not None and address.lat is not None:
        distance = haversine_km(settings.shop.lat, settings.shop.lng, address.lat, address.lng)
    deliv = compute_delivery(settings, subtotal - discount, distance)

    taxable = subtotal - discount
    tax = round(taxable * settings.tax_rate, 2)
    total = round(taxable + deliv["fee"] + tax, 2)

    bill = {
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "delivery": deliv["fee"],
        "delivery_free": deliv["free"],
        "distance_km": deliv["distance_km"],
        "deliverable": deliv["deliverable"],
        "tax": tax,
        "tax_rate": settings.tax_rate,
        "total": total,
        "currency": settings.currency,
        "coupon_applied": rate > 0,
    }
    return order_items, bill


@router.post("/quote")
async def quote(body: OrderCreate, user: dict = Depends(get_current_user)):
    """Preview the bill (delivery/tax/total) before placing the order."""
    db = get_db()
    _, bill = await _build_bill(db, body.items, body.address, body.coupon_code)
    return bill


async def _decrement_stock(db, order_items):
    for it in order_items:
        prod = await db.products.find_one({"_id": to_object_id(it["product_id"])})
        if not prod:
            continue
        colors = prod.get("colors") or []
        if colors and it.get("color"):
            for c in colors:
                if c.get("name") == it["color"]:
                    c["stock"] = max(0, int(c.get("stock", 0)) - it["qty"])
            await db.products.update_one({"_id": prod["_id"]}, {"$set": {"colors": colors}})
        else:
            await db.products.update_one({"_id": prod["_id"]}, {"$inc": {"stock": -it["qty"]}})


@router.post("")
async def create_order(body: OrderCreate, user: dict = Depends(get_current_user)):
    db = get_db()
    order_items, bill = await _build_bill(db, body.items, body.address, body.coupon_code)
    if not bill["deliverable"]:
        raise HTTPException(status_code=400, detail="Address is outside the delivery area")
    if bill["subtotal"] <= 0:
        raise HTTPException(status_code=400, detail="Empty order")

    is_cod = body.payment_method == "cod"
    doc = {
        "user_id": user["id"],
        "items": order_items,
        "address": body.address.model_dump(),
        "payment_method": "cod" if is_cod else "online",
        "coupon_code": body.coupon_code,
        **bill,
        "amount": bill["total"],
        "status": "confirmed" if is_cod else "placed",
        "razorpay_order_id": None,
        "razorpay_payment_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.orders.insert_one(doc)
    our_id = str(res.inserted_id)

    if is_cod:
        await _decrement_stock(db, order_items)
        await fcm_service.notify_user(db, user["id"], "Order placed 🎉",
                                      "Your COD order is confirmed.", {"order_id": our_id})
        return {"order_id": our_id, "payment_method": "cod", "status": "confirmed", "bill": bill}

    try:
        rp = razorpay_service.create_order(round(bill["total"] * 100), receipt=our_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    await db.orders.update_one({"_id": res.inserted_id}, {"$set": {"razorpay_order_id": rp["id"]}})

    return {
        "order_id": our_id,
        "payment_method": "online",
        "razorpay_order_id": rp["id"],
        "amount": round(bill["total"] * 100),
        "currency": bill["currency"],
        "key_id": app_settings.RAZORPAY_KEY_ID,
        "bill": bill,
        "prefill": {
            "name": body.address.name or user.get("name", ""),
            "contact": body.address.phone,
            "email": user.get("email", ""),
        },
    }


@router.post("/verify")
async def verify_order(body: OrderVerify, user: dict = Depends(get_current_user)):
    db = get_db()
    order = await db.orders.find_one({"_id": to_object_id(body.order_id)})
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    ok = razorpay_service.verify_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    )
    if not ok:
        await db.orders.update_one({"_id": order["_id"]}, {"$set": {"status": "cancelled"}})
        raise HTTPException(status_code=400, detail="Payment verification failed")

    await db.orders.update_one(
        {"_id": order["_id"]},
        {"$set": {"status": "confirmed", "razorpay_payment_id": body.razorpay_payment_id,
                  "paid_at": datetime.now(timezone.utc)}},
    )
    await _decrement_stock(db, order["items"])
    await fcm_service.notify_user(db, user["id"], "Payment successful 🎉",
                                  "Your order is confirmed.", {"order_id": body.order_id})
    return {"status": "confirmed", "order_id": body.order_id}


@router.get("")
async def my_orders(user: dict = Depends(get_current_user)):
    db = get_db()
    docs = await db.orders.find({"user_id": user["id"]}).sort("created_at", -1).to_list(length=100)
    return [serialize(d) for d in docs]


@router.get("/admin/all", dependencies=[Depends(require_admin)])
async def all_orders(status: str | None = None):
    db = get_db()
    q = {"status": status} if status else {}
    docs = await db.orders.find(q).sort("created_at", -1).to_list(length=500)
    return [serialize(d) for d in docs]


@router.get("/{order_id}")
async def get_order(order_id: str, user: dict = Depends(get_current_user)):
    db = get_db()
    doc = await db.orders.find_one({"_id": to_object_id(order_id)})
    if not doc or (doc["user_id"] != user["id"] and user.get("role") != "admin"):
        raise HTTPException(status_code=404, detail="Order not found")
    return serialize(doc)


@router.patch("/{order_id}/status", dependencies=[Depends(require_admin)])
async def update_status(order_id: str, status: str = Body(..., embed=True)):
    if status not in STAGES + ["cancelled"]:
        raise HTTPException(status_code=400, detail="Invalid status")
    db = get_db()
    res = await db.orders.find_one_and_update(
        {"_id": to_object_id(order_id)}, {"$set": {"status": status}}, return_document=True
    )
    if not res:
        raise HTTPException(status_code=404, detail="Order not found")

    # Count units sold once, when the order is delivered.
    if status == "delivered" and not res.get("sold_counted"):
        for it in res.get("items", []):
            await db.products.update_one(
                {"_id": to_object_id(it["product_id"])}, {"$inc": {"sold_count": it["qty"]}}
            )
        await db.orders.update_one({"_id": res["_id"]}, {"$set": {"sold_counted": True}})

    await fcm_service.notify_user(db, res["user_id"], "Order update",
                                  STATUS_MSG.get(status, f"Status: {status}"), {"order_id": order_id})
    return serialize(res)
