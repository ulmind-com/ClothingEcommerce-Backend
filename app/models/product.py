from pydantic import BaseModel, Field


class SizeStock(BaseModel):
    size: str                          # e.g. "M"
    price: float | None = None         # per-size selling price (falls back to colour/base)
    mrp: float | None = None           # per-size MRP (falls back to colour/base)
    discount_pct: float | None = None  # per-size extra discount % (falls back to colour/base)
    discount_on: str | None = None     # "price" | "mrp" (falls back to colour/base)
    stock: int = 0                     # inventory for this colour + size


class ColorVariant(BaseModel):
    name: str                          # e.g. "Orange"
    hex: str = "#000000"               # swatch colour
    images: list[str] = []             # images shown when this colour is picked
    price: float | None = None         # per-colour selling price (falls back to base)
    mrp: float | None = None           # per-colour MRP (falls back to base)
    discount_pct: float | None = None  # per-colour extra discount % (falls back to base)
    discount_on: str | None = None     # "price" | "mrp" (falls back to base)
    stock: int = 0                     # inventory for this colour (when it has no per-size rows)
    sizes: list[SizeStock] = []        # per-size price + stock within this colour


class ProductCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = ""
    brand: str | None = None
    category_id: str | None = None

    mrp: float = Field(ge=0)                    # actual price (struck through)
    price: float = Field(ge=0)                  # needed / selling price
    discount_pct: float = Field(default=0, ge=0, le=95)  # admin extra discount
    discount_on: str = "price"                  # "mrp" | "price"

    images: list[str] = []                      # general gallery (no colour)
    colors: list[ColorVariant] = []             # colour-wise images + stock
    sizes: list[str] = []

    stock: int = 0                              # used when there are no colour variants
    low_stock_threshold: int = 5

    rating: float = Field(default=0, ge=0, le=5)
    review_count: int = 0
    sold_count: int = 0

    is_active: bool = True


class ProductUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    brand: str | None = None
    category_id: str | None = None
    mrp: float | None = None
    price: float | None = None
    discount_pct: float | None = None
    discount_on: str | None = None
    images: list[str] | None = None
    colors: list[ColorVariant] | None = None
    sizes: list[str] | None = None
    stock: int | None = None
    low_stock_threshold: int | None = None
    rating: float | None = None
    review_count: int | None = None
    sold_count: int | None = None
    is_active: bool | None = None
