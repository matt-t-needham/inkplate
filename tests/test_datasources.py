import time
from datetime import date, timedelta

import pytest

import datasources
import questions


@pytest.fixture()
def no_network(monkeypatch):
    """Any un-stubbed HTTP call in these tests is a bug — fail loudly."""
    def boom(*a, **k):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(datasources, "_get_json", boom)
    monkeypatch.setattr(datasources, "_get_bytes", boom)


OPEN_METEO_RAW = {
    "current": {"temperature_2m": 26.8, "weather_code": 0},
    "daily": {
        "time": ["2026-09-17", "2026-09-18", "2026-09-19"],
        "weather_code": [0, 61, 3],
        "temperature_2m_max": [28.1, 24.1, 26.4],
        "temperature_2m_min": [11.5, 11.8, 10.6],
        "sunrise": ["2026-09-17T06:53", "2026-09-18T06:54", "2026-09-19T06:55"],
        "sunset": ["2026-09-17T19:22", "2026-09-18T19:20", "2026-09-19T19:18"],
    },
}


def test_weather_parse_and_cache(data_dir, no_network, monkeypatch):
    calls = []

    def fake_json(url, params=None, timeout=15.0):
        calls.append(url)
        return OPEN_METEO_RAW

    monkeypatch.setattr(datasources, "_get_json", fake_json)
    w = datasources.get_weather(45.52, -122.68)
    assert w["current_c"] == 26.8
    assert len(w["days"]) == 3
    assert w["days"][0]["sunrise"] == "2026-09-17T06:53"

    # Second call inside TTL served from cache — no new HTTP call.
    w2 = datasources.get_weather(45.52, -122.68)
    assert w2 == w
    assert len(calls) == 1

    # A different location is a different cache key.
    datasources.get_weather(51.5, -0.1)
    assert len(calls) == 2


def test_weather_stale_on_error(data_dir, no_network, monkeypatch):
    good = {"ok": True}
    monkeypatch.setattr(datasources, "_cached_fetch",
                        datasources._cached_fetch)  # no-op, clarity
    datasources._cache_write("weather_45.520_-122.680.json",
                             {"fetched_at": time.time() - 99999, "data": good})

    def fail(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(datasources, "_get_json", fail)
    assert datasources.get_weather(45.52, -122.68) == good


def test_weather_none_when_never_fetched(data_dir, no_network, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(datasources, "_get_json", fail)
    assert datasources.get_weather(1.0, 2.0) is None


def test_icon_kind_mapping():
    assert datasources.icon_kind(0) == "sun"
    assert datasources.icon_kind(2) == "partly"
    assert datasources.icon_kind(61) == "rain"
    assert datasources.icon_kind(75) == "snow"
    assert datasources.icon_kind(95) == "storm"
    assert datasources.icon_kind(45) == "fog"
    assert datasources.icon_kind(12345) == "cloud"
    assert datasources.icon_kind(None) == "cloud"


COMMONS_RAW = {
    "query": {"pages": {
        "1": {"title": "File:Columba domestica - 1860 - Print - Iconographia Zoologica - UBA01.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/pigeon.jpg"}]},
        "2": {"title": "File:Vulpes vulpes - 1864 - Print - Iconographia Zoologica - UBA01.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/fox.jpg",
                             "extmetadata": {"Artist": {"value": "<b>A. Schouman</b>"}}}]},
    }},
}


def _pin(monkeypatch, day, entry, collections=None):
    """Pin the animal (and optionally the collections) for determinism.
    A one-entry list means any content slot resolves to this animal, so the
    fixtures stay valid whatever period_days/offset the test uses."""
    monkeypatch.setattr(datasources, "ANIMALS", [entry])
    monkeypatch.setattr(datasources, "COLLECTIONS",
                        collections or [datasources.COLLECTIONS[0]])


def test_animal_filters_to_genus_and_parses_fields(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("fox", "Vulpes", "the Northern Hemisphere", "mammal"))
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: COMMONS_RAW)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"jpegbytes")

    a = datasources.get_animal(day)
    assert a["common_name"] == "fox"
    assert a["sci_name"] == "Vulpes vulpes"
    assert a["year"] == "1864"
    assert a["artist"] == "A. Schouman"  # HTML stripped, beats the collection default
    assert a["place"] == "Amsterdam"
    assert a["collection"] == "iz"
    assert a["native"] == "the Northern Hemisphere"
    assert a["for_date"] == day.isoformat()
    assert (data_dir / "cache" / "animal.jpg").read_bytes() == b"jpegbytes"


def test_animal_stale_fallback(data_dir, no_network, monkeypatch):
    old = {"path": str(data_dir / "cache" / "animal.jpg"),
           "caption": "Strix aluco · 1850", "common_name": "owl",
           "for_date": "2020-01-01"}
    (data_dir / "cache").mkdir(parents=True)
    (data_dir / "cache" / "animal.jpg").write_bytes(b"old")
    datasources._cache_write("animal.json", {"fetched_at": 0, "data": old})

    def fail(*a, **k):
        raise RuntimeError("commons down")
    monkeypatch.setattr(datasources, "_get_json", fail)
    assert datasources.get_animal(date(2026, 9, 17)) == old


def test_animal_none_when_nothing_cached(data_dir, no_network, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("commons down")
    monkeypatch.setattr(datasources, "_get_json", fail)
    assert datasources.get_animal(date(2026, 9, 17)) is None


def test_now_playing_parse(data_dir, no_network, monkeypatch):
    raw = {"artist": "Magic Dirt", "song": "Watch Out Boys", "album": "Tough Love",
           "year": "2003", "channel_name": "RAGE", "position": 145}
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    np = datasources.get_now_playing()
    assert np == {"artist": "Magic Dirt", "song": "Watch Out Boys",
                  "year": "2003", "channel_name": "RAGE"}


def test_now_playing_none_when_down(data_dir, no_network, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("rage down")
    monkeypatch.setattr(datasources, "_get_json", fail)
    assert datasources.get_now_playing() is None


def test_animal_skips_anatomical_plates(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("sloth", "Bradypus", "Central & South America", "mammal"))
    raw = {"query": {"pages": {
        "1": {"title": "File:Bradypus spec. - ingewanden - 1700-1880 - Print - UBA01.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/guts.jpg"}]},
        "2": {"title": "File:Bradypus tridactylus - 1820 - Print - UBA01.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/sloth.jpg"}]},
    }}}
    fetched = []
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: fetched.append(url) or b"img")
    a = datasources.get_animal(day)
    assert a["sci_name"] == "Bradypus tridactylus"
    assert fetched == ["https://thumb.example/sloth.jpg"]


def test_animal_falls_through_collections(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    colls = [
        {"key": "empty", "query": "EMPTYQ {latin}", "artist": "Nobody",
         "place": "Nowhere", "years": "1600", "kinds": None},
        {"key": "gessner", "query": "GESSNERQ {latin}", "artist": "Conrad Gessner",
         "place": "Zürich", "years": "1551-1558", "kinds": None},
    ]
    _pin(monkeypatch, day, ("rhinoceros", "Rhinoceros", "South Asia", "mammal"),
         collections=colls)

    def fake_json(url, params=None, timeout=15.0):
        if "EMPTYQ" in params["gsrsearch"]:
            return {"query": {"pages": {}}}
        return {"query": {"pages": {
            "1": {"title": "File:Historiae animalium Rhinoceros woodcut.jpg",
                  "imageinfo": [{"thumburl": "https://thumb.example/rhino.jpg"}]},
        }}}

    monkeypatch.setattr(datasources, "_get_json", fake_json)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"rhino")
    # Rotation may start at either collection; the empty one can never win.
    a = datasources.get_animal(day)
    assert a["collection"] == "gessner"
    assert a["artist"] == "Conrad Gessner"
    assert a["place"] == "Zürich"
    assert a["year"] == "1551-1558"  # no year in filename -> collection era
    assert a["sci_name"] == "Rhinoceros"


def test_birds_only_collections_excluded_for_mammals(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    colls = [
        {"key": "birds", "query": "BIRDQ {common}", "artist": "A", "place": "B",
         "years": "1800", "kinds": {"bird"}},
        {"key": "any", "query": "ANYQ {latin}", "artist": "C", "place": "D",
         "years": "1700", "kinds": None},
    ]
    _pin(monkeypatch, day, ("fox", "Vulpes", "the Northern Hemisphere", "mammal"),
         collections=colls)
    queries = []

    def fake_json(url, params=None, timeout=15.0):
        queries.append(params["gsrsearch"])
        return {"query": {"pages": {
            "1": {"title": "File:Vulpes vulpes 1820.jpg",
                  "imageinfo": [{"thumburl": "https://thumb.example/fox.jpg"}]}}}}

    monkeypatch.setattr(datasources, "_get_json", fake_json)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"fox")
    a = datasources.get_animal(day)
    assert a["collection"] == "any"
    assert all("BIRDQ" not in q for q in queries)


def test_two_word_latin_requires_full_binomial(data_dir, no_network, monkeypatch):
    """'Canis' alone would label a jackal as the wolf — a two-word Latin term
    must appear in full in the title, with no genus-only fallback."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("wolf", "Canis lupus", "Eurasia", "mammal"))
    raw = {"query": {"pages": {
        "1": {"title": "File:Canis aureus - 1830 - Print - Iconographia Zoologica.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/jackal.jpg"}]},
        "2": {"title": "File:Canis lupus - 1840 - Print - Iconographia Zoologica.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/wolf.jpg"}]},
    }}}
    fetched = []
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: fetched.append(url) or b"img")
    a = datasources.get_animal(day)
    assert a["sci_name"] == "Canis lupus"
    assert fetched == ["https://thumb.example/wolf.jpg"]  # never the jackal


def test_two_word_latin_no_genus_fallback(data_dir, no_network, monkeypatch):
    """With only a wrong-species plate available, the collection is skipped
    rather than mislabeled."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("wolf", "Canis lupus", "Eurasia", "mammal"))
    raw = {"query": {"pages": {
        "1": {"title": "File:Canis aureus - 1830 - Print.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/jackal.jpg"}]},
    }}}
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"img")
    assert datasources.get_animal(day) is None


def test_animal_list_is_well_formed():
    """Every entry: 4 fields, unique names, valid kind."""
    kinds = {"mammal", "bird", "reptile", "amphibian", "fish", "invert"}
    commons, latins = [], []
    for entry in datasources.ANIMALS:
        assert len(entry) == 4, entry
        common, latin, native, kind = entry
        assert kind in kinds, entry
        assert common and latin and native, entry
        commons.append(common)
        latins.append(latin)
    assert len(commons) == len(set(commons)), "duplicate common name"
    assert len(latins) == len(set(latins)), "duplicate latin term"
    assert len(datasources.ANIMALS) > 200


def test_animal_period_days_holds_steady(data_dir, no_network, monkeypatch):
    """With a 7-day cadence the same plate is served all week, from cache."""
    # Slots align to a fixed ordinal grid — start on a boundary so the window
    # is one whole slot.
    day = date(2026, 9, 17)
    day += timedelta(days=(7 - day.toordinal() % 7) % 7)
    _pin(monkeypatch, day, ("fox", "Vulpes", "the Northern Hemisphere", "mammal"))
    calls = []
    monkeypatch.setattr(datasources, "_get_json",
                        lambda *a, **k: calls.append(1) or COMMONS_RAW)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"img")

    first = datasources.get_animal(day, period_days=7)
    for i in range(1, 7):
        assert datasources.get_animal(day + timedelta(days=i), period_days=7) == first
    assert len(calls) == 1  # one fetch for the whole slot
    # The next slot does fetch again.
    datasources.get_animal(day + timedelta(days=7), period_days=7)
    assert len(calls) == 2


def test_animal_offset_changes_slot(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("fox", "Vulpes", "the Northern Hemisphere", "mammal"))
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: COMMONS_RAW)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"img")
    a = datasources.get_animal(day, offset=0, period_days=30)
    b = datasources.get_animal(day, offset=1, period_days=30)
    assert b["slot"] == a["slot"] + 1


def test_icon_kind_covers_the_richer_glyph_set():
    assert datasources.icon_kind(51) == "drizzle"
    assert datasources.icon_kind(56) == "sleet"     # freezing drizzle
    assert datasources.icon_kind(67) == "sleet"     # freezing rain
    assert datasources.icon_kind(81) == "showers"
    assert datasources.icon_kind(96) == "hail"      # thunderstorm with hail


def test_every_wmo_kind_has_a_glyph():
    """A code mapped to a kind the icon font doesn't know would silently
    render as a generic cloud."""
    import screens
    for code, kind in datasources.WMO_KINDS.items():
        assert kind in screens.WI_GLYPHS, (code, kind)


def test_prefers_a_plate_scan_over_a_photograph_of_one(data_dir, no_network, monkeypatch):
    """Both are usable, but a scan of the plate beats someone's photo of it."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("toucan", "Ramphastos", "South America", "bird"),
         collections=[{"key": "gould", "query": "GOULD {common}",
                       "artist": "John Gould", "place": "London",
                       "years": "1840-1881", "kinds": {"bird"}}])
    raw = {"query": {"pages": {
        "1": {"title": "File:Ramphastos swainsonii (chestnut-mandibled toucan).jpg",
              "imageinfo": [{"thumburl": "https://thumb.example/photo.jpg",
                             "extmetadata": {
                                 "Artist": {"value": "James St. John"},
                                 "DateTimeOriginal": {"value": "2011-06-10 12:29:20"}}}]},
        "2": {"title": "File:Ramphastos brevis Gould.jpg",
              "imageinfo": [{"thumburl": "https://thumb.example/plate.jpg",
                             "extmetadata": {"Artist": {"value": "John Gould"}}}]},
    }}}
    fetched = []
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: fetched.append(url) or b"img")
    a = datasources.get_animal(day)
    assert fetched == ["https://thumb.example/plate.jpg"]
    assert a["artist"] == "John Gould"      # never the 2011 photographer


def test_year_falls_back_to_file_date_then_collection(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    coll = {"key": "gould", "query": "GOULD {common}", "artist": "John Gould",
            "place": "London", "years": "1840-1881", "kinds": {"bird"}}
    _pin(monkeypatch, day, ("toucan", "Ramphastos", "South America", "bird"),
         collections=[coll])
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"img")

    # No year in the title, but the file records an 1835 original date.
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: {"query": {"pages": {
        "1": {"title": "File:Toco Toucan by Ramphastos.JPG",
              "imageinfo": [{"thumburl": "u", "extmetadata": {
                  "DateTimeOriginal": {"value": "1835"}}}]}}}})
    assert datasources.get_animal(day)["year"] == "1835"

    # Nothing at all -> the collection's era.
    (data_dir / "cache" / "animal.json").unlink()
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: {"query": {"pages": {
        "1": {"title": "File:Ramphastos plate.jpg",
              "imageinfo": [{"thumburl": "u"}]}}}})
    assert datasources.get_animal(day)["year"] == "1840-1881"


def test_photograph_is_used_but_credits_the_original_artist(data_dir, no_network,
                                                            monkeypatch):
    """When the only file is a modern photo of the plate, use it — but the
    caption must credit whoever drew it, not whoever photographed it, and
    must not date the work to the day of the photo."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("toucan", "Ramphastos", "South America", "bird"),
         collections=[{"key": "gould", "query": "GOULD {common}",
                       "artist": "John Gould", "place": "London",
                       "years": "1840-1881", "kinds": {"bird"}}])
    raw = {"query": {"pages": {
        "1": {"title": "File:Ramphastos swainsonii (chestnut-mandibled toucan).jpg",
              "imageinfo": [{"thumburl": "https://thumb.example/photo.jpg",
                             "extmetadata": {
                                 "Artist": {"value": "James St. John"},
                                 "DateTimeOriginal": {"value": "2011-06-10 12:29:20"}}}]},
    }}}
    fetched = []
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: fetched.append(url) or b"img")
    a = datasources.get_animal(day)
    assert fetched == ["https://thumb.example/photo.jpg"]   # photo is fine
    assert a["artist"] == "John Gould"                      # original credited
    assert a["year"] == "1840-1881"                         # not 2011


def test_clean_artist_handles_commons_quirks():
    # Commons repeats the field when markup nests; and a lot of plates are
    # credited to nobody in particular.
    assert datasources._clean_artist("Unknown author Unknown author") == ""
    assert datasources._clean_artist("Unknown author") == ""
    assert datasources._clean_artist("anonymous") == ""
    assert datasources._clean_artist("John Gould John Gould") == "John Gould"
    assert datasources._clean_artist("John Gould") == "John Gould"
    assert datasources._clean_artist("Elizabeth Gould and John Gould") == \
        "Elizabeth Gould and John Gould"   # not a duplicate, keep intact


def test_explicit_unknown_artist_prints_nothing(data_dir, no_network, monkeypatch):
    """A file that positively says the artist is unknown should drop the
    credit, not substitute a guess and not print the word "Unknown"."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("raven", "Corvus", "the Northern Hemisphere", "bird"),
         collections=[{"key": "gould", "query": "G {common}", "artist": "John Gould",
                       "place": "London", "years": "1840-1881", "kinds": {"bird"}}])
    raw = {"query": {"pages": {"1": {
        "title": "File:Corvus corax - 1850.jpg",
        "imageinfo": [{"thumburl": "u", "extmetadata": {
            "Artist": {"value": "<span>Unknown author</span><span>Unknown author</span>"}}}]}}}}
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"img")
    assert datasources.get_animal(day)["artist"] is None


def test_absent_artist_still_uses_the_collection(data_dir, no_network, monkeypatch):
    """No Artist field at all is different from one that says 'unknown': the
    corpus attribution is a real fact about where the plate came from."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("raven", "Corvus", "the Northern Hemisphere", "bird"),
         collections=[{"key": "gould", "query": "G {common}", "artist": "John Gould",
                       "place": "London", "years": "1840-1881", "kinds": {"bird"}}])
    raw = {"query": {"pages": {"1": {
        "title": "File:Corvus corax - 1850.jpg",
        "imageinfo": [{"thumburl": "u"}]}}}}
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: b"img")
    assert datasources.get_animal(day)["artist"] == "John Gould"
