"""Customer-support chat agent (Groq, OpenAI-compatible) with tool use.

When the chat is opened for a specific order, the agent can actually act on the
customer's behalf — cancel the order or file a return/exchange — via tool calls,
scoped to that one order. Otherwise it answers questions grounded in store
policy and the customer's recent orders.
"""
import asyncio
import json

import requests

from app.core.config import settings
from app.models.common import to_object_id
from app.services import order_actions
from app.services.pricing import get_settings

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# General quick questions (no order in context).
SUGGESTIONS = [
    "Where's my order?",
    "Payment methods",
    "Delivery charges",
    "Offers & coupons",
    "How do returns work?",
    "Talk to a human",
]


def _key() -> str:
    return settings.GROQ_AGENT_API_KEY or settings.GROQ_API_KEY


def _groq(messages: list[dict], tools: list | None = None) -> dict:
    payload: dict = {"model": settings.GROQ_MODEL, "messages": messages, "temperature": 0.3, "max_tokens": 500}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    resp = requests.post(
        _GROQ_URL,
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
        json=payload,
        timeout=settings.GROQ_TIMEOUT + 4,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


async def _order(db, user, order_id):
    if not order_id:
        return None
    try:
        o = await db.orders.find_one({"_id": to_object_id(order_id)})
    except Exception:
        o = None
    return o if (o and o.get("user_id") == user["id"]) else None


async def _system_prompt(db, user: dict, order_id: str | None) -> str:
    s = await get_settings(db)
    contact = []
    if s.shop.phone:
        contact.append(f"call {s.shop.phone}")
    if s.shop.email:
        contact.append(f"email {s.shop.email}")
    human = " or ".join(contact) if contact else "our support line"

    lines = [
        "You are 'Cleo', the customer-support assistant for the Clothing store (a fashion e-commerce app).",
        "Help with orders, tracking, cancellations, returns/refunds, delivery, payments and offers. Be concise, warm and clear (2-5 sentences).",
        f"Currency: {s.currency}. Payments: Cash on Delivery, or online UPI/Card/Netbanking (Razorpay).",
        f"To reach a human, tell the customer to {human}.",
        "You can perform actions with the provided tools ONLY when the customer clearly confirms. For a return, ask whether they want a refund or an exchange and a short reason before calling the tool. Never claim you did something unless the tool succeeded.",
    ]

    order = await _order(db, user, order_id)
    if order:
        oid = str(order["_id"])[-6:].upper()
        items = ", ".join(
            f"{it.get('title')} (x{it.get('qty')}{', ' + it['size'] if it.get('size') else ''})"
            for it in (order.get("items") or [])
        )
        lines.append(f"CURRENT ORDER #{oid}: status={order.get('status')}, total={s.currency}{order.get('amount')}, payment={'online' if order.get('payment_method') == 'online' else 'COD'}. Items: {items}. Focus on THIS order.")
    else:
        docs = await db.orders.find({"user_id": user["id"]}).sort("created_at", -1).to_list(length=5)
        if docs:
            lines.append("Recent orders: " + "; ".join(f"#{str(d['_id'])[-6:].upper()} ({d.get('status')})" for d in docs))
    return "\n".join(lines)


_CANCEL_TOOL = {
    "type": "function",
    "function": {
        "name": "cancel_order",
        "description": "Cancel the customer's current order and start a refund if it was paid online. Only after the customer confirms.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
_RETURN_TOOL = {
    "type": "function",
    "function": {
        "name": "create_return",
        "description": "File a return/exchange request for the eligible items of the current order. Call after collecting a reason and whether they want a refund or an exchange.",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["refund", "exchange"]},
                "reason": {"type": "string", "description": "Short reason for the return"},
                "note": {"type": "string", "description": "Optional note, e.g. desired size for an exchange"},
            },
            "required": ["type", "reason"],
        },
    },
}


async def _tools_for(db, user, order_id):
    order = await _order(db, user, order_id)
    if not order:
        return []
    s = await get_settings(db)
    tools = []
    if await order_actions.can_cancel(db, order, s):
        tools.append(_CANCEL_TOOL)
    items, _ = await order_actions.eligible_return_items(db, order, s)
    if items:
        tools.append(_RETURN_TOOL)
    return tools


async def suggestions_for(db, user, order_id) -> list[str]:
    order = await _order(db, user, order_id)
    if not order:
        return SUGGESTIONS
    s = await get_settings(db)
    out = ["Where's my order?"]
    if await order_actions.can_cancel(db, order, s):
        out.append("Cancel this order")
    items, _ = await order_actions.eligible_return_items(db, order, s)
    if items:
        out.append("Return / exchange an item")
    out.append("Talk to a human")
    return out


async def _run_tool(db, user, order_id, name, args) -> dict:
    try:
        if name == "cancel_order":
            return await order_actions.cancel(db, user["id"], order_id)
        if name == "create_return":
            return await order_actions.create_return(
                db, user["id"], order_id, args.get("type", "refund"), args.get("reason", ""), args.get("note", "")
            )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # pragma: no cover
        return {"error": f"Something went wrong: {e}"}
    return {"error": "Unknown action"}


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
        return "Hi! I'm Cleo 👋 How can I help you today?"

    tools = await _tools_for(db, user, order_id)

    try:
        for _ in range(4):
            msg = await asyncio.to_thread(_groq, messages, tools or None)
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return msg.get("content") or "How else can I help?"
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                result = await _run_tool(db, user, order_id, name, args)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})
        # Ran out of tool iterations — one plain wrap-up.
        final = await asyncio.to_thread(_groq, messages, None)
        return final.get("content") or "Done."
    except Exception as e:  # pragma: no cover
        print(f"[agent] groq failed: {e}")
        return "Sorry, I'm having a little trouble right now. Please try again in a moment."
