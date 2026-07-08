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


@router.get("/suggestions")
async def suggestions():
    """Quick questions the app offers as taps."""
    return {"questions": agent.SUGGESTIONS}


@router.post("")
async def chat(body: ChatIn, user: dict = Depends(get_current_user)):
    history = [m.model_dump() for m in body.messages]
    text = await agent.reply(get_db(), user, history)
    return {"reply": text}
