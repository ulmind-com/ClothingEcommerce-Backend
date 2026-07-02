from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.mongodb import get_db
from app.deps import get_current_user
from app.models.common import serialize, to_object_id

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewIn(BaseModel):
    product_id: str
    rating: float = Field(ge=1, le=5)
    text: str = ""


async def _has_delivered(db, user_id: str, product_id: str) -> bool:
    order = await db.orders.find_one(
        {"user_id": user_id, "status": "delivered", "items.product_id": product_id}
    )
    return order is not None


@router.get("")
async def list_reviews(product_id: str):
    db = get_db()
    docs = (
        await db.reviews.find({"product_id": product_id})
        .sort("created_at", -1)
        .to_list(length=100)
    )
    return [serialize(d) for d in docs]


@router.get("/can-review")
async def can_review(product_id: str, user: dict = Depends(get_current_user)):
    db = get_db()
    already = await db.reviews.find_one({"product_id": product_id, "user_id": user["id"]})
    delivered = await _has_delivered(db, user["id"], product_id)
    return {"can": delivered and not already, "delivered": delivered, "already": bool(already)}


@router.post("")
async def add_review(body: ReviewIn, user: dict = Depends(get_current_user)):
    db = get_db()
    if not await _has_delivered(db, user["id"], body.product_id):
        raise HTTPException(status_code=403, detail="You can review only after your order is delivered")
    if await db.reviews.find_one({"product_id": body.product_id, "user_id": user["id"]}):
        raise HTTPException(status_code=409, detail="You already reviewed this product")
    doc = {
        "product_id": body.product_id,
        "user_id": user["id"],
        "user_name": user.get("name", "User"),
        "rating": body.rating,
        "text": body.text,
        "created_at": datetime.now(timezone.utc),
    }
    await db.reviews.insert_one(doc)

    # Recompute aggregate rating for the product.
    reviews = await db.reviews.find({"product_id": body.product_id}).to_list(length=1000)
    count = len(reviews)
    avg = round(sum(r["rating"] for r in reviews) / count, 1) if count else 0
    await db.products.update_one(
        {"_id": to_object_id(body.product_id)},
        {"$set": {"rating": avg, "review_count": count}},
    )
    return {"ok": True, "rating": avg, "review_count": count}
