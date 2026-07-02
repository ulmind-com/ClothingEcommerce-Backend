"""Firebase Cloud Messaging sender.

Safe to call even before Firebase is configured: it no-ops until
FIREBASE_CREDENTIALS is set AND firebase-admin is installed. Add the
service-account JSON path/contents in .env (FIREBASE_CREDENTIALS) and add
`firebase-admin` to requirements to activate — no other code changes needed.
"""
import json

from app.core.config import settings

_app = None
_ready = False
_tried = False


def _ensure_app():
    global _app, _ready, _tried
    if _tried:
        return _ready
    _tried = True
    if not settings.FIREBASE_CREDENTIALS:
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials

        raw = settings.FIREBASE_CREDENTIALS.strip()
        cred = credentials.Certificate(json.loads(raw) if raw.startswith("{") else raw)
        _app = firebase_admin.initialize_app(cred)
        _ready = True
    except Exception as e:  # pragma: no cover
        print(f"[fcm] not initialised: {e}")
        _ready = False
    return _ready


async def send_to_tokens(tokens: list[str], title: str, body: str, data: dict | None = None):
    """Send a push notification to the given device tokens. No-op if unconfigured."""
    if not tokens or not _ensure_app():
        return
    try:
        from firebase_admin import messaging

        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            tokens=tokens,
        )
        messaging.send_each_for_multicast(message)
    except Exception as e:  # pragma: no cover
        print(f"[fcm] send failed: {e}")


async def notify_user(db, user_id: str, title: str, body: str, data: dict | None = None):
    user = await db.users.find_one({"_id": _oid(user_id)})
    tokens = (user or {}).get("fcm_tokens", [])
    await send_to_tokens(tokens, title, body, data)


def _oid(v):
    from bson import ObjectId

    return ObjectId(v)
