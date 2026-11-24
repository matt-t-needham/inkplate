"""The planner service's export, read for the planner screens (DECISIONS §31).

The planner writes `inkplate.json` (schema_version 1) atomically, hourly and
on every edit; config.PLANNER_EXPORT names it. This module validates it and
hands the screens the shapes they draw:

- get_planner(today): the daily screen's {"events", "todos", ...} (the shape
  planner_stub.get_planner documents). Todos are real; events are still the
  stub's, because the export's calendar is empty until the calendar feed
  exists, so the page says "Example events".
- get_bingo(today): the card the planner's `bingo_card` names.
- view(): which planner screen the export asks for ("daily" or "bingo").
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

import config
import planner_stub
from datasources import REPEATED_FAILURES

log = logging.getLogger("inkplate.planner")

SCHEMA_VERSION = 1
VIEWS = ("daily", "bingo")
# The planner's owners → the screens' owner tags.
OWNER_TAGS = {"matt": "m", "kass": "k", "together": "mk"}
GRID = 4

_failures = 0   # renders in a row that couldn't use the export


def _timestamp(v, what: str) -> datetime:
    """An ISO timestamp with an offset ("Z" or "+00:00"), as an aware datetime."""
    if not isinstance(v, str):
        raise ValueError(f"{what} is not a timestamp string")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{what} is not an ISO timestamp") from None
    if dt.tzinfo is None:
        raise ValueError(f"{what} has no UTC offset")
    return dt


def _string(v, what: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{what} is not a string")
    return v


def _owner(v, what: str) -> str:
    """A known owner key. Type-checked first: an unhashable value would
    raise TypeError on the membership test, escaping the fallback."""
    if not isinstance(v, str) or v not in OWNER_TAGS:
        raise ValueError(f"{what} is not a known owner")
    return v


def _validate(data) -> dict:
    """The export if it matches schema_version 1, else ValueError naming the
    field (never its contents: todo text is personal). Every shape is
    checked before it is used, so any malformed export is a ValueError."""
    if not isinstance(data, dict):
        raise ValueError("export is not an object")
    v = data.get("schema_version")
    if type(v) is not int or v != SCHEMA_VERSION:
        raise ValueError(f"schema_version is not {SCHEMA_VERSION}")
    _timestamp(data.get("generated_at"), "generated_at")
    if not isinstance(data.get("view"), str) or data["view"] not in VIEWS:
        raise ValueError("view is not daily or bingo")
    _owner(data.get("bingo_card"), "bingo_card")
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise ValueError("cards is not a list")
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("a card is not an object")
        _owner(card.get("id"), "a card's id")
        _string(card.get("title"), "a card's title")
        squares = card.get("squares")
        if not isinstance(squares, list):
            raise ValueError("a card's squares is not a list")
        cells = set()
        for sq in squares:
            if not isinstance(sq, dict):
                raise ValueError("a square is not an object")
            r, c = sq.get("row"), sq.get("col")
            if type(r) is not int or type(c) is not int or not (0 <= r < GRID and 0 <= c < GRID):
                raise ValueError("a square is off the 4x4 grid")
            _string(sq.get("summary"), "a square's summary")
            if type(sq.get("done")) is not bool:
                raise ValueError("a square's done is not true or false")
            cells.add((r, c))
        if len(cells) != GRID * GRID or len(squares) != GRID * GRID:
            raise ValueError("a card does not have one square per cell of 4x4")
    if data["bingo_card"] not in {c["id"] for c in cards}:
        raise ValueError("bingo_card names no card in cards")
    next_up = data.get("next_up")
    if not isinstance(next_up, dict):
        raise ValueError("next_up is not an object")
    for owner in OWNER_TAGS:
        item = next_up.get(owner)
        if item is not None:
            if not isinstance(item, dict):
                raise ValueError(f"next_up.{owner} is not an object")
            _string(item.get("text"), f"next_up.{owner}.text")
    completed = data.get("completed")
    if not isinstance(completed, list):
        raise ValueError("completed is not a list")
    for item in completed:
        if not isinstance(item, dict):
            raise ValueError("a completed item is not an object")
        _owner(item.get("owner"), "a completed item's owner")
        _string(item.get("text"), "a completed item's text")
        _timestamp(item.get("done_at"), "a completed item's done_at")
    return data


def _read():
    """The validated export, or raises OSError/ValueError (a bad file, bad
    UTF-8 and bad JSON are all one of those). None when no export is
    configured."""
    if config.PLANNER_EXPORT is None:
        return None
    return _validate(json.loads(config.PLANNER_EXPORT.read_text(encoding="utf-8")))


def _load() -> dict | None:
    """The export for a render, logging a failure (WARNING, ERROR from
    REPEATED_FAILURES in a row) and a recovery after a reported run. None:
    draw the example data. A render never fails because of the planner."""
    global _failures
    try:
        data = _read()
    except (OSError, ValueError) as e:
        _failures += 1
        level = logging.ERROR if _failures >= REPEATED_FAILURES else logging.WARNING
        run = f" ({_failures} in a row)" if _failures > 1 else ""
        log.log(level, "planner export unusable%s, drawing example data: %s: %s",
                run, e.__class__.__name__, e)
        return None
    if data is not None:
        if _failures >= REPEATED_FAILURES:
            log.info("planner export recovered after %d in a row", _failures)
        _failures = 0
    return data


def view() -> str:
    """The planner screen the export asks for; "daily" without a usable one.
    Never raises, and is silent: the render that follows logs any problem."""
    try:
        data = _read()
    except (OSError, ValueError):
        return "daily"   # deliberate: the render logs it (see _load)
    return data["view"] if data else "daily"


def _stale(data: dict, today: date, now: datetime | None) -> str | None:
    """The offline marker's text when the export is too old, else None. Its
    time is the export's own, so the text is the same every render."""
    now = now or datetime.now(timezone.utc)
    made = _timestamp(data["generated_at"], "generated_at")
    if now - made < timedelta(seconds=config.PLANNER_STALE_S):
        return None
    local = made.astimezone()   # the container's TZ, DECISIONS §11
    when = f"{local:%H:%M}" if local.date() == today else f"{local.day} {local:%b} {local:%H:%M}"
    return f"Planner offline since {when}"


def get_planner(today: date, now: datetime | None = None) -> dict:
    """The daily screen's data. `stale` is the offline marker's text or None;
    `example` means it is all the stub's, `example_events` that only the
    events are."""
    data = _load()
    stub = planner_stub.get_planner(today)
    if data is None:
        return {**stub, "stale": None}
    open_todos = [{"owner": OWNER_TAGS[o], "title": data["next_up"][o]["text"]}
                  for o in OWNER_TAGS if data["next_up"].get(o)]
    done_todos = [{"owner": OWNER_TAGS[t["owner"]], "title": t["text"],
                   "date": _timestamp(t["done_at"], "done_at").astimezone().date().isoformat()}
                  for t in data["completed"]]
    return {"events": stub["events"], "todos": {"open": open_todos, "done": done_todos},
            "example": False, "example_events": True, "stale": _stale(data, today, now)}


def get_bingo(today: date, now: datetime | None = None) -> dict:
    """The bingo screen's card: {title, owner (tag), squares: 16 {row, col,
    summary, done} in reading order, example, stale}."""
    data = _load()
    if data is None:
        return {**planner_stub.get_bingo(), "stale": None}
    card = next(c for c in data["cards"] if c["id"] == data["bingo_card"])
    squares = sorted(({"row": s["row"], "col": s["col"], "summary": s["summary"],
                       "done": s["done"]} for s in card["squares"]),
                     key=lambda s: (s["row"], s["col"]))
    return {"title": card["title"], "owner": OWNER_TAGS[card["id"]], "squares": squares,
            "example": False, "stale": _stale(data, today, now)}
