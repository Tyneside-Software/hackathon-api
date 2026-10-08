"""Kanban board stored in SQLite."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..board_store import (
    BoardError,
    create_card,
    delete_card,
    duplicate_card,
    export_state,
    get_board,
    move_card,
    reorder_cards,
    update_card,
)

router = APIRouter(prefix="/v1/board", tags=["board"])


class CommitIn(BaseModel):
    repo: str
    sha: str
    summary: str = ""


class CardIn(BaseModel):
    title: str
    by: str = ""
    person: str = ""
    owners: list[str] | None = None
    hours: float | None = None
    column: str = "todo"
    brief: str = ""
    tag: str = ""
    tag_kind: str = ""
    value: int | None = 3
    id: str | None = None
    commits: list[CommitIn] | None = None


class CardPatch(BaseModel):
    by: str = ""
    title: str | None = None
    person: str | None = None
    owners: list[str] | None = None
    hours: float | None = None
    brief: str | None = None
    tag: str | None = None
    tag_kind: str | None = None
    value: int | None = None
    column: str | None = None
    reason: str = ""


class MoveIn(BaseModel):
    column: str
    by: str = ""
    reason: str = ""


class ReorderIn(BaseModel):
    column: str
    ids: list[str] = Field(default_factory=list)
    by: str = ""


class ActorIn(BaseModel):
    by: str = ""


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except BoardError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("")
def read_board() -> dict:
    return _call(get_board)


@router.get("/export")
def read_export() -> dict:
    return _call(export_state)


@router.post("/cards")
def add_card(body: CardIn) -> dict:
    data = body.model_dump()
    data["card_id"] = data.pop("id", None)
    if data.get("commits") is not None:
        data["commits"] = [item if isinstance(item, dict) else item for item in data["commits"]]
    return _call(create_card, None, **data)


@router.patch("/cards/{card_id}")
def patch_card(card_id: str, body: CardPatch) -> dict:
    data = body.model_dump(exclude_unset=True)
    data.pop("by", None)
    return _call(update_card, None, card_id, by="", fields=data)


@router.post("/cards/{card_id}/move")
def move(card_id: str, body: MoveIn) -> dict:
    return _call(move_card, None, card_id, **body.model_dump())


@router.post("/reorder")
def reorder(body: ReorderIn) -> dict:
    return _call(reorder_cards, None, **body.model_dump())


@router.post("/cards/{card_id}/duplicate")
def duplicate(card_id: str, body: ActorIn) -> dict:
    return _call(duplicate_card, None, card_id, by=body.by)


@router.delete("/cards/{card_id}")
def remove_card(card_id: str, by: str = "") -> dict:
    return _call(delete_card, None, card_id, by=by)
