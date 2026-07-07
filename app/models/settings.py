from pydantic import BaseModel, Field


class DeliverySlab(BaseModel):
    up_to_km: float          # applies up to this distance
    fee: float               # flat fee for this slab


class DeliveryConfig(BaseModel):
    free_radius_km: float = 3          # free within this radius
    per_km_rate: float = 8             # charge per km beyond free radius (if no slabs)
    base_fee: float = 0                # base fee beyond free radius
    free_above: float = 0              # order subtotal for free delivery (0 = off)
    max_service_km: float = 30         # not deliverable beyond this
    slabs: list[DeliverySlab] = []     # optional slab-based pricing (overrides per_km)


class FirstOrderConfig(BaseModel):
    """A one-time discount for a customer's very first order.

    Fully isolated from coupons. `enabled=False` (the default) means it has no
    effect whatsoever on the existing order flow.
    """
    enabled: bool = False
    type: str = "percent"      # "percent" | "flat"
    value: float = 0           # percent (e.g. 10) or flat amount off
    min_order: float = 0       # minimum cart subtotal to qualify (0 = no minimum)
    max_discount: float = 0    # cap for percent discounts (0 = no cap)


class ShopConfig(BaseModel):
    name: str = "Clothing Store"
    address: str = ""
    phone: str = ""
    state: str = ""          # seller's state — same-state order = CGST+SGST, else IGST
    lat: float | None = None
    lng: float | None = None


class Settings(BaseModel):
    currency: str = "₹"
    currency_code: str = "INR"
    tax_rate: float = 0.05             # 5%
    shop: ShopConfig = ShopConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    first_order: FirstOrderConfig = FirstOrderConfig()


class SettingsUpdate(BaseModel):
    currency: str | None = None
    currency_code: str | None = None
    tax_rate: float | None = None
    shop: ShopConfig | None = None
    delivery: DeliveryConfig | None = None
    first_order: FirstOrderConfig | None = None
