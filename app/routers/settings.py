from fastapi import APIRouter, Depends

from app.db.mongodb import get_db
from app.deps import require_admin
from app.models.settings import Settings, SettingsUpdate
from app.services import pricing

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=Settings)
async def read_settings():
    """Public: mobile app reads currency, tax, shop, delivery rules."""
    return await pricing.get_settings(get_db())


@router.put("", response_model=Settings, dependencies=[Depends(require_admin)])
async def update_settings(body: SettingsUpdate):
    db = get_db()
    current = await pricing.get_settings(db)
    data = current.model_dump()
    patch = body.model_dump(exclude_none=True)
    data.update(patch)
    merged = Settings(**data)
    return await pricing.save_settings(db, merged)
