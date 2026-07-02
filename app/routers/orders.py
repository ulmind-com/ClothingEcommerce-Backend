from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import settings
from app.db.mongodb import get_db
from app.deps import get_current_user
from app.models.common import serialize, to_object_id
from app.models.order import OrderCreate, OrderVerify
from app.services import razorpay_service

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("")
async def create_order(body: OrderCreate, user: dict = Depends(get_current_user)):
    db = get_db()

    # Build authoritative order items from DB (never trust client prices).
    order_items = []
    amount = 0.0
    for it in body.items:
        prod = await db.products.find_one({"_id": to_object_id(it.product_id)})
        if not prod or not prod.get("is_active", True):
            raise HTTPException(status_code=400, detail="Product unavailable")
        line = prod["price"] * it.qty
        amount += line
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

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Empty order")

    amount_paise = round(amount * 100)

    # Insert our order first (status created).
    doc = {
        "user_id": user["id"],
        "items": order_items,
        "address": body.address.model_dump(),
        "amount": amount,
        "currency": "INR",
        "status": "created",
        "razorpay_order_id": None,
        "razorpay_payment_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.orders.insert_one(doc)
    our_id = str(res.inserted_id)

    # Create Razorpay order.
    try:
        rp = razorpay_service.create_order(amount_paise, receipt=our_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    await db.orders.update_one(
        {"_id": res.inserted_id}, {"$set": {"razorpay_order_id": rp["id"]}}
    )

    return {
        "order_id": our_id,
        "razorpay_order_id": rp["id"],
        "amount": amount_paise,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "prefill": {
            "name": body.address.name,
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
        await db.orders.update_one(
            {"_id": order["_id"]}, {"$set": {"status": "failed"}}
        )
        raise HTTPException(status_code=400, detail="Payment verification failed")

    await db.orders.update_one(
        {"_id": order["_id"]},
        {
            "$set": {
                "status": "paid",
                "razorpay_payment_id": body.razorpay_payment_id,
                "paid_at": datetime.now(timezone.utc),
            }
        },
    )
    return {"status": "paid", "order_id": body.order_id}


@router.get("")
async def my_orders(user: dict = Depends(get_current_user)):
    db = get_db()
    docs = (
        await db.orders.find({"user_id": user["id"]})
        .sort("created_at", -1)
        .to_list(length=100)
    )
    return [serialize(d) for d in docs]
