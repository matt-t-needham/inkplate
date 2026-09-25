from datetime import date, timedelta

import questions


def test_deterministic_for_a_date():
    d = date(2026, 9, 17)
    assert questions.question_for(d) == questions.question_for(d)


def test_changes_day_to_day():
    d = date(2026, 9, 17)
    texts = {questions.question_for(d + timedelta(days=i))["text"] for i in range(10)}
    assert len(texts) == 10


def test_no_repeats_within_a_full_cycle():
    n = len(questions.BANK)
    start = date(2026, 1, 1)
    # Align to the start of a cycle so the window covers exactly one permutation.
    start += timedelta(days=(n - start.toordinal() % n) % n)
    texts = [questions.question_for(start + timedelta(days=i))["text"] for i in range(n)]
    assert len(set(texts)) == n


def test_shape_and_categories():
    q = questions.question_for(date(2026, 9, 17))
    assert set(q) == {"category", "text"}
    cats = {c for c, _ in questions.BANK}
    assert cats == {"personal", "philosophy", "politics", "ethics"}
    assert all(len(t) > 15 for _, t in questions.BANK)


def test_offset_advances_the_sequence():
    d = date(2026, 9, 17)
    assert questions.question_for(d, 1) != questions.question_for(d, 0)
    assert questions.question_for(d, 1) == questions.question_for(d + timedelta(days=1), 0)


def test_period_days_holds_the_question_steady():
    # Slots align to a fixed ordinal grid, so start on a slot boundary.
    d = date(2026, 9, 17)
    d += timedelta(days=(7 - d.toordinal() % 7) % 7)
    weekly = [questions.question_for(d + timedelta(days=i), 0, 7)["text"] for i in range(7)]
    assert len(set(weekly)) == 1  # unchanged all week
    later = questions.question_for(d + timedelta(days=7), 0, 7)["text"]
    assert later not in weekly    # ...then moves on


def test_offset_still_cycles_under_a_long_period():
    d = date(2026, 9, 17)
    a = questions.question_for(d, 0, 30)
    b = questions.question_for(d, 1, 30)
    assert a != b  # the pane's button works regardless of cadence


def test_slot_for():
    d = date(2026, 9, 17)
    assert questions.slot_for(d, 1, 0) == d.toordinal()
    assert questions.slot_for(d, 7, 0) == d.toordinal() // 7
    assert questions.slot_for(d, 7, 2) == d.toordinal() // 7 + 2
    assert questions.slot_for(d, 0, 0) == d.toordinal()  # period 0 treated as 1


def test_categories_are_evenly_balanced():
    """No flavour should dominate the rotation."""
    from collections import Counter
    counts = Counter(c for c, _ in questions.BANK)
    assert set(counts) == {"personal", "philosophy", "politics", "ethics"}
    assert max(counts.values()) - min(counts.values()) <= 2, counts


def test_house_style_holds():
    """Seminar-prompt tone creeps back in if nobody checks for it."""
    banned = ("discuss", "defend your", "supererogatory", "decision procedure",
              "thought experiment:", "(computing)", "(medical)", "(global)")
    for _, text in questions.BANK:
        low = text.lower()
        for b in banned:
            assert b not in low, f"{b!r} in {text!r}"
        assert text.endswith(("?", ".")), text
        assert len(text) <= 150, text  # four wrapped lines on the panel
