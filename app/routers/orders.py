from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from app.core.config import settings as app_settings
from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id
from app.models.order import OrderCreate, OrderVerify
from app.routers.coupons import get_coupon
from app.services import notifications, razorpay_service
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

    # Coupon lookup is user-aware so a first-order-only coupon can't be used by
    # a returning customer (see get_coupon).
    coupon_doc = await get_coupon(db, coupon, user_id)
    discount = coupon_discount(coupon_doc, subtotal) if coupon_doc else 0.0

    # distance-based delivery
    distance = None
    if settings.shop.lat is not None and address.lat is not None:
        distance = haversine_km(settings.shop.lat, settings.shop.lng, address.lat, address.lng)
    deliv = compute_delivery(settings, subtotal - discount, distance)

    # Same-state order -> CGST + SGST from the product; different state -> IGST.
    shop_state = (settings.shop.state or "").strip().lower()
    dest_state = (getattr(address, "state", "") or "").strip().lower()
    interstate = bool(shop_state and dest_state and shop_state != dest_state)

    total_cgst = total_sgst = total_igst = 0.0
    tax_items = []
    for oi, (line_total, comp) in zip(order_items, lines):
        share = (line_total / subtotal * discount) if subtotal else 0.0
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


async def _restore_stock(db, order_items):
    """Put stock back (inverse of _decrement_stock) — used on cancellation."""
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
                                ss["stock"] = int(ss.get("stock", 0)) + it["qty"]
                    else:
                        c["stock"] = int(c.get("stock", 0)) + it["qty"]
            await db.products.update_one({"_id": prod["_id"]}, {"$set": {"colors": colors}})
        else:
            await db.products.update_one({"_id": prod["_id"]}, {"$inc": {"stock": it["qty"]}})


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
        await notifications.notify_users(db, [user["id"]], "Order placed 🎉",
                                         "Your COD order is confirmed.",
                                         {"type": "order", "order_id": our_id}, kind="order")
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
    await notifications.notify_users(db, [user["id"]], "Payment successful 🎉",
                                     "Your order is confirmed.",
                                     {"type": "order", "order_id": body.order_id}, kind="order")
    return {"status": "confirmed", "order_id": body.order_id}


# Orders can be self-cancelled only before they're handed to shipping.
CANCELLABLE = ["placed", "confirmed"]


@router.post("/{order_id}/cancel")
async def cancel_order(order_id: str, user: dict = Depends(get_current_user)):
    """Customer cancels their own order — allowed only within the admin's
    cancellation window and before the order ships. Restores stock and, for a
    paid online order, initiates a Razorpay refund."""
    db = get_db()
    order = await db.orders.find_one({"_id": to_object_id(order_id)})
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] not in CANCELLABLE:
        raise HTTPException(status_code=400, detail="This order can no longer be cancelled")

    settings = await get_settings(db)
    window = settings.cancel_window_hours or 0
    if window <= 0:
        raise HTTPException(status_code=400, detail="Order cancellation isn't available")

    created = order.get("created_at")
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    elapsed_h = (now - created).total_seconds() / 3600 if created else 0
    if elapsed_h > window:
        raise HTTPException(
            status_code=400,
            detail=f"The cancellation window ({_fmt_window(window)}) has passed",
        )

    update: dict = {"status": "cancelled", "cancelled_at": now}

    # Refund a captured online payment (best-effort — cancel regardless).
    refunded = False
    if order.get("payment_method") == "online" and order.get("razorpay_payment_id"):
        try:
            r = razorpay_service.refund(order["razorpay_payment_id"], int(round(order["amount"] * 100)))
            update["refund_id"] = r.get("id")
            update["refund_status"] = "initiated"
            refunded = True
        except Exception as e:  # pragma: no cover
            print(f"[orders] refund failed: {e}")
            update["refund_status"] = "failed"

    await db.orders.update_one({"_id": order["_id"]}, {"$set": update})

    # Stock was only decremented once the order was confirmed (COD or paid).
    if order["status"] == "confirmed":
        await _restore_stock(db, order["items"])

    msg = "Your order has been cancelled."
    if refunded:
        msg += " Your refund has been initiated."
    await notifications.notify_users(
        db, [user["id"]], "Order cancelled", msg, {"type": "order", "order_id": order_id}, kind="order"
    )
    return {"status": "cancelled", "refund": refunded}


def _fmt_window(hours: float) -> str:
    if hours >= 24 and hours % 24 == 0:
        d = int(hours // 24)
        return f"{d} day" + ("s" if d != 1 else "")
    h = int(hours) if float(hours).is_integer() else hours
    return f"{h} hour" + ("s" if h != 1 else "")


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


@router.get("/admin/refunds", dependencies=[Depends(require_admin)])
async def admin_refunds():
    """All cancelled orders with their refund details (newest cancellation first)."""
    db = get_db()
    docs = await db.orders.find({"status": "cancelled"}).sort("cancelled_at", -1).to_list(length=500)
    return [serialize(d) for d in docs]


@router.post("/{order_id}/refund", dependencies=[Depends(require_admin)])
async def admin_refund(order_id: str):
    """Manually initiate / retry a Razorpay refund for a paid online order."""
    db = get_db()
    order = await db.orders.find_one({"_id": to_object_id(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("payment_method") != "online" or not order.get("razorpay_payment_id"):
        raise HTTPException(status_code=400, detail="This order has no online payment to refund")
    if order.get("refund_status") == "initiated":
        raise HTTPException(status_code=400, detail="A refund is already initiated for this order")
    try:
        r = razorpay_service.refund(order["razorpay_payment_id"], int(round(order["amount"] * 100)))
    except Exception as e:
        await db.orders.update_one({"_id": order["_id"]}, {"$set": {"refund_status": "failed"}})
        raise HTTPException(status_code=502, detail=f"Refund failed: {e}")
    await db.orders.update_one(
        {"_id": order["_id"]}, {"$set": {"refund_id": r.get("id"), "refund_status": "initiated"}}
    )
    return {"refund_status": "initiated", "refund_id": r.get("id")}


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

    await notifications.notify_users(db, [res["user_id"]], "Order update",
                                     STATUS_MSG.get(status, f"Status: {status}"),
                                     {"type": "order", "order_id": order_id}, kind="order")
    return serialize(res)
