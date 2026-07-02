from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from app.core.config import settings
from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id
from app.models.order import OrderCreate, OrderVerify
from app.services import razorpay_service

router = APIRouter(prefix="/orders", tags=["orders"])

# --- Pricing rules ---
DELIVERY_FEE = 6.0
FREE_DELIVERY_ABOVE = 200.0
TAX_RATE = 0.05
COUPONS = {"WELCOMEOFFER": 0.10}  # code -> fraction off subtotal

STAGES = ["placed", "confirmed", "shipped", "out_for_delivery", "delivered"]


def _price(subtotal: float, coupon: str | None):
    rate = COUPONS.get((coupon or "").upper(), 0.0)
    discount = round(subtotal * rate, 2)
    delivery = 0.0 if subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_FEE
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal - discount + delivery + tax, 2)
    return {
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "delivery": delivery,
        "tax": tax,
        "total": total,
        "coupon_applied": rate > 0,
    }


@router.post("")
async def create_order(body: OrderCreate, user: dict = Depends(get_current_user)):
    db = get_db()

    order_items = []
    subtotal = 0.0
    for it in body.items:
        prod = await db.products.find_one({"_id": to_object_id(it.product_id)})
        if not prod or not prod.get("is_active", True):
            raise HTTPException(status_code=400, detail="Product unavailable")
        subtotal += prod["price"] * it.qty
        order_items.append(
            {
                "product_id": it.product_id,
                "title": prod["title"],
                "price": prod["price"],
                "qty": it.qty,
                "color": it.color,
                "size": it.size,
                "image": (prod.get("images") or [None])[0],
            }
        )

    if subtotal <= 0:
        raise HTTPException(status_code=400, detail="Empty order")

    bill = _price(subtotal, body.coupon_code)
    is_cod = body.payment_method == "cod"

    doc = {
        "user_id": user["id"],
        "items": order_items,
        "address": body.address.model_dump(),
        "payment_method": "cod" if is_cod else "online",
        "coupon_code": body.coupon_code,
        **bill,
        "amount": bill["total"],
        "currency": "INR",
        "status": "confirmed" if is_cod else "placed",
        "razorpay_order_id": None,
        "razorpay_payment_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.orders.insert_one(doc)
    our_id = str(res.inserted_id)

    if is_cod:
        return {
            "order_id": our_id,
            "payment_method": "cod",
            "status": "confirmed",
            "bill": bill,
        }

    # Online -> Razorpay
    try:
        rp = razorpay_service.create_order(round(bill["total"] * 100), receipt=our_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    await db.orders.update_one(
        {"_id": res.inserted_id}, {"$set": {"razorpay_order_id": rp["id"]}}
    )

    return {
        "order_id": our_id,
        "payment_method": "online",
        "razorpay_order_id": rp["id"],
        "amount": round(bill["total"] * 100),
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
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
        {
            "$set": {
                "status": "confirmed",
                "razorpay_payment_id": body.razorpay_payment_id,
                "paid_at": datetime.now(timezone.utc),
            }
        },
    )
    return {"status": "confirmed", "order_id": body.order_id}


@router.get("")
async def my_orders(user: dict = Depends(get_current_user)):
    db = get_db()
    docs = (
        await db.orders.find({"user_id": user["id"]})
        .sort("created_at", -1)
        .to_list(length=100)
    )
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
    return serialize(res)
