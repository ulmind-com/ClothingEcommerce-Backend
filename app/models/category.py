from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=1, max_length=100)
    parent_id: str | None = None  # None = top-level (Mens, Womens ...)
    image: str | None = None
    order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    parent_id: str | None = None
    image: str | None = None
    order: int | None = None
