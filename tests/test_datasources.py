import time
from datetime import date, timedelta

import pytest

import datasources


def _plate_bytes() -> bytes:
    """A small plate the faint-image check accepts: a figure on paper."""
    import io
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (300, 400), (240, 235, 220))
    ImageDraw.Draw(im).ellipse([80, 100, 220, 320], fill=(40, 35, 30))
    buf = io.BytesIO(); im.save(buf, "JPEG")
    return buf.getvalue()


PLATE = _plate_bytes()


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


def test_one_off_failure_is_quiet_repeated_ones_are_reported(data_dir, no_network,
                                                              monkeypatch, caplog):
    import logging
    import state
    good = {"ok": True}
    name = "weather_45.520_-122.680.json"
    datasources._cache_write(name, {"fetched_at": time.time() - 99999, "data": good})

    def down(*a, **k):
        raise RuntimeError("503 from upstream")
    monkeypatch.setattr(datasources, "_get_json", down)
    caplog.set_level(logging.INFO, logger="inkplate.datasources")

    def warnings():
        return [r for r in caplog.records if r.levelno >= logging.WARNING]

    for n in range(1, datasources.REPEATED_FAILURES):
        assert datasources.get_weather(45.52, -122.68) == good
        assert warnings() == [], f"failure {n} of a run should not warn"
        assert datasources._cache_read(name)["failures"] == n
    assert not [e for e in state.read_events() if e["kind"] == "error"]
    # bix-infra's log compiler flags any line matching its DEFAULT_FLAG; a blip
    # must not.
    import re
    flag = re.compile(r'\b(error|errors?:|warn(ing)?|fail(ed|ure)?|exception|crash(ed)?'
                      r'|fatal|panic|traceback|critical)\b', re.I)
    assert not any(flag.search(r.getMessage()) for r in caplog.records), \
        "a blip must not read as a failure to the log review"

    assert datasources.get_weather(45.52, -122.68) == good   # the run reaches the limit
    assert len(warnings()) == 1 and "in a row" in warnings()[0].getMessage()
    assert [e["kind"] for e in state.read_events()] == ["error"]


def test_failure_count_resets_on_success(data_dir, no_network, monkeypatch):
    name = "weather_45.520_-122.680.json"
    datasources._cache_write(name, {"fetched_at": time.time() - 99999, "data": {"old": 1},
                                    "failures": datasources.REPEATED_FAILURES})
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: OPEN_METEO_RAW)
    datasources.get_weather(45.52, -122.68)
    assert "failures" not in datasources._cache_read(name)


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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)

    a = datasources.get_animal(day)
    assert a["common_name"] == "fox"
    assert a["sci_name"] == "Vulpes vulpes"
    assert a["year"] == "1864"
    assert a["artist"] == "A. Schouman"  # HTML stripped, beats the collection default
    assert a["place"] == "Amsterdam"
    assert a["collection"] == "iz"
    assert a["native"] == "the Northern Hemisphere"
    assert a["for_date"] == day.isoformat()
    assert (data_dir / "cache" / "animal.jpg").read_bytes() == PLATE


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
                        lambda url, **k: fetched.append(url) or PLATE)
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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
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
                        lambda url, **k: fetched.append(url) or PLATE)
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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)

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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
    a = datasources.get_animal(day, offset=0, period_days=30)
    b = datasources.get_animal(day, offset=1, period_days=30)
    assert b["slot"] == a["slot"] + 1


def test_vetoed_image_is_replaced_by_another_plate(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("fox", "Vulpes", "the Northern Hemisphere", "mammal"))
    raw = {"query": {"pages": {
        "1": {"title": "File:Vulpes vulpes - 1864 - Print - Iconographia Zoologica - UBA01.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/fox1.jpg"}]},
        "2": {"title": "File:Vulpes vulpes - 1870 - Print - Iconographia Zoologica - UBA02.tif",
              "imageinfo": [{"thumburl": "https://thumb.example/fox2.jpg"}]},
    }}}
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)

    first = datasources.get_animal(day)
    assert first["file"].startswith("File:Vulpes vulpes")
    # Same slot, already cached — the veto must force a different image of
    # the same animal, not skip the animal.
    second = datasources.get_animal(day, vetoed=[first["file"]])
    assert second["common_name"] == "fox"
    assert second["file"] != first["file"]
    assert datasources.cached_animal()["file"] == second["file"]
    # Every plate vetoed: nothing to show, and the vetoed stale one isn't served.
    assert datasources.get_animal(day, vetoed=[first["file"], second["file"]]) is None


def test_cached_animal_without_file_is_refetched(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("fox", "Vulpes", "the Northern Hemisphere", "mammal"))
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: COMMONS_RAW)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
    a = datasources.get_animal(day)
    old = dict(a); old.pop("file")
    datasources._cache_write("animal.json", {"fetched_at": 0, "data": old})
    assert datasources.get_animal(day)["file"] == a["file"]

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
         collections=[{**datasources.COLLECTIONS[6]}])
    raw = {"query": {"pages": {
        "1": {"title": "File:Ramphastos swainsonii (chestnut-mandibled toucan).jpg",
              "imageinfo": [{"thumburl": "https://thumb.example/photo.jpg",
                             "extmetadata": {
                                 "Artist": {"value": "James St. John"},
                                 "ImageDescription": {"value": "Plate from John Gould's A Monograph of the Ramphastidae"},
                                 "DateTimeOriginal": {"value": "2011-06-10 12:29:20"}}}]},
        "2": {"title": "File:Ramphastos brevis Gould.jpg",
              "imageinfo": [{"thumburl": "https://thumb.example/plate.jpg",
                             "extmetadata": {"Artist": {"value": "John Gould"}}}]},
    }}}
    fetched = []
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: fetched.append(url) or PLATE)
    a = datasources.get_animal(day)
    assert fetched == ["https://thumb.example/plate.jpg"]
    assert a["artist"] == "John Gould"      # never the 2011 photographer


def test_year_falls_back_to_file_date_then_collection(data_dir, no_network, monkeypatch):
    day = date(2026, 9, 17)
    coll = {"key": "gould", "query": "GOULD {common}", "artist": "John Gould",
            "place": "London", "years": "1840-1881", "kinds": {"bird"}}
    _pin(monkeypatch, day, ("toucan", "Ramphastos", "South America", "bird"),
         collections=[coll])
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)

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
         collections=[{**datasources.COLLECTIONS[6]}])
    raw = {"query": {"pages": {
        "1": {"title": "File:Ramphastos swainsonii (chestnut-mandibled toucan).jpg",
              "imageinfo": [{"thumburl": "https://thumb.example/photo.jpg",
                             "extmetadata": {
                                 "Artist": {"value": "James St. John"},
                                 "ImageDescription": {"value": "Plate from John Gould's A Monograph of the Ramphastidae"},
                                 "DateTimeOriginal": {"value": "2011-06-10 12:29:20"}}}]},
    }}}
    fetched = []
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: fetched.append(url) or PLATE)
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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
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
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
    assert datasources.get_animal(day)["artist"] == "John Gould"


SNOW_RAW = {
    "current": {"time": "2026-12-15T10:00", "snow_depth": 1.524},
    "hourly": {
        # 4 days of hourly data from 2026-12-12T00:00; 1 cm every hour.
        "time": [f"2026-12-{12 + h // 24:02d}T{h % 24:02d}:00" for h in range(96)],
        "snowfall": [1.0] * 96,
    },
    "daily": {
        "time": ["2026-12-12", "2026-12-13", "2026-12-14", "2026-12-15",
                 "2026-12-16", "2026-12-17"],
        "snowfall_sum": [0, 0, 0, 12.0, None, 18.5],
        "weather_code": [3, 3, 3, 73, 3, 75],
    },
}


def test_snow_parse(data_dir, no_network, monkeypatch):
    seen = {}
    monkeypatch.setattr(datasources, "_get_json",
                        lambda url, params=None, **k: seen.update(params) or SNOW_RAW)
    s = datasources.get_snow(45.33, -121.66)
    assert seen["past_days"] == 3
    assert round(s["depth_cm"], 1) == 152.4      # metres -> cm
    assert s["recent_cm"] == 72                  # the 72 hours ending now
    assert [d["date"] for d in s["days"]] == ["2026-12-15", "2026-12-16", "2026-12-17"]
    assert s["days"][1]["snow_cm"] == 0          # null -> 0
    assert s["days"][2]["code"] == 75


def test_slot_for():
    d = date(2026, 9, 17)
    assert datasources.slot_for(d, 1, 0) == d.toordinal()
    assert datasources.slot_for(d, 7, 0) == d.toordinal() // 7
    assert datasources.slot_for(d, 7, 2) == d.toordinal() // 7 + 2
    assert datasources.slot_for(d, 0, 0) == d.toordinal()  # period 0 treated as 1


def test_genus_must_lead_not_trail(data_dir, no_network, monkeypatch):
    """'Papilio' is the butterflies — not the fish Pteraclis papilio, the
    mudskipper variety 'ε Papilio', the snail Strombus papilio, or a
    butterflyfish matched by the common name."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("butterfly", "Papilio", "every continent but Antarctica", "invert"))
    impostors = ["File:Pteraclis papilio - 1700-1880 - Print - Iconographia Zoologica.tif",
                 "File:Periophthalmus koelreuteri var. ε Papilio - 1700-1880 - Print.tif",
                 "File:Strombus papilio - - Print - Iconographia Zoologica.tif",
                 "File:Butterflyfish - 1880 - Print.tif"]
    real = "File:Papilio machaon - - Print - Iconographia Zoologica.tif"

    def raw(titles):
        return {"query": {"pages": {str(i): {"title": t, "imageinfo": [{"thumburl": "u"}]}
                                    for i, t in enumerate(titles)}}}
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw(impostors))
    assert datasources.get_animal(day) is None
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw(impostors + [real]))
    a = datasources.get_animal(day)
    assert a["file"] == real and a["sci_name"] == "Papilio machaon"


GOULD = {**datasources.COLLECTIONS[6]}


def _page(title, url, **meta):
    return {"title": title, "imageinfo": [{"thumburl": url, "extmetadata": {
        k: {"value": v} for k, v in meta.items()}}]}


def _serve(monkeypatch, *pages):
    fetched = []
    raw = {"query": {"pages": {str(i): p for i, p in enumerate(pages)}}}
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: fetched.append(url) or PLATE)
    return fetched


def test_file_outside_the_corpus_is_rejected(data_dir, no_network, monkeypatch):
    """Full-text search for "John Gould quail" returned a light aircraft and
    "John Gould dove" a 2023 wildlife photo; neither is a Gould plate, and a
    title naming the beast doesn't make it one."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("quail", "Coturnix", "Eurasia & Africa", "bird"),
         collections=[GOULD])
    fetched = _serve(
        monkeypatch,
        _page("File:Quail 4.jpg", "https://t/plane.jpg",
              ImageDescription="Aerosport Quail", Categories="Aerosport Quail"),
        _page("File:Quail in a sanctuary 2023.jpg", "https://t/photo.jpg",
              DateTimeOriginal="2023-04-18 13:45:50", Artist="Someone"),
        _page("File:Young Jay Gould and his quail.jpg", "https://t/financier.jpg",
              Categories="Jay Gould"))
    assert datasources.get_animal(day) is None
    assert fetched == []

    fetched = _serve(
        monkeypatch,
        _page("File:Quail 4.jpg", "https://t/plane.jpg",
              ImageDescription="Aerosport Quail"),
        _page("File:Coturnix coturnix (Common Quail).jpg", "https://t/plate.jpg",
              Categories="Lithographs by John Gould and Henry Constantine Richter"))
    assert datasources.get_animal(day)["file"] == "File:Coturnix coturnix (Common Quail).jpg"
    assert fetched == ["https://t/plate.jpg"]


def test_anatomy_in_the_description_is_rejected(data_dir, no_network, monkeypatch):
    """A skull on a plinth titled only "Hyrax Buffon 1789": the title is
    innocent, the description and categories are not."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("hyrax", "Hyrax", "Africa & Arabia", "mammal"),
         collections=[{**datasources.COLLECTIONS[2]}])
    fetched = _serve(
        monkeypatch,
        _page("File:Hyrax Buffon 1789.png", "https://t/skull.png",
              ImageDescription="Skull of Procavia capensis, in the Histoire naturelle",
              Categories="Procavia capensis skulls"))
    assert datasources.get_animal(day) is None
    assert fetched == []


def test_common_name_namesake_is_rejected_by_its_taxon(data_dir, no_network,
                                                       monkeypatch):
    """"The Turkey Buzzard" names a turkey and is filed under Cathartes aura.
    A common-name match whose taxa name another genus is a namesake — but a
    Latin match is trusted over the taxa, whose genus is often today's, not
    the period's (our colugo is Galeopithecus; Commons says Galeopterus)."""
    day = date(2026, 9, 17)
    catesby, iz = {**datasources.COLLECTIONS[4]}, {**datasources.COLLECTIONS[0]}
    _pin(monkeypatch, day, ("turkey", "Meleagris", "North America", "bird"),
         collections=[catesby])
    fetched = _serve(
        monkeypatch,
        _page("File:The Turkey Buzzard.jpg", "https://t/vulture.jpg",
              Categories="Cathartes aura (illustrations)|The natural history of Carolina"),
        _page("File:The Wild Turkey.jpg", "https://t/turkey.jpg",
              Categories="Meleagris gallopavo (illustrations)|The natural history of Carolina"))
    assert datasources.get_animal(day)["file"] == "File:The Wild Turkey.jpg"

    _pin(monkeypatch, day, ("colugo", "Galeopithecus", "Southeast Asia", "mammal"),
         collections=[iz])
    (data_dir / "cache" / "animal.json").unlink()
    _serve(monkeypatch, _page(
        "File:Galeopithecus rufus - Iconographia Zoologica - UBA01 IZ19700101.tif",
        "https://t/colugo.jpg", Categories="Galeopterus variegatus (illustrations)"))
    assert datasources.get_animal(day)["sci_name"] == "Galeopithecus rufus"


def test_new_world_corpus_skipped_for_old_world_animals(data_dir, no_network,
                                                        monkeypatch):
    """Catesby and Audubon drew American fauna: their "large Lark" is the
    meadowlark and their "Vulgo Pheasant" the ruffed grouse. An animal native
    only to the Old World is never looked for there."""
    day = date(2026, 9, 17)
    catesby = {**datasources.COLLECTIONS[4]}
    _pin(monkeypatch, day, ("lark", "Alauda", "Eurasia & North Africa", "bird"),
         collections=[catesby])
    fetched = _serve(monkeypatch, _page(
        "File:Alauda magna - The large Lark.jpg", "https://t/meadowlark.jpg",
        ImageDescription="Catesby, The natural history of Carolina"))
    assert datasources.get_animal(day) is None
    assert fetched == []
    for native in ("worldwide", "the Northern Hemisphere", "North America",
                   "wetlands of Eurasia & America", "the Mediterranean & Atlantic"):
        assert datasources._could_be_new_world(native), native
    for native in ("Eurasia", "Asia", "Australia", "Eurasia & Africa", "New Zealand"):
        assert not datasources._could_be_new_world(native), native


def test_known_namesakes_are_rejected(data_dir, no_network, monkeypatch):
    """"Squilla mantis" is the mantis shrimp, not a praying mantis — but the
    same shape, "Phascolomys wombat", is the wombat under its period genus,
    so only the listed NAMESAKES are refused."""
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("mantis", "Mantis", "warm regions worldwide", "invert"))
    _serve(monkeypatch, _page(
        "File:Squilla mantis - Print - Iconographia Zoologica - UBA01 IZ1.tif",
        "https://t/shrimp.jpg"))
    assert datasources.get_animal(day) is None
    _serve(monkeypatch, _page(
        "File:Mantis religiosa - Print - Iconographia Zoologica - UBA01 IZ2.tif",
        "https://t/mantis.jpg"))
    assert datasources.get_animal(day)["sci_name"] == "Mantis religiosa"
    _pin(monkeypatch, day, ("wombat", "Vombatus", "Australia", "mammal"))
    (data_dir / "cache" / "animal.json").unlink()
    _serve(monkeypatch, _page(
        "File:Phascolomys wombat - 1872 - Print - Iconographia Zoologica - UBA01 IZ3.tif",
        "https://t/wombat.jpg"))
    assert datasources.get_animal(day)["file"].startswith("File:Phascolomys wombat")


def test_faint_or_blank_plate_falls_to_the_next_in_the_pool(data_dir, no_network,
                                                            monkeypatch):
    """A blank scan or a faint pencil sketch is skipped for the next file in
    the pool; a collection whose candidates are all blank is passed over."""
    import io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (300, 400), (240, 235, 220)).save(buf, "JPEG")
    blank = buf.getvalue()
    day = date(2026, 9, 17)
    _pin(monkeypatch, day, ("tapir", "Tapirus", "South America & Asia", "mammal"))
    pages = [_page(f"File:Tapirus spec. - Print - Iconographia Zoologica - UBA01 IZ{i}.tif",
                   f"https://t/{i}.jpg") for i in range(2)]
    raw = {"query": {"pages": {str(i): p for i, p in enumerate(pages)}}}
    monkeypatch.setattr(datasources, "_get_json", lambda *a, **k: raw)
    slot = datasources.slot_for(day)
    bad = f"https://t/{slot % 2}.jpg"
    monkeypatch.setattr(datasources, "_get_bytes",
                        lambda url, **k: blank if url == bad else PLATE)
    a = datasources.get_animal(day)
    assert a["file"] == pages[(slot + 1) % 2]["title"]

    (data_dir / "cache" / "animal.json").unlink()
    monkeypatch.setattr(datasources, "_get_bytes", lambda url, **k: blank)
    assert datasources.get_animal(day) is None
    assert datasources._too_faint(b"not an image")
