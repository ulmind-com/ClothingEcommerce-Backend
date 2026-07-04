"""
Product recommendations (Flipkart-style).

A pragmatic hybrid recommender that needs no ML infra:
- Content-based similarity (category, brand, price band, sizes/colours).
- Item-to-item collaborative signal via order co-occurrence
  ("customers who bought X also bought Y").
- Popularity prior (sold count, rating) for cold-start / tie-breaking.

Endpoints:
- GET /recommendations/home              → personalised for the user, else popular.
- GET /recommendations/similar/{id}      → similar products (product page).
- GET /recommendations/cart?product_ids= → "You may like" for the current cart.
"""

from fastapi import APIRouter, Depends, Query

from app.db.mongodb import get_db
from app.deps import get_optional_user
from app.models.common import to_object_id
from app.routers.products import _decorate

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


def _pop_score(p: dict) -> float:
    """Popularity prior from sales, rating and review volume."""
    return (
        (p.get("sold_count") or 0) * 1.0
        + (p.get("rating") or 0) * 8.0
        + (p.get("review_count") or 0) * 0.5
    )


def _content_sim(a: dict, b: dict) -> float:
    """Similarity between two products (higher = more alike)."""
    score = 0.0
    if a.get("category_id") and a.get("category_id") == b.get("category_id"):
        score += 5.0
    if a.get("brand") and a.get("brand") == b.get("brand"):
        score += 3.0
    pa, pb = a.get("final_price") or 0, b.get("final_price") or 0
    if pa and pb:
        score += (min(pa, pb) / max(pa, pb)) * 2.0  # price-band closeness (0..2)
    if set(a.get("sizes") or []) & set(b.get("sizes") or []):
        score += 1.0
    ca = {c.get("name") for c in (a.get("colors") or []) if isinstance(c, dict)}
    cb = {c.get("name") for c in (b.get("colors") or []) if isinstance(c, dict)}
    if ca & cb:
        score += 0.5
    return score


def _avail(p: dict) -> float:
    """Small penalty so out-of-stock items sink below buyable ones."""
    return 0.0 if p.get("in_stock") else -1000.0


async def _active_products(db, limit: int = 800) -> list[dict]:
    docs = await db.products.find({"is_active": True}).limit(limit).to_list(length=limit)
    return [_decorate(d) for d in docs]


def _popular(products: list[dict], limit: int, exclude: set[str]) -> list[dict]:
    ranked = sorted(
        (p for p in products if p["id"] not in exclude),
        key=lambda p: _avail(p) + _pop_score(p),
        reverse=True,
    )
    return ranked[:limit]


def _rank(products: list[dict], seeds: list[dict], exclude: set[str],
          copurchase: dict[str, int] | None, limit: int) -> list[dict]:
    """Score candidates against seed products + optional co-purchase counts."""
    copurchase = copurchase or {}
    scored: list[tuple[float, dict]] = []
    for p in products:
        if p["id"] in exclude:
            continue
        content = max((_content_sim(p, s) for s in seeds), default=0.0)
        co = copurchase.get(p["id"], 0) * 4.0
        s = co + content + _pop_score(p) * 0.02 + _avail(p)
        if s > -900:  # keep out-of-stock only as last resort
            scored.append((s, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = [p for s, p in scored if s > 0][:limit]
    if len(out) < limit:  # pad with popular so a rail is never empty
        have = {p["id"] for p in out} | exclude
        out += _popular(products, limit - len(out), have)
    return out[:limit]


@router.get("/home")
async def home_recommendations(
    limit: int = Query(12, ge=1, le=30),
    user: dict | None = Depends(get_optional_user),
):
    db = get_db()
    products = await _active_products(db)
    by_id = {p["id"]: p for p in products}

    seed_ids: set[str] = set()
    if user:
        orders = await db.orders.find({"user_id": user["id"]}).to_list(length=500)
        for o in orders:
            for it in o.get("items", []):
                if it.get("product_id"):
                    seed_ids.add(it["product_id"])
        wl = await db.wishlists.find_one({"user_id": user["id"]})
        for pid in (wl or {}).get("product_ids", []):
            seed_ids.add(pid)

    seeds = [by_id[i] for i in seed_ids if i in by_id]
    if seeds:
        # Don't re-recommend items already bought/wishlisted.
        return _rank(products, seeds, exclude=seed_ids, copurchase=None, limit=limit)
    # Cold start → most popular.
    return _popular(products, limit, exclude=set())


@router.get("/similar/{product_id}")
async def similar_products(product_id: str, limit: int = Query(12, ge=1, le=30)):
    db = get_db()
    seed_doc = await db.products.find_one({"_id": to_object_id(product_id)})
    products = await _active_products(db)
    if not seed_doc:
        return _popular(products, limit, exclude={product_id})
    seed = _decorate(seed_doc)
    return _rank(products, [seed], exclude={product_id}, copurchase=None, limit=limit)


@router.get("/cart")
async def cart_recommendations(
    product_ids: str = Query("", description="comma-separated product ids in the cart"),
    limit: int = Query(10, ge=1, le=30),
):
    db = get_db()
    ids = [x.strip() for x in product_ids.split(",") if x.strip()]
    products = await _active_products(db)
    by_id = {p["id"]: p for p in products}
    seeds = [by_id[i] for i in ids if i in by_id]
    exclude = set(ids)

    if not seeds:
        return _popular(products, limit, exclude=exclude)

    # "Customers who bought these also bought…" via order co-occurrence.
    copurchase: dict[str, int] = {}
    orders = await db.orders.find({"items.product_id": {"$in": ids}}).to_list(length=1000)
    for o in orders:
        prod_ids = {it.get("product_id") for it in o.get("items", [])}
        if not (prod_ids & exclude):
            continue
        for pid in prod_ids:
            if pid and pid not in exclude:
                copurchase[pid] = copurchase.get(pid, 0) + 1

    return _rank(products, seeds, exclude=exclude, copurchase=copurchase, limit=limit)
