import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from app.core.config import settings as app_settings
from app.core.security import (
    create_access_token,
    create_signup_token,
    decode_signup_token,
    hash_password,
    verify_password,
)
from app.db.mongodb import get_db
from app.deps import get_current_user
from app.models.common import serialize, to_object_id
from app.models.user import (
    AuthResponse,
    OtpRequest,
    OtpVerify,
    ProfileUpdate,
    SignupComplete,
    UserLogin,
    UserPublic,
)
from app.services import email_service, notifications

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


def _otp_hash(code: str, email: str) -> str:
    """Keyed hash so codes aren't stored in plaintext at rest."""
    return hashlib.sha256(
        f"{code}:{email}:{app_settings.JWT_SECRET}".encode()
    ).hexdigest()


@router.post("/otp/request")
async def request_otp(body: OtpRequest):
    """Step 1 of signup: email a 6-digit verification code (rate-limited)."""
    db = get_db()
    email = body.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="Email already registered. Please log in.")

    now = datetime.now(timezone.utc)
    existing = await db.otps.find_one({"_id": email})
    if existing:
        last = existing.get("last_sent_at")
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last:
            elapsed = (now - last).total_seconds()
            if elapsed < app_settings.OTP_RESEND_COOLDOWN_SECONDS:
                wait = int(app_settings.OTP_RESEND_COOLDOWN_SECONDS - elapsed)
                raise HTTPException(status_code=429, detail=f"Please wait {wait}s before requesting another code.")

    code = f"{secrets.randbelow(1000000):06d}"
    await db.otps.update_one(
        {"_id": email},
        {"$set": {
            "code_hash": _otp_hash(code, email),
            "expires_at": now + timedelta(minutes=app_settings.OTP_TTL_MINUTES),
            "attempts": 0,
            "last_sent_at": now,
        }},
        upsert=True,
    )

    sent = email_service.send_otp_email(email, code, app_settings.OTP_TTL_MINUTES)
    if not sent and app_settings.RESEND_API_KEY:
        raise HTTPException(status_code=502, detail="Could not send the verification email. Please try again.")
    if not sent:
        print(f"[otp] {email} -> {code} (email disabled; dev only)")
    return {"ok": True, "message": f"We sent a 6-digit code to {email}.",
            "resend_in": app_settings.OTP_RESEND_COOLDOWN_SECONDS}


@router.post("/otp/verify")
async def verify_otp(body: OtpVerify):
    """Step 2: check the code and hand back a short-lived signup token."""
    db = get_db()
    email = body.email.lower()
    doc = await db.otps.find_one({"_id": email})
    if not doc:
        raise HTTPException(status_code=400, detail="Please request a new code.")

    exp = doc.get("expires_at")
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if not exp or datetime.now(timezone.utc) > exp:
        await db.otps.delete_one({"_id": email})
        raise HTTPException(status_code=400, detail="This code has expired. Request a new one.")
    if doc.get("attempts", 0) >= app_settings.OTP_MAX_ATTEMPTS:
        await db.otps.delete_one({"_id": email})
        raise HTTPException(status_code=429, detail="Too many attempts. Please request a new code.")
    if _otp_hash(body.code.strip(), email) != doc.get("code_hash"):
        await db.otps.update_one({"_id": email}, {"$inc": {"attempts": 1}})
        raise HTTPException(status_code=400, detail="Incorrect code. Please try again.")

    # Correct — burn the code so it can't be reused, and issue the signup token.
    await db.otps.delete_one({"_id": email})
    return {"verified": True, "signup_token": create_signup_token(email)}


@router.post("/register", response_model=AuthResponse)
async def register(body: SignupComplete):
    """Step 3: consume the signup token (verified email) + profile -> account."""
    db = get_db()
    try:
        email = decode_signup_token(body.signup_token).lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Your verification expired. Please verify your email again.")

    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="Email already registered. Please log in.")

    doc = {
        "name": body.name.strip(),
        "email": email,
        "phone": body.phone.strip(),
        "password": hash_password(body.password),
        "role": "user",
        "addresses": [],
        "fcm_tokens": [],
        "email_verified": True,
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.users.insert_one(doc)
    doc["_id"] = res.inserted_id
    user = serialize(doc)

    # First-order welcome nudge: a little after signup, if a first-order coupon
    # is live then, remind them to use it (checked at send time).
    await notifications.schedule(
        db, user["id"], "first_order_welcome", notifications.WELCOME_DELAY_MIN
    )

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


@router.post("/google", response_model=AuthResponse)
async def google_auth(id_token: str = Body(..., embed=True)):
    """Sign in with a Google ID token from the app; upsert the user, issue JWT."""
    if not app_settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google sign-in is not configured")

    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token as g_id_token

    try:
        info = g_id_token.verify_oauth2_token(
            id_token, g_requests.Request(), app_settings.GOOGLE_CLIENT_ID
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    email = (info.get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email")

    db = get_db()
    doc = await db.users.find_one({"email": email})
    if not doc:
        doc = {
            "name": info.get("name") or email.split("@")[0],
            "email": email,
            "phone": None,
            "password": None,
            "role": "user",
            "provider": "google",
            "google_sub": info.get("sub"),
            "avatar": info.get("picture"),
            "addresses": [],
            "fcm_tokens": [],
            "created_at": datetime.now(timezone.utc),
        }
        res = await db.users.insert_one(doc)
        doc["_id"] = res.inserted_id
        await notifications.schedule(
            db, str(doc["_id"]), "first_order_welcome", notifications.WELCOME_DELAY_MIN
        )
    else:
        # Link Google to an existing email-based account; backfill avatar.
        patch = {}
        if not doc.get("provider"):
            patch["provider"] = "google"
        if not doc.get("google_sub"):
            patch["google_sub"] = info.get("sub")
        if not doc.get("avatar") and info.get("picture"):
            patch["avatar"] = info.get("picture")
        if patch:
            await db.users.update_one({"_id": doc["_id"]}, {"$set": patch})
            doc.update(patch)

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
