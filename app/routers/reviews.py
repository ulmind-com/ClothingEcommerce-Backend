from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id

router = APIRouter(prefix="/reviews", tags=["reviews"])

# What a customer can say they liked about the product (left demo screen).
ALLOWED_TAGS = ["Quality", "Material", "Fit", "Design"]


class ReviewIn(BaseModel):
    product_id: str
    rating: float = Field(ge=1, le=5)
    title: str = ""
    text: str = ""
    photos: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class VoteIn(BaseModel):
    helpful: bool


async def _has_delivered(db, user_id: str, product_id: str) -> bool:
    order = await db.orders.find_one(
        {"user_id": user_id, "status": "delivered", "items.product_id": product_id}
    )
    return order is not None


async def _recompute(db, product_id: str) -> dict:
    reviews = await db.reviews.find({"product_id": product_id}).to_list(length=5000)
    count = len(reviews)
    avg = round(sum(r["rating"] for r in reviews) / count, 1) if count else 0
    await db.products.update_one(
        {"_id": to_object_id(product_id)},
        {"$set": {"rating": avg, "review_count": count}},
    )
    return {"rating": avg, "review_count": count}


def _public(doc: dict) -> dict:
    """Serialize a review for public consumption (hide voter identities)."""
    out = serialize(doc)
    out.pop("helpful_by", None)
    out.pop("unhelpful_by", None)
    out.setdefault("title", "")
    out.setdefault("photos", [])
    out.setdefault("tags", [])
    out.setdefault("helpful_count", 0)
    out.setdefault("unhelpful_count", 0)
    return out


@router.get("")
async def list_reviews(product_id: str, rating: int | None = None):
    db = get_db()
    query: dict = {"product_id": product_id}
    if rating in (1, 2, 3, 4, 5):
        # Ratings are stored as whole stars; bucket defensively.
        query["rating"] = {"$gte": rating, "$lt": rating + 1}
    docs = (
        await db.reviews.find(query)
        .sort("created_at", -1)
        .to_list(length=200)
    )
    return [_public(d) for d in docs]


@router.get("/summary")
async def review_summary(product_id: str):
    db = get_db()
    docs = await db.reviews.find({"product_id": product_id}).to_list(length=5000)
    count = len(docs)
    avg = round(sum(d["rating"] for d in docs) / count, 1) if count else 0
    breakdown = {str(s): 0 for s in range(1, 6)}
    for d in docs:
        star = max(1, min(5, int(round(d["rating"]))))
        breakdown[str(star)] += 1
    return {"count": count, "average": avg, "breakdown": breakdown}


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
    tags = [t for t in body.tags if t in ALLOWED_TAGS]
    doc = {
        "product_id": body.product_id,
        "user_id": user["id"],
        "user_name": user.get("name", "User"),
        "rating": body.rating,
        "title": body.title.strip()[:120],
        "text": body.text.strip(),
        "photos": body.photos[:6],
        "tags": tags,
        "helpful_by": [],
        "unhelpful_by": [],
        "helpful_count": 0,
        "unhelpful_count": 0,
        "created_at": datetime.now(timezone.utc),
    }
    await db.reviews.insert_one(doc)
    agg = await _recompute(db, body.product_id)
    return {"ok": True, **agg}


@router.post("/{review_id}/vote")
async def vote_review(review_id: str, body: VoteIn, user: dict = Depends(get_current_user)):
    db = get_db()
    oid = to_object_id(review_id)
    doc = await db.reviews.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Review not found")
    uid = user["id"]
    helpful_by = set(doc.get("helpful_by", []))
    unhelpful_by = set(doc.get("unhelpful_by", []))
    # A user counts once; a new vote replaces their previous one.
    helpful_by.discard(uid)
    unhelpful_by.discard(uid)
    if body.helpful:
        helpful_by.add(uid)
    else:
        unhelpful_by.add(uid)
    await db.reviews.update_one(
        {"_id": oid},
        {"$set": {
            "helpful_by": list(helpful_by),
            "unhelpful_by": list(unhelpful_by),
            "helpful_count": len(helpful_by),
            "unhelpful_count": len(unhelpful_by),
        }},
    )
    return {"helpful_count": len(helpful_by), "unhelpful_count": len(unhelpful_by)}


@router.get("/admin/all", dependencies=[Depends(require_admin)])
async def admin_all_reviews(product_id: str | None = None):
    db = get_db()
    query = {"product_id": product_id} if product_id else {}
    docs = await db.reviews.find(query).sort("created_at", -1).to_list(length=1000)
    # Attach the product title for context in the admin panel.
    titles: dict[str, str] = {}
    out = []
    for d in docs:
        pid = d.get("product_id")
        if pid and pid not in titles:
            prod = await db.products.find_one({"_id": to_object_id(pid)}) if pid else None
            titles[pid] = (prod or {}).get("title", "Unknown product")
        item = _public(d)
        item["product_title"] = titles.get(pid, "Unknown product")
        out.append(item)
    return out
