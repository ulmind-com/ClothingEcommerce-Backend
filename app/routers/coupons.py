from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.mongodb import get_db
from app.deps import get_current_user, require_admin
from app.models.common import serialize, to_object_id
from app.services.pricing import coupon_discount

router = APIRouter(prefix="/coupons", tags=["coupons"])


class CouponIn(BaseModel):
    code: str
    type: str = "percent"          # "percent" | "flat"
    value: float = Field(ge=0)
    min_order: float = 0
    max_discount: float = 0        # 0 = no cap (percent only)
    active: bool = True
    valid_from: str | None = None   # ISO datetime — coupon starts showing/working
    valid_until: str | None = None  # ISO datetime — auto-expires after this
    description: str = ""


def _parse(dt: str | None):
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt)
    except ValueError:
        return None


def _in_window(coupon: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    vf, vu = _parse(coupon.get("valid_from")), _parse(coupon.get("valid_until"))
    if vf and now < vf:
        return False
    if vu and now > vu:
        return False
    return True


async def get_coupon(db, code: str):
    if not code:
        return None
    c = await db.coupons.find_one({"code": code.strip().upper(), "active": True})
    return c if (c and _in_window(c)) else None


@router.get("", dependencies=[Depends(require_admin)])
async def list_coupons():
    db = get_db()
    docs = await db.coupons.find().sort("created_at", -1).to_list(length=200)
    return [serialize(d) for d in docs]


@router.get("/active")
async def active_coupons():
    """Public: coupons currently within their time window (for the Offers screen)."""
    db = get_db()
    docs = await db.coupons.find({"active": True}).sort("created_at", -1).to_list(length=200)
    return [serialize(d) for d in docs if _in_window(d)]


@router.post("", dependencies=[Depends(require_admin)])
async def create_coupon(body: CouponIn):
    db = get_db()
    code = body.code.strip().upper()
    if await db.coupons.find_one({"code": code}):
        raise HTTPException(status_code=409, detail="Coupon code already exists")
    doc = body.model_dump()
    doc["code"] = code
    doc["created_at"] = datetime.now(timezone.utc)
    res = await db.coupons.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize(doc)


@router.patch("/{coupon_id}", dependencies=[Depends(require_admin)])
async def update_coupon(coupon_id: str, body: dict = Body(...)):
    db = get_db()
    body.pop("id", None)
    if "code" in body and body["code"]:
        body["code"] = body["code"].strip().upper()
    res = await db.coupons.find_one_and_update(
        {"_id": to_object_id(coupon_id)}, {"$set": body}, return_document=True
    )
    if not res:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return serialize(res)


@router.delete("/{coupon_id}", dependencies=[Depends(require_admin)])
async def delete_coupon(coupon_id: str):
    db = get_db()
    await db.coupons.delete_one({"_id": to_object_id(coupon_id)})
    return {"deleted": True}


@router.post("/validate")
async def validate_coupon(
    code: str = Body(..., embed=True),
    subtotal: float = Body(0, embed=True),
    user: dict = Depends(get_current_user),
):
    db = get_db()
    coupon = await get_coupon(db, code)
    if not coupon:
        return {"valid": False, "discount": 0, "message": "Invalid or expired coupon"}
    if subtotal < coupon.get("min_order", 0):
        return {
            "valid": False,
            "discount": 0,
            "message": f"Minimum order ₹{coupon['min_order']:.0f} required",
        }
    discount = coupon_discount(coupon, subtotal)
    return {
        "valid": True,
        "discount": discount,
        "code": coupon["code"],
        "message": f"You saved ₹{discount:.0f}!",
    }
