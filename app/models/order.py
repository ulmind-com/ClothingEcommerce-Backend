from pydantic import BaseModel, Field


class OrderItemIn(BaseModel):
    product_id: str
    qty: int = Field(ge=1)
    color: str | None = None
    size: str | None = None


class Address(BaseModel):
    name: str
    phone: str
    line: str
    city: str
    pincode: str


class OrderCreate(BaseModel):
    items: list[OrderItemIn]
    address: Address


class OrderVerify(BaseModel):
    order_id: str  # our order id
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
