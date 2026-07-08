"""Customer-support chat agent (Groq, OpenAI-compatible).

Flipkart-style: the app shows a few quick questions and the AI answers them,
grounded in the store's policies and the customer's own recent orders.
"""
import asyncio

import requests

from app.core.config import settings
from app.services.pricing import get_settings

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Quick-reply questions surfaced in the app — kept short so they fit one line.
SUGGESTIONS = [
    "Where's my order?",
    "Cancel an order",
    "Payment methods",
    "Delivery charges",
    "Offers & coupons",
    "Return an item",
]


def _key() -> str:
    return settings.GROQ_AGENT_API_KEY or settings.GROQ_API_KEY


def _call_groq(messages: list[dict]) -> str:
    resp = requests.post(
        _GROQ_URL,
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
        json={
            "model": settings.GROQ_MODEL,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 400,
        },
        timeout=settings.GROQ_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _order_block(db, user: dict, order_id: str, currency: str) -> str:
    from app.models.common import to_object_id
    try:
        o = await db.orders.find_one({"_id": to_object_id(order_id)})
    except Exception:
        o = None
    if not o or o.get("user_id") != user["id"]:
        return ""
    oid = str(o["_id"])[-6:].upper()
    items = ", ".join(
        f"{it.get('title')} (x{it.get('qty')}{', ' + it['size'] if it.get('size') else ''}{', ' + it['color'] if it.get('color') else ''})"
        for it in (o.get("items") or [])
    )
    lines = [
        "IMPORTANT: The customer opened this chat from a specific order — focus your help on THIS order:",
        f"- Order #{oid}: status={o.get('status')}, total={currency}{o.get('amount')}, payment={'online' if o.get('payment_method') == 'online' else 'COD'}",
        f"- Items: {items}",
    ]
    if o.get("status") == "cancelled":
        lines.append("- This order is cancelled" + (" and a refund was initiated." if o.get("refund_status") == "initiated" else "."))
    return "\n".join(lines)


async def _system_prompt(db, user: dict, order_id: str | None = None) -> str:
    s = await get_settings(db)
    lines = [
        "You are 'Cleo', the friendly customer-support assistant for the Clothing store, a fashion e-commerce app.",
        "Only help with this store: orders, tracking, cancellations, delivery, payments, coupons/offers and products.",
        "Be concise, warm and helpful (2-5 sentences). If something needs a human or you're unsure, say you'll connect them to the support team.",
        "Never invent order details — only use the orders listed below.",
        f"Currency: {s.currency}. Payments accepted: Cash on Delivery, or online UPI / Card / Netbanking (Razorpay).",
        (
            f"Cancellations: a customer can cancel an order from its tracking screen within {int(s.cancel_window_hours)} hour(s) of placing it, as long as it hasn't shipped."
            if s.cancel_window_hours
            else "Order cancellation is currently unavailable."
        ),
        "Delivery is distance-based: free within a set radius or above an order value, otherwise a small fee shown at checkout.",
        "Offers/coupons appear in the Offers tab and auto-apply at checkout.",
    ]
    docs = await db.orders.find({"user_id": user["id"]}).sort("created_at", -1).to_list(length=5)
    if docs:
        lines.append("This customer's recent orders (newest first):")
        for d in docs:
            oid = str(d["_id"])[-6:].upper()
            lines.append(
                f"- #{oid}: status={d.get('status')}, total={s.currency}{d.get('amount')}, items={len(d.get('items', []))}, paid={'online' if d.get('payment_method') == 'online' else 'COD'}"
            )
    else:
        lines.append("This customer has no orders yet.")
    if order_id:
        block = await _order_block(db, user, order_id, s.currency)
        if block:
            lines.append(block)
    return "\n".join(lines)


async def reply(db, user: dict, history: list[dict], order_id: str | None = None) -> str:
    if not _key():
        return "Sorry, chat support isn't available right now. Please try again later."
    messages = [{"role": "system", "content": await _system_prompt(db, user, order_id)}]
    for m in (history or [])[-10:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = str(m.get("content", "")).strip()[:1000]
        if content:
            messages.append({"role": role, "content": content})
    if len(messages) == 1:
        return "Hi! I'm Cleo 👋 How can I help you with your orders, delivery, payments or offers today?"
    try:
        return await asyncio.to_thread(_call_groq, messages)
    except Exception as e:  # pragma: no cover
        print(f"[agent] groq call failed: {e}")
        return "Sorry, I'm having a little trouble right now. Please try again in a moment."
