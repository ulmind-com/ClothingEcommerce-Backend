from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.db.mongodb import close_mongo_connection, connect_to_mongo
from app.routers import (
    auth,
    banners,
    categories,
    coupons,
    orders,
    products,
    reviews,
    settings as settings_router,
    upload,
    users,
    wishlist,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    yield
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


@app.get("/", tags=["health"])
async def health():
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.ENV}
