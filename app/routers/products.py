from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.mongodb import get_db
from app.deps import require_admin
from app.models.common import serialize, to_object_id
from app.models.product import ProductCreate, ProductUpdate
from app.services.pricing import product_final_price

router = APIRouter(prefix="/products", tags=["products"])


def _total_stock(doc: dict) -> int:
    colors = [c for c in (doc.get("colors") or []) if isinstance(c, dict)]
    if colors:
        return sum(int(c.get("stock", 0)) for c in colors)
    return int(doc.get("stock", 0))


def _decorate(doc: dict) -> dict:
    d = serialize(doc)
    # drop any legacy string colours so the client always gets variant objects
    d["colors"] = [c for c in (d.get("colors") or []) if isinstance(c, dict)]
    d.update(product_final_price(d))
    stock = _total_stock(d)
    d["total_stock"] = stock
    d["in_stock"] = stock > 0
    d["low_stock"] = 0 < stock <= (d.get("low_stock_threshold") or 5)
    return d


async def _category_ids_with_children(db, category_id: str) -> list[str]:
    ids = [category_id]
    children = await db.categories.find({"parent_id": category_id}).to_list(length=200)
    ids += [str(c["_id"]) for c in children]
    return ids


@router.get("")
async def list_products(
    category_id: str | None = None,
    q: str | None = Query(default=None),
    limit: int = Query(default=20, le=100),
    skip: int = 0,
    admin: bool = False,
):
    db = get_db()
    query: dict = {}
    if not admin:
        query["is_active"] = True
    if category_id:
        query["category_id"] = {"$in": await _category_ids_with_children(db, category_id)}
    if q:
        query["$text"] = {"$search": q}
    cursor = db.products.find(query).skip(skip).limit(limit).sort("created_at", -1)
    docs = await cursor.to_list(length=limit)
    return [_decorate(d) for d in docs]


@router.get("/{product_id}")
async def get_product(product_id: str):
    db = get_db()
    doc = await db.products.find_one({"_id": to_object_id(product_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")
    return _decorate(doc)


@router.post("", dependencies=[Depends(require_admin)])
async def create_product(body: ProductCreate):
    db = get_db()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    res = await db.products.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _decorate(doc)


@router.patch("/{product_id}", dependencies=[Depends(require_admin)])
async def update_product(product_id: str, body: ProductUpdate):
    db = get_db()
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    res = await db.products.find_one_and_update(
        {"_id": to_object_id(product_id)}, {"$set": update}, return_document=True
    )
    if not res:
        raise HTTPException(status_code=404, detail="Product not found")
    return _decorate(res)


@router.delete("/{product_id}", dependencies=[Depends(require_admin)])
async def delete_product(product_id: str):
    db = get_db()
    res = await db.products.delete_one({"_id": to_object_id(product_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": True}


@router.get("/admin/low-stock", dependencies=[Depends(require_admin)])
async def low_stock():
    """Products at or below their low-stock threshold."""
    db = get_db()
    docs = await db.products.find().to_list(length=500)
    out = [_decorate(d) for d in docs]
    return [p for p in out if p["total_stock"] <= (p.get("low_stock_threshold") or 5)]
