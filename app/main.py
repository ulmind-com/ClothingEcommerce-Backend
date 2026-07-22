import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_db
from app.routers import (
    auth,
    banners,
    categories,
    chat,
    coupons,
    home_sections,
    notifications as notifications_router,
    orders,
    products,
    recommendations,
    returns,
    reviews,
    search,
    settings as settings_router,
    site_media,
    upload,
    users,
    wishlist,
)
from app.services import notifications as notif_service

SWEEP_SECONDS = 60


async def _notification_sweeper():
    """Send scheduled notifications whose time has come, once a minute."""
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await notif_service.run_due(get_db())
        except Exception as e:  # pragma: no cover
            print(f"[notif] sweeper error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    sweeper = asyncio.create_task(_notification_sweeper())
    yield
    sweeper.cancel()
    await close_mongo_connection()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(categories.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(wishlist.router)
app.include_router(reviews.router)
app.include_router(coupons.router)
app.include_router(banners.router)
app.include_router(settings_router.router)
app.include_router(users.router)
app.include_router(upload.router)
app.include_router(search.router)
app.include_router(recommendations.router)
app.include_router(home_sections.router)
app.include_router(notifications_router.router)
app.include_router(chat.router)
app.include_router(returns.router)
app.include_router(site_media.router)


import os

from fastapi.responses import FileResponse

_BRAND_IMAGE = os.path.join(os.path.dirname(__file__), "static", "notification-image.png")


@app.get("/static/notification-image.png", tags=["static"])
async def notification_image():
    """Public brand image shown alongside push notifications."""
    return FileResponse(_BRAND_IMAGE, media_type="image/png")


@app.get("/", tags=["health"])
async def health():
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.ENV}
