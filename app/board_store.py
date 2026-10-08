"""Kanban rows in SQLite.

``app/board_seed.json`` is loaded only when this database has never held
cards. A database that already has cards is left alone, and a board that
has been emptied is not filled again. The seed file is the copy that
survives a machine that does not have the database yet.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import SessionLocal, utc_now
from .models import BoardCard, BoardEvent, BoardMeta, BoardPerson

SEED_PATH = Path(__file__).resolve().parent / "board_seed.json"

COLUMNS = ("backlog", "todo", "next", "doing", "ready", "done")
# Backlog is a side pile. These five are the ordered board, earliest first.
# Same order as the Hermione sprint board: To do, Next, In progress, Ready to deploy, Done.
ORDERED_COLUMNS = ("todo", "next", "doing", "ready", "done")
COLUMN_LABELS = {
    "backlog": "Backlog",
    "todo": "To do",
    "next": "Next",
    "doing": "In progress",
    "ready": "Ready to deploy",
    "done": "Done",
}
COLUMN_EMPTY = {
    "backlog": "Nothing waiting. Add a card, or send one back from the board.",
    "todo": "Nothing here.",
    "next": "Nothing lined up.",
    "doing": "Empty on purpose. Pull a card and ship it.",
    "ready": "Nothing ready to deploy.",
    "done": "Nothing finished yet.",
}
DEFAULT_PEOPLE = [
    {"id": "reeve", "name": "Reeve", "emoji": "🧭"},
    {"id": "connor", "name": "Connor", "emoji": "⚡"},
    {"id": "michael", "name": "Michael", "emoji": "🏗️"},
    {"id": "lewis", "name": "Lewis", "emoji": "🌱"},
    {"id": "noah", "name": "Noah", "emoji": "🔧"},
]


class BoardError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _own_session(session: Session | None) -> tuple[Session, bool]:
    if session is not None:
        return session, False
    return SessionLocal(), True


def _finish(session: Session, own: bool, payload: dict) -> dict:
    session.commit()
    if own:
        session.close()
    return payload


def column_index(column: str) -> int:
    try:
        return COLUMNS.index(column)
    except ValueError as exc:
        raise BoardError(f"Column must be one of {', '.join(COLUMNS)}.") from exc


def move_is_backward(previous: str, column: str) -> bool:
    """True when a card moves to an earlier work column.

    Used only to word the history line. A reason is optional.
    Backlog sits beside the board. Moving a card onto it, or back onto the
    board, is not a move backwards.
    """
    if not previous or not column or previous == column:
        return False
    if previous == "backlog" or column == "backlog":
        return False
    try:
        return ORDERED_COLUMNS.index(column) < ORDERED_COLUMNS.index(previous)
    except ValueError:
        return column_index(column) < column_index(previous)


def norm_id(value: object) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text.zfill(2)
    return text


def hours_out(value: float | int | None) -> float | int:
    number = round(float(value or 0), 2)
    if number == int(number):
        return int(number)
    return number


def _load_seed() -> dict:
    if not SEED_PATH.is_file():
        raise BoardError(f"Missing seed file {SEED_PATH.name}.", 500)
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    cards = data["cards"] if isinstance(data, dict) and "cards" in data else data
    if not isinstance(cards, list) or not cards:
        raise BoardError("Board seed has no cards.", 500)
    people = data.get("people") if isinstance(data, dict) else None
    events = data.get("events") if isinstance(data, dict) else None
    return {
        "people": people if isinstance(people, list) and people else DEFAULT_PEOPLE,
        "cards": cards,
        "events": events if isinstance(events, list) else [],
    }


def seed_if_empty(session: Session | None = None) -> bool:
    """Import the git seed once. Never replaces cards that are already stored."""
    session, own = _own_session(session)
    try:
        already = session.get(BoardMeta, "seeded")
        count = session.scalar(select(func.count()).select_from(BoardCard)) or 0
        if already is not None or count:
            if already is None:
                session.add(BoardMeta(key="seeded", value="existing"))
                session.commit()
            return False
        data = _load_seed()
        _replace_people(session, data["people"])
        ranks: dict[str, int] = {col: 0 for col in COLUMNS}
        now = utc_now()
        for raw in data["cards"]:
            column = str(raw.get("column") or "todo")
            if column not in COLUMN_LABELS:
                column = "todo"
            card_id = norm_id(raw.get("id"))
            if not card_id or session.get(BoardCard, card_id) is not None:
                continue
            person = str(raw.get("person") or "").strip().lower()
            rank = raw.get("rank")
            if rank is None:
                rank = ranks[column]
            ranks[column] = max(ranks[column], int(rank) + 1)
            known, hours = _hours_from_seed(raw.get("hours"))
            session.add(
                BoardCard(
                    id=card_id,
                    person=person,
                    owners=_owners_text(raw.get("owners")),
                    hours=hours,
                    hours_known=known,
                    emoji=str(raw.get("emoji") or ""),
                    title=str(raw.get("title") or f"Card {card_id}")[:240],
                    column=column,
                    tag=str(raw.get("tag") or "")[:120],
                    tag_kind=str(raw.get("tag_kind") or "")[:16],
                    brief=str(raw.get("brief") or ""),
                    value=_seed_value(raw.get("value")),
                    commits=_commits_text(raw.get("commits")),
                    rank=int(rank),
                    created_at=now,
                    updated_at=now,
                )
            )
        for raw in data["events"]:
            if not isinstance(raw, dict):
                continue
            session.add(
                BoardEvent(
                    card_id=norm_id(raw.get("card_id")),
                    at=str(raw.get("at") or now),
                    by=str(raw.get("by") or ""),
                    action=str(raw.get("action") or ""),
                    from_column=str(raw.get("from_column") or ""),
                    to_column=str(raw.get("to_column") or ""),
                    reason=str(raw.get("reason") or ""),
                    detail=str(raw.get("detail") or ""),
                )
            )
        session.add(BoardMeta(key="seeded", value=now))
        session.add(BoardMeta(key="seed_source", value="app/board_seed.json"))
        session.commit()
        return True
    finally:
        if own:
            session.close()


def _replace_people(session: Session, people: list[dict]) -> None:
    for row in session.scalars(select(BoardPerson)).all():
        session.delete(row)
    session.flush()
    for index, raw in enumerate(people):
        pid = str(raw.get("id") or "").strip().lower()
        if not pid:
            continue
        session.add(
            BoardPerson(
                id=pid,
                name=str(raw.get("name") or pid.title()),
                emoji=str(raw.get("emoji") or ""),
                sort_order=index,
            )
        )


def _people(session: Session) -> list[BoardPerson]:
    return list(session.scalars(select(BoardPerson).order_by(BoardPerson.sort_order, BoardPerson.id)).all())


def _require_person(session: Session, by: str) -> BoardPerson:
    key = (by or "").strip().lower()
    row = session.get(BoardPerson, key)
    if row is None:
        raise BoardError("Choose a person on this board (the I am field).")
    return row


def _find_card(session: Session, card_id: object) -> BoardCard:
    key = str(card_id or "").strip()
    row = session.get(BoardCard, key)
    if row is None and key.isdigit():
        row = session.get(BoardCard, key.zfill(2))
        if row is None:
            row = session.get(BoardCard, str(int(key)))
    if row is None:
        raise BoardError(f"No card {card_id}.", 404)
    return row


def _person_name(session: Session, pid: str) -> str:
    row = session.get(BoardPerson, (pid or "").strip().lower())
    return row.name if row else (pid or "Someone")


def _event(
    session: Session,
    card: BoardCard,
    by: str,
    action: str,
    detail: str,
    from_column: str = "",
    to_column: str = "",
    reason: str = "",
) -> None:
    session.add(
        BoardEvent(
            card_id=card.id,
            at=utc_now(),
            by=(by or "").strip().lower(),
            action=action,
            from_column=from_column,
            to_column=to_column,
            reason=(reason or "").strip(),
            detail=detail,
        )
    )


def _events_for(session: Session, card_id: str) -> list[dict]:
    rows = session.scalars(
        select(BoardEvent).where(BoardEvent.card_id == card_id).order_by(BoardEvent.id.desc())
    ).all()
    return [_event_out(row) for row in rows[:40]]


def _event_out(row: BoardEvent) -> dict:
    return {
        "id": row.id,
        "card_id": row.card_id,
        "at": row.at,
        "by": row.by,
        "action": row.action,
        "from_column": row.from_column,
        "to_column": row.to_column,
        "reason": row.reason,
        "detail": row.detail,
    }


def _card_out(session: Session, card: BoardCard, with_events: bool = True) -> dict:
    payload = {
        "id": card.id,
        "person": card.person or "",
        "owners": _owners_of(card),
        "hours": None if not int(card.hours_known or 0) else hours_out(card.hours),
        "emoji": card.emoji,
        "title": card.title,
        "column": card.column,
        "tag": card.tag or "",
        "tag_kind": card.tag_kind or "",
        "brief": card.brief or "",
        "value": int(card.value) if card.value is not None else None,
        "commits": _commits_of(card),
        "rank": int(card.rank or 0),
        "updated_at": card.updated_at or "",
    }
    if with_events:
        payload["events"] = _events_for(session, card.id)
    return payload


def _columns_out() -> list[dict]:
    return [
        {"id": col, "label": COLUMN_LABELS[col], "empty": COLUMN_EMPTY[col]}
        for col in COLUMNS
    ]


def board_payload(session: Session) -> dict:
    cards = list(session.scalars(select(BoardCard).order_by(BoardCard.rank, BoardCard.id)).all())
    cards.sort(key=lambda card: (column_index(card.column) if card.column in COLUMN_LABELS else 99, card.rank, card.id))
    seeded = session.get(BoardMeta, "seeded")
    return {
        "source": "sqlite",
        "seeded": bool(seeded),
        "people": [
            {"id": row.id, "name": row.name, "emoji": row.emoji}
            for row in _people(session)
        ],
        "columns": _columns_out(),
        "cards": [_card_out(session, card) for card in cards],
    }


def export_state(session: Session | None = None) -> dict:
    """Shape written back to the git seed. Events are oldest first."""
    session, own = _own_session(session)
    try:
        payload = board_payload(session)
        events = session.scalars(select(BoardEvent).order_by(BoardEvent.id)).all()
        cards = []
        for card in payload["cards"]:
            item = dict(card)
            item.pop("events", None)
            item.pop("updated_at", None)
            cards.append(item)
        return {
            "people": payload["people"],
            "cards": cards,
            "events": [
                {
                    "id": row.id,
                    "card_id": row.card_id,
                    "at": row.at,
                    "by": row.by,
                    "action": row.action,
                    "from_column": row.from_column,
                    "to_column": row.to_column,
                    "reason": row.reason,
                    "detail": row.detail,
                }
                for row in events
            ],
        }
    finally:
        if own:
            session.close()


def get_board(session: Session | None = None) -> dict:
    session, own = _own_session(session)
    try:
        payload = board_payload(session)
        if own:
            session.close()
        return payload
    except Exception:
        if own:
            session.close()
        raise


def _next_id(session: Session) -> str:
    nums = []
    for card_id in session.scalars(select(BoardCard.id)).all():
        if str(card_id).isdigit():
            nums.append(int(card_id))
    return f"{max(nums, default=0) + 1:02d}"


def _next_rank(session: Session, column: str) -> int:
    current = session.scalar(select(func.max(BoardCard.rank)).where(BoardCard.column == column))
    return 0 if current is None else int(current) + 1


def _clean_brief(brief: str, title: str) -> str:
    raw = (brief or "").strip()
    if not raw:
        return f"<p>{html.escape(title)}</p>"
    if len(raw) > 100_000:
        raise BoardError("Brief is too long.")
    if raw.startswith("<"):
        return raw
    parts = [f"<p>{html.escape(line.strip())}</p>" for line in raw.splitlines() if line.strip()]
    return "".join(parts) or f"<p>{html.escape(title)}</p>"


def _clean_title(title: str) -> str:
    text = " ".join((title or "").split())
    if not text:
        raise BoardError("A card needs a title.")
    if len(text) > 240:
        raise BoardError("Title is too long.")
    return text


def _hours_from_seed(hours: object) -> tuple[int, float]:
    if hours is None:
        return 0, 0.0
    return 1, float(hours or 0)


def _owners_text(raw: object) -> str:
    if not isinstance(raw, list):
        return ""
    found: list[str] = []
    for item in raw:
        key = str(item or "").strip().lower()
        if key and key not in found:
            found.append(key)
    return json.dumps(found) if found else ""


def _commits_text(raw: object) -> str:
    if not isinstance(raw, list) or not raw:
        return ""
    return json.dumps(raw)


def _seed_value(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    if number < 1 or number > 5:
        return None
    return number


def _owners_of(card: BoardCard) -> list[str]:
    raw = (card.owners or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            found: list[str] = []
            for item in data:
                key = str(item or "").strip().lower()
                if key and key not in found:
                    found.append(key)
            return found
    return [card.person] if card.person else []


def _commits_of(card: BoardCard) -> list[dict]:
    raw = (card.commits or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    found = []
    for item in data:
        if isinstance(item, dict) and item.get("repo") and item.get("sha"):
            found.append(
                {
                    "repo": str(item["repo"]),
                    "sha": str(item["sha"]),
                    "summary": str(item.get("summary") or ""),
                }
            )
    return found


def _store_owners(session: Session, card: BoardCard, owners: list[str]) -> None:
    card.owners = json.dumps(owners) if owners else "[]"
    card.person = owners[0] if owners else ""
    if owners:
        person = session.get(BoardPerson, owners[0])
        if person is not None:
            card.emoji = person.emoji


def _clean_owners(session: Session, owners: list | None, person: str | None) -> list[str]:
    if owners is None:
        if person is None:
            return []
        key = str(person).strip().lower()
        if not key:
            return []
        return [_require_person(session, key).id]
    found: list[str] = []
    for item in owners:
        key = str(item or "").strip().lower()
        if not key:
            continue
        pid = _require_person(session, key).id
        if pid not in found:
            found.append(pid)
    return found


def _clean_hours(hours: float | int | None) -> tuple[int, float]:
    if hours is None or hours == "":
        return 0, 0.0
    try:
        number = float(hours)
    except (TypeError, ValueError) as exc:
        raise BoardError("Hours must be a number, or blank when the card is not estimated.") from exc
    if number < 0 or number > 1000:
        raise BoardError("Hours must be between 0 and 1000.")
    return 1, number


def _clean_value(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BoardError("Value must be a whole number from 1 to 5.") from exc
    if number < 1 or number > 5:
        raise BoardError("Value must be from 1 to 5.")
    return number


def _clip(value: object, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "empty"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _people_label(session: Session, owners: list[str]) -> str:
    if not owners:
        return "nobody"
    return ", ".join(_person_name(session, pid) for pid in owners)


def _check_reason(reason: str, *, required: bool) -> str:
    why = " ".join((reason or "").split())
    if required and not why:
        raise BoardError("A move back to an earlier column needs a reason.")
    if len(why) > 500:
        raise BoardError("The reason must be 500 characters or fewer.")
    return why


def _normalise_commit(repo: str, sha: str, summary: str = "") -> dict:
    name = (repo or "").strip()
    digest = (sha or "").strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", name):
        raise BoardError("Repo must be a short name, such as hackathon-site.")
    if not re.fullmatch(r"[0-9a-f]{7,40}", digest):
        raise BoardError("Commit must be a git sha of 7 to 40 hex characters.")
    line = " ".join((summary or "").split())
    if len(line) > 200:
        raise BoardError("Commit summary is too long.")
    return {"repo": name, "sha": digest, "summary": line}


def _commit_label(change: dict) -> str:
    label = f"{change['repo']}@{change['sha'][:7]}"
    if change.get("summary"):
        label += f" ({change['summary']})"
    return label


def _clean_tag(tag: str, kind: str) -> tuple[str, str]:
    text = (tag or "").strip()
    if len(text) > 120:
        raise BoardError("Tag is too long.")
    kind = (kind or "").strip().lower()
    if kind not in {"", "ok", "wait"}:
        raise BoardError("Tag kind must be ok or wait.")
    if text and not kind:
        kind = "wait"
    if not text:
        kind = ""
    return text, kind


def create_card(
    session: Session | None,
    *,
    title: str,
    by: str,
    person: str = "",
    hours: float | None = None,
    column: str = "todo",
    brief: str = "",
    tag: str = "",
    tag_kind: str = "",
    owners: list | None = None,
    value: int | None = 3,
    card_id: str | None = None,
    commits: list | None = None,
) -> dict:
    session, own = _own_session(session)
    try:
        actor = _require_person(session, by)
        column_index(column)
        names = _clean_owners(session, owners, person if owners is None else None)
        text = _clean_title(title)
        tag_text, kind = _clean_tag(tag, tag_kind)
        if column == "done" and not kind:
            kind = "ok" if tag_text else kind
        known, hours_value = _clean_hours(hours)
        chosen = card_id.strip() if isinstance(card_id, str) else ""
        if chosen:
            chosen = norm_id(chosen)
            if session.get(BoardCard, chosen) is not None:
                raise BoardError(f"Card {chosen} already exists.")
        else:
            chosen = _next_id(session)
        stored_commits = []
        for item in commits or []:
            if not isinstance(item, dict):
                continue
            stored_commits.append(
                _normalise_commit(str(item.get("repo") or ""), str(item.get("sha") or ""), str(item.get("summary") or ""))
            )
        now = utc_now()
        card = BoardCard(
            id=chosen,
            hours=hours_value,
            hours_known=known,
            title=text,
            column=column,
            tag=tag_text,
            tag_kind=kind,
            brief=_clean_brief(brief, text),
            value=_clean_value(value),
            commits=json.dumps(stored_commits) if stored_commits else "",
            rank=_next_rank(session, column),
            created_at=now,
            updated_at=now,
        )
        _store_owners(session, card, names)
        session.add(card)
        session.flush()
        _event(
            session,
            card,
            actor.id,
            "add",
            f"Added to {COLUMN_LABELS[column]}.",
            to_column=column,
        )
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def update_card(
    session: Session | None,
    card_id: str,
    *,
    by: str,
    fields: dict,
) -> dict:
    """Apply the keys the caller actually sent. ``hours: null`` clears the estimate."""
    session, own = _own_session(session)
    try:
        actor = _require_person(session, by)
        card = _find_card(session, card_id)
        parts: list[str] = []
        if "title" in fields:
            new_title = _clean_title(str(fields.get("title") or ""))
            if new_title != card.title:
                parts.append(f"title from {_clip(card.title)} to {_clip(new_title)}")
                card.title = new_title
        if "owners" in fields or "person" in fields:
            if "owners" in fields:
                names = _clean_owners(session, list(fields.get("owners") or []), None)
            else:
                names = _clean_owners(session, None, str(fields.get("person") or ""))
            current = _owners_of(card)
            if names != current:
                parts.append(
                    f"assignees from {_people_label(session, current)} to {_people_label(session, names)}"
                )
                _store_owners(session, card, names)
        if "hours" in fields:
            known, number = _clean_hours(fields.get("hours"))
            was = None if not int(card.hours_known or 0) else hours_out(card.hours)
            now_hours = None if not known else hours_out(number)
            if was != now_hours:
                parts.append(f"hours from {_clip(was)} to {_clip(now_hours)}")
                card.hours_known = known
                card.hours = number
        if "brief" in fields:
            new_brief = _clean_brief(str(fields.get("brief") or ""), card.title)
            if new_brief != (card.brief or ""):
                parts.append("brief")
                card.brief = new_brief
        if "tag" in fields or "tag_kind" in fields:
            new_tag, new_kind = _clean_tag(
                card.tag if "tag" not in fields else str(fields.get("tag") or ""),
                card.tag_kind if "tag_kind" not in fields else str(fields.get("tag_kind") or ""),
            )
            if new_tag != (card.tag or "") or new_kind != (card.tag_kind or ""):
                parts.append(f"tag from {_clip(card.tag)} to {_clip(new_tag)}")
                card.tag = new_tag
                card.tag_kind = new_kind
        if "value" in fields:
            new_value = _clean_value(fields.get("value"))
            if new_value != card.value:
                parts.append(f"value from {_clip(card.value)} to {_clip(new_value)}")
                card.value = new_value
        destination = ""
        if "column" in fields and fields.get("column"):
            destination = str(fields.get("column"))
            column_index(destination)
            if destination == card.column:
                destination = ""
        if not parts and not destination:
            raise BoardError("Nothing to change.")
        why = _check_reason(str(fields.get("reason") or ""), required=False)
        if destination:
            previous = card.column
            backward = move_is_backward(previous, destination)
            card.column = destination
            card.rank = _next_rank(session, destination)
            if destination == "done" and card.tag and not card.tag_kind:
                card.tag_kind = "ok"
            verb = "Moved back" if backward else "Moved"
            detail = f"{verb} from {COLUMN_LABELS[previous]} to {COLUMN_LABELS[destination]}."
            if parts:
                detail += " Edited " + "; ".join(parts) + "."
            card.updated_at = utc_now()
            _event(
                session,
                card,
                actor.id,
                "move" if not parts else "edit",
                detail,
                from_column=previous,
                to_column=destination,
                reason=why,
            )
        else:
            only_people = len(parts) == 1 and parts[0].startswith("assignees ")
            card.updated_at = utc_now()
            if only_people:
                detail = parts[0][:1].upper() + parts[0][1:] + "."
            else:
                detail = "Edited " + "; ".join(parts) + "."
            _event(
                session,
                card,
                actor.id,
                "assignees" if only_people else "edit",
                detail,
            )
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def move_card(
    session: Session | None,
    card_id: str,
    *,
    column: str,
    by: str,
    reason: str = "",
) -> dict:
    session, own = _own_session(session)
    try:
        actor = _require_person(session, by)
        card = _find_card(session, card_id)
        column_index(column)
        if card.column == column:
            return _finish(session, own, board_payload(session))
        backward = move_is_backward(card.column, column)
        why = _check_reason(reason, required=False)
        previous = card.column
        card.column = column
        card.rank = _next_rank(session, column)
        card.updated_at = utc_now()
        if column == "done" and card.tag and not card.tag_kind:
            card.tag_kind = "ok"
        verb = "Moved back" if backward else "Moved"
        _event(
            session,
            card,
            actor.id,
            "move",
            f"{verb} from {COLUMN_LABELS[previous]} to {COLUMN_LABELS[column]}.",
            from_column=previous,
            to_column=column,
            reason=why,
        )
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def reorder_cards(
    session: Session | None,
    *,
    column: str,
    ids: list[str],
    by: str,
) -> dict:
    session, own = _own_session(session)
    try:
        _require_person(session, by)
        column_index(column)
        rows = list(session.scalars(select(BoardCard).where(BoardCard.column == column)).all())
        have = {row.id for row in rows}
        want = []
        seen = set()
        for raw in ids:
            card = _find_card(session, raw)
            if card.id in seen:
                raise BoardError("Reorder listed a card twice.")
            seen.add(card.id)
            want.append(card.id)
        if set(want) != have:
            raise BoardError("Reorder must list every card in that column once.")
        for rank, card_id in enumerate(want):
            card = session.get(BoardCard, card_id)
            if card is None:
                continue
            card.rank = rank
            card.updated_at = utc_now()
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def duplicate_card(session: Session | None, card_id: str, *, by: str) -> dict:
    session, own = _own_session(session)
    try:
        actor = _require_person(session, by)
        source = _find_card(session, card_id)
        now = utc_now()
        title = source.title
        suffix = " (copy)"
        if not title.endswith(suffix):
            title = (title + suffix)[:240]
        card = BoardCard(
            id=_next_id(session),
            person=source.person,
            owners=source.owners or "",
            hours=source.hours,
            hours_known=source.hours_known,
            emoji=source.emoji,
            title=title,
            column=source.column,
            tag=source.tag,
            tag_kind=source.tag_kind,
            brief=source.brief,
            value=source.value,
            rank=_next_rank(session, source.column),
            created_at=now,
            updated_at=now,
        )
        session.add(card)
        session.flush()
        _event(session, card, actor.id, "duplicate", f"Copied from {source.id}.")
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def delete_card(session: Session | None, card_id: str, *, by: str) -> dict:
    session, own = _own_session(session)
    try:
        _require_person(session, by)
        card = _find_card(session, card_id)
        for event in session.scalars(select(BoardEvent).where(BoardEvent.card_id == card.id)).all():
            session.delete(event)
        session.delete(card)
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def add_commit(
    session: Session | None,
    card_id: str,
    *,
    by: str,
    repo: str,
    sha: str,
    summary: str = "",
) -> dict:
    session, own = _own_session(session)
    try:
        actor = _require_person(session, by)
        card = _find_card(session, card_id)
        change = _normalise_commit(repo, sha, summary)
        current = _commits_of(card)
        if any(item["repo"] == change["repo"] and item["sha"] == change["sha"] for item in current):
            raise BoardError("That commit is already on this card.")
        current.append(change)
        card.commits = json.dumps(current)
        card.updated_at = utc_now()
        _event(session, card, actor.id, "commit", f"Recorded commit {_commit_label(change)}.")
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def remove_commit(
    session: Session | None,
    card_id: str,
    *,
    by: str,
    repo: str,
    sha: str,
) -> dict:
    session, own = _own_session(session)
    try:
        actor = _require_person(session, by)
        card = _find_card(session, card_id)
        change = _normalise_commit(repo, sha)
        current = _commits_of(card)
        kept = [
            item for item in current
            if not (item["repo"] == change["repo"] and item["sha"] == change["sha"])
        ]
        if len(kept) == len(current):
            raise BoardError("That commit is not on this card.")
        removed = next(
            item for item in current
            if item["repo"] == change["repo"] and item["sha"] == change["sha"]
        )
        card.commits = json.dumps(kept) if kept else ""
        card.updated_at = utc_now()
        _event(session, card, actor.id, "commit", f"Removed commit {_commit_label(removed)}.")
        return _finish(session, own, board_payload(session))
    except Exception:
        session.rollback()
        if own:
            session.close()
        raise


def ensure_board_schema() -> None:
    """Add board columns to a database created before assignees, value, and commits."""
    from sqlalchemy import inspect, text

    from .database import engine

    insp = inspect(engine)
    if "board_cards" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("board_cards")}
    statements = []
    if "owners" not in cols:
        statements.append("ALTER TABLE board_cards ADD COLUMN owners TEXT NOT NULL DEFAULT ''")
    if "hours_known" not in cols:
        statements.append("ALTER TABLE board_cards ADD COLUMN hours_known INTEGER NOT NULL DEFAULT 1")
    if "value" not in cols:
        statements.append("ALTER TABLE board_cards ADD COLUMN value INTEGER")
    if "commits" not in cols:
        statements.append("ALTER TABLE board_cards ADD COLUMN commits TEXT NOT NULL DEFAULT ''")
    if not statements:
        return
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))
