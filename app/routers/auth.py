from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import create_access_token, hash_password, verify_password
from app.db.mongodb import get_db
from app.deps import get_current_user
from app.models.common import serialize, to_object_id
from app.models.user import (
    AuthResponse,
    ProfileUpdate,
    UserLogin,
    UserPublic,
    UserRegister,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _public(doc: dict) -> UserPublic:
    return UserPublic(
        id=doc["id"],
        name=doc["name"],
        email=doc["email"],
        phone=doc.get("phone"),
        avatar=doc.get("avatar"),
        role=doc.get("role", "user"),
    )


@router.post("/register", response_model=AuthResponse)
async def register(body: UserRegister):
    db = get_db()
    if await db.users.find_one({"email": body.email.lower()}):
        raise HTTPException(status_code=409, detail="Email already registered")

    doc = {
        "name": body.name,
        "email": body.email.lower(),
        "phone": body.phone,
        "password": hash_password(body.password),
        "role": "user",
        "addresses": [],
        "fcm_tokens": [],
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.users.insert_one(doc)
    doc["_id"] = res.inserted_id
    user = serialize(doc)
    token = create_access_token(user["id"], user["role"])
    return AuthResponse(access_token=token, user=_public(user))


@router.post("/login", response_model=AuthResponse)
async def login(body: UserLogin):
    db = get_db()
    doc = await db.users.find_one({"email": body.email.lower()})
    if not doc or not verify_password(body.password, doc.get("password", "")):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user = serialize(doc)
    token = create_access_token(user["id"], user.get("role", "user"))
    return AuthResponse(access_token=token, user=_public(user))


@router.get("/me", response_model=UserPublic)
async def me(user: dict = Depends(get_current_user)):
    return _public(user)


@router.patch("/me", response_model=UserPublic)
async def update_me(body: ProfileUpdate, user: dict = Depends(get_current_user)):
    db = get_db()
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.phone is not None:
        updates["phone"] = body.phone.strip() or None
    if body.avatar is not None:
        updates["avatar"] = body.avatar or None
    if updates:
        await db.users.update_one({"_id": to_object_id(user["id"])}, {"$set": updates})
    doc = await db.users.find_one({"_id": to_object_id(user["id"])})
    return _public(serialize(doc))
