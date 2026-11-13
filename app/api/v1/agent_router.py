"""POST /agent/chat - natural-language front door to the MCP tools (Chapter 7)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agent.service import AgentService
from app.api.deps import CurrentUser, get_container, oauth2_scheme
from app.container import Container

router = APIRouter(prefix="/agent", tags=["agent"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ToolTraceOut(BaseModel):
    tool: str
    arguments: dict
    result: str
    is_error: bool


class ChatResponse(BaseModel):
    answer: str
    steps: int
    tool_calls: list[ToolTraceOut]
    unavailable_servers: list[str]


def get_agent_service(c: Annotated[Container, Depends(get_container)]) -> AgentService:
    return AgentService(c)  # override in tests to swap the whole agent


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, user: CurrentUser,
               token: Annotated[str, Depends(oauth2_scheme)],
               agent: Annotated[AgentService, Depends(get_agent_service)]):
    result = await agent.chat(body.message, user_token=token)  # user's own token => user's own scopes
    return ChatResponse(answer=result.answer, steps=result.steps, unavailable_servers=result.unavailable_servers,
                        tool_calls=[ToolTraceOut(tool=t.tool, arguments=t.arguments, result=t.result,
                                                 is_error=t.is_error) for t in result.trace])
