from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings


class _DB:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None


_state = _DB()


async def connect_to_mongo() -> None:
    _state.client = AsyncIOMotorClient(settings.MONGO_URI)
    _state.db = _state.client[settings.MONGO_DB]
    await _ensure_indexes()


async def close_mongo_connection() -> None:
    if _state.client:
        _state.client.close()


def get_db() -> AsyncIOMotorDatabase:
    assert _state.db is not None, "Mongo not initialized"
    return _state.db


async def _ensure_indexes() -> None:
    db = get_db()
    await db.users.create_index("email", unique=True)
    await db.categories.create_index("slug", unique=True)
    await db.categories.create_index("parent_id")
    await db.products.create_index([("title", "text"), ("description", "text")])
    await db.products.create_index("category_id")
    await db.orders.create_index("user_id")
    await db.notifications.create_index([("user_id", 1), ("created_at", -1)])
    await db.scheduled_notifications.create_index([("status", 1), ("due_at", 1)])
