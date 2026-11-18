from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class TerminalInput(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(default='', max_length=160)
    venue: str = Field(min_length=1, max_length=120)
    declared_slots: List[int] = Field(min_length=1, max_length=200)


class EventInput(BaseModel):
    record_uid: str = Field(min_length=1, max_length=120)
    kind: Literal['exposure', 'rating']
    visitor: str = Field(min_length=1, max_length=120)
    exhibit: str = Field(min_length=1, max_length=120)
    occurred_at: str = Field(min_length=1, max_length=40)
    slot: int
    rating: Optional[float] = None
    venue: Optional[str] = Field(default=None, max_length=120)


class EventBatch(BaseModel):
    events: List[EventInput] = Field(min_length=1, max_length=1000)
