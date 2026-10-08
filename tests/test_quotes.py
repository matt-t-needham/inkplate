from datetime import date

import pytest

import datasources
import quotes

TODAY = date(2026, 10, 8)

TWAIN = """\
'''Mark Twain''' was an American writer.

== Quotes ==
=== Following the Equator (1897) ===
* Truth is stranger than [[fiction]], but it is because Fiction is obliged to stick to possibilities; Truth isn't.
** Chapter XV

== Sourced elsewhere ==
* An undated line with no year anywhere near it.
** Letter to a friend
* Whenever you find yourself on the side of the majority, it is time to pause and reflect.
** ''Notebook'', 1904

== Disputed ==
* The reports of my death are greatly exaggerated.
** Often quoted; wording uncertain

== Misattributed ==
* Get your facts first, and then you can distort them as you please.
** Rudyard Kipling, ''From Sea to Sea'' (1899)
"""


def qotd_html(text, author="Mark Twain", page="Mark Twain"):
    return (f'<div class="mw-parser-output"><p>{text}</p>'
            f'<p>~ <a href="/wiki/{page.replace(" ", "_")}" title="{page}">{author}</a> ~</p></div>')


@pytest.fixture()
def wiki(data_dir, monkeypatch):
    """Fake Wikiquote API. Fill `pages` with QOTD dates → HTML and author
    titles → wikitext; anything else is a missing page."""
    pages, calls = {}, []

    def fake_get_json(url, params=None, **k):
        assert url == quotes.API
        title, prop = params["page"], params["prop"]
        calls.append(title)
        body = pages.get(title)
        if body is None:
            return {"error": {"code": "missingtitle"}}
        return {"parse": {prop: body}}

    monkeypatch.setattr(datasources, "_get_json", fake_get_json)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: pytest.fail("no bytes"))
    pages["Mark Twain"] = TWAIN
    return pages, calls


def test_today_qotd_with_work_and_year(wiki):
    pages, _ = wiki
    pages[quotes.qotd_title(TODAY)] = qotd_html(
        "Truth is stranger than fiction, but it is because Fiction is obliged "
        "to stick to possibilities; Truth isn't.")
    q = quotes.get_quote(TODAY)
    assert q["author"] == "Mark Twain"
    assert q["work"] == "Following the Equator"     # from the section heading
    assert q["year"] == "1897"
    assert q["qotd_date"] == TODAY.isoformat()
    assert quotes.cached_quote() == q


def test_italic_work_and_year_from_citation(wiki):
    pages, _ = wiki
    pages[quotes.qotd_title(TODAY)] = qotd_html(
        "Whenever you find yourself on the side of the majority, it is time to pause and reflect.")
    q = quotes.get_quote(TODAY)
    assert (q["work"], q["year"]) == ("Notebook", "1904")


def test_same_day_is_cached_no_refetch(wiki):
    pages, calls = wiki
    pages[quotes.qotd_title(TODAY)] = qotd_html(
        "Whenever you find yourself on the side of the majority, it is time to pause and reflect.")
    first = quotes.get_quote(TODAY)
    n = len(calls)
    assert quotes.get_quote(TODAY) == first
    assert len(calls) == n


@pytest.mark.parametrize("text", [
    "The reports of my death are greatly exaggerated.",                       # Disputed
    "Get your facts first, and then you can distort them as you please.",     # Misattributed
    "An undated line with no year anywhere near it.",                        # no year
    "A line that isn't on the author's page at all.",                        # unsourced
])
def test_unreliable_quotes_are_never_shown(wiki, text):
    pages, _ = wiki
    pages[quotes.qotd_title(TODAY)] = qotd_html(text)
    assert quotes.get_quote(TODAY) is None   # and nothing else is available


def test_too_long_falls_back_to_archive_before_cutoff(wiki):
    pages, calls = wiki
    pages[quotes.qotd_title(TODAY)] = qotd_html("Far too long. " * 50)
    # Make every archive date serve a good, short quote.
    good = qotd_html("Whenever you find yourself on the side of the majority, "
                     "it is time to pause and reflect.")
    for d in list(quotes._candidates(TODAY))[1:]:
        pages[quotes.qotd_title(d)] = good
    q = quotes.get_quote(TODAY, fits=lambda t: len(t) < 200)
    assert q is not None
    assert date.fromisoformat(q["qotd_date"]) < quotes.NO_REPEAT_BEFORE
    assert calls[0] == quotes.qotd_title(TODAY)


def test_fallback_dates_are_seeded_and_before_cutoff():
    a = list(quotes._candidates(TODAY))
    assert a == list(quotes._candidates(TODAY))      # re-render picks the same
    assert a[0] == TODAY
    assert all(quotes.ARCHIVE_START <= d < quotes.NO_REPEAT_BEFORE for d in a[1:])
    assert len(a) == quotes.MAX_TRIES


def test_vetoed_and_already_shown_are_skipped(wiki):
    pages, _ = wiki
    text = "Whenever you find yourself on the side of the majority, it is time to pause and reflect."
    pages[quotes.qotd_title(TODAY)] = qotd_html(text)
    assert quotes.get_quote(TODAY, vetoed=[text]) is None
    # Shown once on one day → never again on another.
    day2 = date(2026, 10, 9)
    pages[quotes.qotd_title(day2)] = qotd_html(text)
    assert quotes.get_quote(day2)["text"] == text
    day3 = date(2026, 10, 10)
    pages[quotes.qotd_title(day3)] = qotd_html(text)
    assert quotes.get_quote(day3)["text"] == text   # stale beats nothing...
    assert quotes.cached_quote()["qotd_date"] == day2.isoformat()  # ...but no new pick


def test_veto_drops_current_and_repicks(wiki):
    pages, _ = wiki
    text = "Whenever you find yourself on the side of the majority, it is time to pause and reflect."
    pages[quotes.qotd_title(TODAY)] = qotd_html(text)
    assert quotes.get_quote(TODAY)["text"] == text
    assert quotes.get_quote(TODAY, vetoed=[text.upper()]) is None   # key ignores case


def test_failed_day_backs_off(wiki):
    pages, calls = wiki
    assert quotes.get_quote(TODAY) is None
    n = len(calls)
    assert quotes.get_quote(TODAY) is None
    assert len(calls) == n   # no second sweep within the backoff window


def test_bc_years_and_markup_cleanup():
    assert quotes._year_in("Analects, c. 500 BC") == "500 BC"
    assert quotes._wiki_text("''[[Hamlet|The Tragedy]]'' {{cite}}<ref>x</ref>") == "The Tragedy"
