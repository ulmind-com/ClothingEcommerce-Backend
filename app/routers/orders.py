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
    first_order_discount,
    get_settings,
    haversine_km,
    resolve_price,
)

router = APIRouter(prefix="/orders", tags=["orders"])

STAGES = ["placed", "confirmed", "shipped", "out_for_delivery", "delivered"]

# An order counts as "made" once it reaches at least confirmed (paid online or
# COD). A customer with none of these is on their first order.
FIRST_ORDER_DONE = ["confirmed", "shipped", "out_for_delivery", "delivered"]


async def _is_first_order(db, user_id: str) -> bool:
    if not user_id:
        return False
    n = await db.orders.count_documents({"user_id": user_id, "status": {"$in": FIRST_ORDER_DONE}})
    return n == 0

STATUS_MSG = {
    "confirmed": "Your order is confirmed and being packed 📦",
    "shipped": "Your order has been shipped 🚚",
    "out_for_delivery": "Your order is out for delivery 🛵",
    "delivered": "Your order has been delivered ✅",
    "cancelled": "Your order was cancelled",
}


async def _build_bill(db, items_in, address, coupon, user_id=None):
    settings = await get_settings(db)
    order_items = []
    lines = []  # (line_total, {cgst, sgst, igst})
    subtotal = 0.0
    for it in items_in:
        prod = await db.products.find_one({"_id": to_object_id(it.product_id)})
        if not prod or not prod.get("is_active", True):
            raise HTTPException(status_code=400, detail="Product unavailable")
        # Price for the exact colour + size the customer chose (variant-aware).
        unit = resolve_price(prod, it.color, it.size)["final_price"]
        line_total = unit * it.qty
        subtotal += line_total
        comp = {
            "cgst": float(prod.get("cgst") or 0),
            "sgst": float(prod.get("sgst") or 0),
            "igst": float(prod.get("igst") or 0),
        }
        lines.append((line_total, comp))
        order_items.append(
            {
                "product_id": it.product_id,
                "title": prod["title"],
                "price": unit,
                "qty": it.qty,
                "color": it.color,
                "size": it.size,
                "cgst": comp["cgst"],
                "sgst": comp["sgst"],
                "igst": comp["igst"],
                "image": (prod.get("images") or [None])[0],
            }
        )

    coupon_doc = await get_coupon(db, coupon)
    discount = coupon_discount(coupon_doc, subtotal) if coupon_doc else 0.0

    # First-order offer (isolated add-on, separate from coupons). Applies only
    # when the admin enabled it AND this is the customer's first order. Off by
    # default -> first_order_disc stays 0 and nothing below changes.
    fo_cfg = settings.first_order.model_dump()
    first_eligible = bool(fo_cfg.get("enabled")) and await _is_first_order(db, user_id)
    first_order_disc = first_order_discount(fo_cfg, subtotal) if first_eligible else 0.0

    # Total order-level discount drives delivery threshold, tax proration and
    # the grand total. Never let combined discounts exceed the subtotal.
    order_discount = discount + first_order_disc
    if order_discount > subtotal:
        order_discount = round(subtotal, 2)
        first_order_disc = max(0.0, round(order_discount - discount, 2))

    # distance-based delivery
    distance = None
    if settings.shop.lat is not None and address.lat is not None:
        distance = haversine_km(settings.shop.lat, settings.shop.lng, address.lat, address.lng)
    deliv = compute_delivery(settings, subtotal - order_discount, distance)

    # Same-state order -> CGST + SGST from the product; different state -> IGST.
    shop_state = (settings.shop.state or "").strip().lower()
    dest_state = (getattr(address, "state", "") or "").strip().lower()
    interstate = bool(shop_state and dest_state and shop_state != dest_state)

    total_cgst = total_sgst = total_igst = 0.0
    tax_items = []
    for oi, (line_total, comp) in zip(order_items, lines):
        share = (line_total / subtotal * order_discount) if subtotal else 0.0
        taxable = max(0.0, line_total - share)
        if interstate:
            i_cgst = i_sgst = 0.0
            i_igst = round(taxable * comp["igst"] / 100, 2)
        else:
            i_cgst = round(taxable * comp["cgst"] / 100, 2)
            i_sgst = round(taxable * comp["sgst"] / 100, 2)
            i_igst = 0.0
        total_cgst += i_cgst
        total_sgst += i_sgst
        total_igst += i_igst
        rate = comp["igst"] if interstate else (comp["cgst"] + comp["sgst"])
        tax_items.append({
            "title": oi["title"], "qty": oi["qty"], "rate": rate,
            "cgst": i_cgst, "sgst": i_sgst, "igst": i_igst,
            "taxable": round(taxable, 2), "tax": round(i_cgst + i_sgst + i_igst, 2),
        })
    total_cgst = round(total_cgst, 2)
    total_sgst = round(total_sgst, 2)
    total_igst = round(total_igst, 2)
    total_tax = round(total_cgst + total_sgst + total_igst, 2)

    gst = {
        "total": total_tax,
        "interstate": interstate,
        "cgst": total_cgst,
        "sgst": total_sgst,
        "igst": total_igst,
        "items": tax_items,  # per-product GST breakdown
    }

    total = round((subtotal - order_discount) + deliv["fee"] + total_tax, 2)

    bill = {
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "first_order_discount": round(first_order_disc, 2),
        "first_order_applied": first_order_disc > 0,
        "total_discount": round(order_discount, 2),
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
    _, bill = await _build_bill(db, body.items, body.address, body.coupon_code, user["id"])
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
    order_items, bill = await _build_bill(db, body.items, body.address, body.coupon_code, user["id"])
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
