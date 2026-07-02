from pydantic import BaseModel, Field


class OrderItemIn(BaseModel):
    product_id: str
    qty: int = Field(ge=1)
    color: str | None = None
    size: str | None = None


class Address(BaseModel):
    tag: str = "Home"          # Home / Work / Other
    name: str = ""
    house: str = ""            # House / Flat / Block no
    area: str = ""             # apartment / road / area
    city: str = ""
    state: str = ""
    pincode: str = ""
    phone: str = ""
    lat: float | None = None
    lng: float | None = None


class OrderCreate(BaseModel):
    items: list[OrderItemIn]
    address: Address
    payment_method: str = "online"   # "online" | "cod"
    coupon_code: str | None = None


class OrderVerify(BaseModel):
    order_id: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
