from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from app.core.config import settings as app_settings
from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id
from app.models.order import OrderCreate, OrderVerify
from app.routers.coupons import get_coupon
from app.services import fcm_service, razorpay_service
from app.services.pricing import (
    compute_delivery,
    coupon_discount,
    get_settings,
    haversine_km,
    resolve_price,
)

router = APIRouter(prefix="/orders", tags=["orders"])

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
    default_pct = (settings.tax_rate or 0) * 100  # store-wide fallback GST %
    order_items = []
    lines = []  # (line_total, tax_pct)
    subtotal = 0.0
    for it in items_in:
        prod = await db.products.find_one({"_id": to_object_id(it.product_id)})
        if not prod or not prod.get("is_active", True):
            raise HTTPException(status_code=400, detail="Product unavailable")
        # Price for the exact colour + size the customer chose (variant-aware).
        unit = resolve_price(prod, it.color, it.size)["final_price"]
        line_total = unit * it.qty
        subtotal += line_total
        pct = prod.get("tax_pct")
        pct = float(pct) if pct is not None else default_pct  # per-product GST, else default
        lines.append((line_total, pct))
        order_items.append(
            {
                "product_id": it.product_id,
                "title": prod["title"],
                "price": unit,
                "qty": it.qty,
                "color": it.color,
                "size": it.size,
                "tax_pct": pct,
                "image": (prod.get("images") or [None])[0],
            }
        )

    coupon_doc = await get_coupon(db, coupon)
    discount = coupon_discount(coupon_doc, subtotal) if coupon_doc else 0.0

    # distance-based delivery
    distance = None
    if settings.shop.lat is not None and address.lat is not None:
        distance = haversine_km(settings.shop.lat, settings.shop.lng, address.lat, address.lng)
    deliv = compute_delivery(settings, subtotal - discount, distance)

    # Per-product GST: tax each line at its own rate, on its post-discount share.
    total_tax = 0.0
    rate_map: dict[float, dict] = {}
    for line_total, pct in lines:
        share = (line_total / subtotal * discount) if subtotal else 0.0
        line_taxable = max(0.0, line_total - share)
        line_tax = line_taxable * pct / 100
        total_tax += line_tax
        r = rate_map.setdefault(pct, {"rate": pct, "taxable": 0.0, "amount": 0.0})
        r["taxable"] += line_taxable
        r["amount"] += line_tax
    total_tax = round(total_tax, 2)

    # Same-state order -> CGST + SGST split; different state -> IGST.
    shop_state = (settings.shop.state or "").strip().lower()
    dest_state = (getattr(address, "state", "") or "").strip().lower()
    interstate = bool(shop_state and dest_state and shop_state != dest_state)
    gst = {
        "total": total_tax,
        "interstate": interstate,
        "cgst": 0.0 if interstate else round(total_tax / 2, 2),
        "sgst": 0.0 if interstate else round(total_tax / 2, 2),
        "igst": total_tax if interstate else 0.0,
        "rates": [
            {"rate": v["rate"], "taxable": round(v["taxable"], 2), "amount": round(v["amount"], 2)}
            for v in sorted(rate_map.values(), key=lambda x: x["rate"])
        ],
    }

    total = round((subtotal - discount) + deliv["fee"] + total_tax, 2)

    bill = {
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "delivery": deliv["fee"],
        "delivery_free": deliv["free"],
        "distance_km": deliv["distance_km"],
        "deliverable": deliv["deliverable"],
        "tax": total_tax,
        "gst": gst,
        "total": total,
        "currency": settings.currency,          # display symbol (₹)
        "currency_code": settings.currency_code,  # ISO code for Razorpay (INR)
        "coupon_applied": discount > 0,
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
                    sizes = c.get("sizes") or []
                    if sizes and it.get("size"):
                        for ss in sizes:
                            if ss.get("size") == it["size"]:
                                ss["stock"] = max(0, int(ss.get("stock", 0)) - it["qty"])
                    else:
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
        rp = razorpay_service.create_order(
            round(bill["total"] * 100), receipt=our_id, currency=bill.get("currency_code", "INR")
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    await db.orders.update_one({"_id": res.inserted_id}, {"$set": {"razorpay_order_id": rp["id"]}})

    return {
        "order_id": our_id,
        "payment_method": "online",
        "razorpay_order_id": rp["id"],
        "amount": round(bill["total"] * 100),
        "currency": bill.get("currency_code", "INR"),  # ISO code for Razorpay
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
