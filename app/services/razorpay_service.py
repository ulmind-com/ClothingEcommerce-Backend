import razorpay

from app.core.config import settings

_client: razorpay.Client | None = None


def _get_client() -> razorpay.Client:
    global _client
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        raise RuntimeError("Razorpay is not configured")
    if _client is None:
        _client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
    return _client


def create_order(amount_paise: int, receipt: str, currency: str = "INR") -> dict:
    client = _get_client()
    return client.order.create(
        {"amount": amount_paise, "currency": currency, "receipt": receipt}
    )


def verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    client = _get_client()
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )
        return True
    except razorpay.errors.SignatureVerificationError:
        return False
