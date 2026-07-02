from pydantic import BaseModel, Field


class Variant(BaseModel):
    color: str | None = None
    size: str | None = None
    sku: str | None = None
    stock: int = 0


class ProductCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = ""
    brand: str | None = None
    category_id: str | None = None
    price: float = Field(ge=0)
    mrp: float | None = Field(default=None, ge=0)
    images: list[str] = []
    colors: list[str] = []
    sizes: list[str] = []
    variants: list[Variant] = []
    is_active: bool = True


class ProductUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    brand: str | None = None
    category_id: str | None = None
    price: float | None = None
    mrp: float | None = None
    images: list[str] | None = None
    colors: list[str] | None = None
    sizes: list[str] | None = None
    variants: list[Variant] | None = None
    is_active: bool | None = None
