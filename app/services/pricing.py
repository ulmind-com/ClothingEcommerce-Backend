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


def _apply(mrp: float, price: float, disc: float, on: str) -> tuple:
    """(final, struck, off_pct) after applying an admin discount."""
    if disc and disc > 0:
        base = mrp if on == "mrp" and mrp else price
        final = round(base * (1 - disc / 100), 2)
    else:
        final = price
    struck = mrp if mrp and mrp > final else None
    off = round((struck - final) / struck * 100) if struck else 0
    return final, struck, off


def product_final_price(product: dict) -> dict:
    """Base display pricing (product-level mrp/price + admin discount)."""
    final, struck, off = _apply(
        product.get("mrp") or 0, product.get("price") or 0,
        product.get("discount_pct") or 0, product.get("discount_on") or "price",
    )
    return {"final_price": final, "struck_price": struck, "off_pct": off}


def _variant_base(product: dict, color=None, size=None) -> tuple:
    """Resolve (price, mrp) for a colour+size with fallbacks: size -> colour -> base."""
    price = product.get("price") or 0
    mrp = product.get("mrp") or 0
    for c in (product.get("colors") or []):
        if isinstance(c, dict) and c.get("name") == color:
            if c.get("price") is not None:
                price = c["price"]
            if c.get("mrp") is not None:
                mrp = c["mrp"]
            for ss in (c.get("sizes") or []):
                if ss.get("size") == size:
                    if ss.get("price") is not None:
                        price = ss["price"]
                    if ss.get("mrp") is not None:
                        mrp = ss["mrp"]
                    break
            break
    return price, mrp


def resolve_price(product: dict, color=None, size=None) -> dict:
    """Display pricing for a specific colour + size selection."""
    price, mrp = _variant_base(product, color, size)
    final, struck, off = _apply(mrp, price, product.get("discount_pct") or 0, product.get("discount_on") or "price")
    return {"final_price": final, "struck_price": struck, "off_pct": off}


def _combos(product: dict) -> list[tuple]:
    """(final, struck, off) for every colour+size combination."""
    disc = product.get("discount_pct") or 0
    on = product.get("discount_on") or "price"
    out = []
    colors = [c for c in (product.get("colors") or []) if isinstance(c, dict)]
    if colors:
        for c in colors:
            sizes = c.get("sizes") or []
            targets = [s.get("size") for s in sizes] if sizes else [None]
            for sz in targets:
                p, m = _variant_base(product, c.get("name"), sz)
                out.append(_apply(m, p, disc, on))
    else:
        out.append(_apply(product.get("mrp") or 0, product.get("price") or 0, disc, on))
    return out or [_apply(product.get("mrp") or 0, product.get("price") or 0, disc, on)]


def price_span(product: dict) -> dict:
    """Card pricing: cheapest combo as the representative, plus the price range."""
    combos = _combos(product)
    finals = [c[0] for c in combos]
    lo, hi = min(finals), max(finals)
    rep = min(combos, key=lambda c: c[0])  # cheapest combo drives the shown price
    return {
        "final_price": rep[0],
        "struck_price": rep[1],
        "off_pct": rep[2],
        "price_from": lo,
        "price_to": hi,
        "price_varies": round(lo, 2) != round(hi, 2),
    }


def variant_stock(product: dict, color=None, size=None) -> int:
    for c in (product.get("colors") or []):
        if isinstance(c, dict) and c.get("name") == color:
            sizes = c.get("sizes") or []
            if sizes:
                for ss in sizes:
                    if ss.get("size") == size:
                        return int(ss.get("stock", 0))
                return 0
            return int(c.get("stock", 0))
    return int(product.get("stock", 0))


def total_stock(product: dict) -> int:
    colors = [c for c in (product.get("colors") or []) if isinstance(c, dict)]
    if colors:
        s = 0
        for c in colors:
            sizes = c.get("sizes") or []
            s += sum(int(x.get("stock", 0)) for x in sizes) if sizes else int(c.get("stock", 0))
        return s
    return int(product.get("stock", 0))


def compute_delivery(settings: Settings, subtotal: float, distance_km: float | None) -> dict:
    """Return {fee, distance_km, deliverable, free}.

    Precedence matters: the max serviceable distance is checked BEFORE any
    free-delivery rule, so an address outside the service area is never marked
    deliverable/free just because the order crossed the free-above threshold.
    Distance is always rounded to one decimal for display.
    """
    d = settings.delivery
    dist = round(distance_km, 1) if distance_km is not None else None

    # 1. Outside the serviceable area -> not deliverable (0 = no limit).
    if dist is not None and d.max_service_km and dist > d.max_service_km:
        return {"fee": 0.0, "distance_km": dist, "deliverable": False, "free": False}

    # 2. Free delivery above an order-value threshold.
    if d.free_above and subtotal >= d.free_above:
        return {"fee": 0.0, "distance_km": dist, "deliverable": True, "free": True}

    # 3. No coordinates -> base fee fallback.
    if dist is None:
        return {"fee": round(d.base_fee, 2), "distance_km": None, "deliverable": True, "free": d.base_fee == 0}

    # 4. Within the free radius.
    if dist <= d.free_radius_km:
        return {"fee": 0.0, "distance_km": dist, "deliverable": True, "free": True}

    # 5. Slab-based pricing (overrides per-km when configured).
    if d.slabs:
        for slab in sorted(d.slabs, key=lambda s: s.up_to_km):
            if dist <= slab.up_to_km:
                return {"fee": round(slab.fee, 2), "distance_km": dist, "deliverable": True, "free": slab.fee == 0}
        last = max(d.slabs, key=lambda s: s.up_to_km)
        return {"fee": round(last.fee, 2), "distance_km": dist, "deliverable": True, "free": last.fee == 0}

    # 6. Beyond the free radius -> base fee + per-km on the FULL distance
    #    (the free radius only decides free-vs-paid, it is NOT subtracted).
    fee = d.base_fee + dist * d.per_km_rate
    return {"fee": round(fee, 2), "distance_km": dist, "deliverable": True, "free": fee == 0}
