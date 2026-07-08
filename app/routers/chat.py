from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.db.mongodb import get_db
from app.deps import get_current_user
from app.services import agent

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatIn(BaseModel):
    messages: list[ChatMessage] = []
    order_id: str | None = None


@router.get("/suggestions")
async def suggestions(order_id: str | None = None, user: dict = Depends(get_current_user)):
    """Quick questions/actions — order-aware when an order_id is given."""
    return {"questions": await agent.suggestions_for(get_db(), user, order_id)}


@router.post("")
async def chat(body: ChatIn, user: dict = Depends(get_current_user)):
    history = [m.model_dump() for m in body.messages]
    text = await agent.reply(get_db(), user, history, body.order_id)
    return {"reply": text}
