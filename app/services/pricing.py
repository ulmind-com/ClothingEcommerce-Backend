import math

from app.models.settings import Settings

_KEY = {"_id": "config"}


async def get_settings(db) -> Settings:
    doc = await db.settings.find_one(_KEY)
    if not doc:
        return Settings()
    doc.pop("_id", None)
    return Settings(**doc)


async def save_settings(db, settings: Settings) -> Settings:
    await db.settings.update_one(_KEY, {"$set": settings.model_dump()}, upsert=True)
    return settings


def coupon_discount(coupon: dict, subtotal: float) -> float:
    """Compute the discount a coupon gives on a subtotal (respecting min/cap)."""
    if not coupon:
        return 0.0
    if subtotal < coupon.get("min_order", 0):
        return 0.0
    if coupon.get("type") == "flat":
        return round(min(coupon.get("value", 0), subtotal), 2)
    # percent
    disc = subtotal * coupon.get("value", 0) / 100
    cap = coupon.get("max_discount", 0)
    if cap and cap > 0:
        disc = min(disc, cap)
    return round(disc, 2)


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def product_final_price(product: dict) -> dict:
    """Compute the display pricing for a product.

    Fields on product:
      mrp   -> actual price (struck through)
      price -> needed / selling price
      discount_pct -> admin extra discount (%)
      discount_on -> 'mrp' | 'price' (which base the discount applies to)
    """
    mrp = product.get("mrp") or 0
    price = product.get("price") or 0
    disc = product.get("discount_pct") or 0
    on = product.get("discount_on") or "price"

    if disc and disc > 0:
        base = mrp if on == "mrp" and mrp else price
        final = round(base * (1 - disc / 100), 2)
    else:
        final = price

    struck = mrp if mrp and mrp > final else None
    off_pct = round((struck - final) / struck * 100) if struck else 0
    return {"final_price": final, "struck_price": struck, "off_pct": off_pct}


def compute_delivery(settings: Settings, subtotal: float, distance_km: float | None) -> dict:
    """Return {fee, distance_km, deliverable, reason}."""
    d = settings.delivery
    if d.free_above and subtotal >= d.free_above:
        return {"fee": 0.0, "distance_km": distance_km, "deliverable": True, "free": True}

    if distance_km is None:
        # no coordinates -> fall back to base fee beyond free radius
        return {"fee": round(d.base_fee, 2), "distance_km": None, "deliverable": True, "free": d.base_fee == 0}

    if distance_km > d.max_service_km:
        return {"fee": 0.0, "distance_km": round(distance_km, 1), "deliverable": False, "free": False}

    if distance_km <= d.free_radius_km:
        return {"fee": 0.0, "distance_km": round(distance_km, 1), "deliverable": True, "free": True}

    # slab based if configured
    if d.slabs:
        for slab in sorted(d.slabs, key=lambda s: s.up_to_km):
            if distance_km <= slab.up_to_km:
                return {"fee": round(slab.fee, 2), "distance_km": round(distance_km, 1), "deliverable": True, "free": False}
        # beyond last slab -> use last slab fee
        last = max(d.slabs, key=lambda s: s.up_to_km)
        return {"fee": round(last.fee, 2), "distance_km": round(distance_km, 1), "deliverable": True, "free": False}

    # per-km beyond free radius
    fee = d.base_fee + (distance_km - d.free_radius_km) * d.per_km_rate
    return {"fee": round(fee, 2), "distance_km": round(distance_km, 1), "deliverable": True, "free": False}
