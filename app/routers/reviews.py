from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.db.mongodb import get_db
from app.deps import get_current_user
from app.models.common import serialize, to_object_id

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewIn(BaseModel):
    product_id: str
    rating: float = Field(ge=1, le=5)
    text: str = ""


@router.get("")
async def list_reviews(product_id: str):
    db = get_db()
    docs = (
        await db.reviews.find({"product_id": product_id})
        .sort("created_at", -1)
        .to_list(length=100)
    )
    return [serialize(d) for d in docs]


@router.post("")
async def add_review(body: ReviewIn, user: dict = Depends(get_current_user)):
    db = get_db()
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
