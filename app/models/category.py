from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=1, max_length=100)
    parent_id: str | None = None  # None = top-level (Mens, Womens ...)
    image: str | None = None
    image_scale: float | None = None  # home pill image size multiplier (1.0 = default)
    order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    parent_id: str | None = None
    image: str | None = None
    image_scale: float | None = None
    order: int | None = None
