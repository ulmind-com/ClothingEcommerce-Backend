from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.mongodb import get_db
from app.deps import require_admin
from app.models.common import serialize, to_object_id
from app.models.product import ProductCreate, ProductUpdate

router = APIRouter(prefix="/products", tags=["products"])


def _with_discount(doc: dict) -> dict:
    price = doc.get("price") or 0
    mrp = doc.get("mrp")
    doc["discount_pct"] = (
        round((mrp - price) / mrp * 100) if mrp and mrp > price else 0
    )
    return doc


@router.get("")
async def list_products(
    category_id: str | None = None,
    q: str | None = Query(default=None, description="text search"),
    limit: int = Query(default=20, le=100),
    skip: int = 0,
):
    db = get_db()
    query: dict = {"is_active": True}
    if category_id:
        query["category_id"] = category_id
    if q:
        query["$text"] = {"$search": q}
    cursor = db.products.find(query).skip(skip).limit(limit).sort("created_at", -1)
    docs = await cursor.to_list(length=limit)
    return [_with_discount(serialize(d)) for d in docs]


@router.get("/{product_id}")
async def get_product(product_id: str):
    db = get_db()
    doc = await db.products.find_one({"_id": to_object_id(product_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")
    return _with_discount(serialize(doc))


@router.post("", dependencies=[Depends(require_admin)])
async def create_product(body: ProductCreate):
    db = get_db()
    doc = body.model_dump()
    doc.update(
        {
            "rating": 0.0,
            "review_count": 0,
            "sold_count": 0,
            "created_at": datetime.now(timezone.utc),
        }
    )
    res = await db.products.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _with_discount(serialize(doc))


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
    return _with_discount(serialize(res))


@router.delete("/{product_id}", dependencies=[Depends(require_admin)])
async def delete_product(product_id: str):
    db = get_db()
    res = await db.products.delete_one({"_id": to_object_id(product_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": True}
