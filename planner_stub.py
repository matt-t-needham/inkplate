"""EXAMPLE DATA, all made up, until the calendar feed and planner exist;
`example: True` makes the screen say so. See DECISIONS §26.
"""

from datetime import date, timedelta

import screens

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


def get_planner(today: date) -> dict:
    """{"events": [{date, time, all_day, owner, title}], "todos": {"open":
    [{owner, title}], "done": [{owner, title, date}]}, "example": True}.
    Dates are ISO strings; `time` is "HH:MM" or None (untimed or all day).

    The contract the real sources must meet: events are per-day occurrences
    in local time; the adapter expands multi-day and overnight events into
    one entry per day; end times are dropped."""
    week = screens._week(today)
    events = [{"date": week[wd].isoformat(),
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
