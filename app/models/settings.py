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
    # How long after placing an order a customer may cancel it (in hours).
    # 0 disables self-cancellation entirely.
    cancel_window_hours: float = 24
    shop: ShopConfig = ShopConfig()
    delivery: DeliveryConfig = DeliveryConfig()


class SettingsUpdate(BaseModel):
    currency: str | None = None
    currency_code: str | None = None
    tax_rate: float | None = None
    cancel_window_hours: float | None = None
    shop: ShopConfig | None = None
    delivery: DeliveryConfig | None = None
