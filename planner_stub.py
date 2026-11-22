"""EXAMPLE DATA for the daily screen, until its real sources exist.

Everything here is made up. The shared calendar feed and the planner service
the daily screen will read from don't exist yet, so this module stands in for
both, shaped like the data they will return. While it is in use the screen
draws an "Example data" footer (`example: True`), so nobody mistakes it for a
real week. See DECISIONS §26.

Events are generated relative to the current week, so the stub always shows
this week whatever the date.
"""

from datetime import date, timedelta

# (weekday 0 = Monday, "HH:MM" | None | "all day", owner, title)
_EVENTS = [
    (0, "07:00", "m", "Gym"),
    (0, "09:30", "m", "Stand-up"),
    (0, "12:30", "k", "Lunch with Priya"),
    (0, None, "mk", "Parcel collect"),
    (1, "08:00", "m", "Car service"),
    (1, "09:30", "k", "Dentist"),
    (1, "15:00", "k", "School run, then swimming with the twins"),
    (1, None, "mk", "Pick up dog food"),
    (2, "10:00", "m", "Team review"),
    (2, "19:00", "mk", "Dinner at Tom's"),
    (3, "11:00", "k", "Vet"),
    (3, "14:00", "mk", "Plumber"),
    (3, None, "m", "Pay council tax"),
    (4, "07:00", "mk", "Bins"),
    (4, "19:30", "k", "Film club"),
    (5, "all day", "mk", "Bin day"),
    (5, "11:00", "mk", "Birthday brunch at the harbour cafe"),
    (5, "20:00", "m", "Gig"),
    (6, "09:00", "mk", "Market"),
    (6, "13:00", "m", "Football"),
    (6, "19:00", "mk", "Dinner at Sam's"),
]

_OPEN = [
    ("m", "Book MOT"), ("k", "Call the vet"), ("mk", "Plan Christmas food"),
    ("m", "Renew car tax"), ("k", "Order contact lenses"),
    ("mk", "Book flights to Wales"),
]

# (days before today it was done, owner, title)
_DONE = [(1, "m", "Send invoice"), (2, "k", "Renew passport"),
         (3, "mk", "Fix shower door")]


def week_start(today: date) -> date:
    """The Monday of today's week: the calendar runs Monday to Sunday and
    moves on to the new week on Monday."""
    return today - timedelta(days=today.weekday())


def get_planner(today: date) -> dict:
    """{"events": [{date, time, all_day, owner, title}], "todos": {"open":
    [{owner, title}], "done": [{owner, title, date}]}, "example": True}.
    Dates are ISO strings; `time` is "HH:MM" or None (untimed or all day)."""
    monday = week_start(today)
    events = [{"date": (monday + timedelta(days=wd)).isoformat(),
               "time": None if t in (None, "all day") else t,
               "all_day": t == "all day", "owner": owner, "title": title}
              for wd, t, owner, title in _EVENTS]
    done = [{"owner": owner, "title": title,
             "date": (today - timedelta(days=ago)).isoformat()}
            for ago, owner, title in _DONE]
    return {"events": events,
            "todos": {"open": [{"owner": o, "title": t} for o, t in _OPEN],
                      "done": done},
            "example": True}
