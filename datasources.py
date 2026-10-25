"""External data for the dashboard screen, with disk-backed caching.

Every fetcher follows the same contract:
- called synchronously from the render thread (renders already run off the
  event loop via asyncio.to_thread);
- caches to DATA_DIR/cache/ with a TTL, so renders are fast and upstream APIs
  are hit gently;
- on fetch failure, serves the last good cached value (stale-is-better-than-
  blank for a wall display); a one-off blip is only noted at INFO, and the
  failure is logged as a warning + pane event once it repeats
  (REPEATED_FAILURES in a row) or when there is no cached value to serve;
- returns None only when there has never been a successful fetch.

Tests monkeypatch `_get_json` / `_get_bytes` — nothing here should be hit by
the offline suite or the Docker build's test stage.
"""

import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path

import httpx

import config

log = logging.getLogger("inkplate.datasources")


def _log_event(message: str) -> None:
    # Lazy import: state imports screens, screens imports this module — a
    # top-level `import state` here closes that loop and breaks startup.
    import state
    state.log_event("error", message)

_UA = f"inkplate-dash/0.1 (personal e-paper dashboard; {config.CONTACT})"


def _get_json(url: str, params: dict | None = None, timeout: float = 15.0):
    with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _get_bytes(url: str, timeout: float = 30.0) -> bytes:
    with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


def slot_for(day: date, period_days: int = 1, offset: int = 0) -> int:
    """The content slot for a date: an integer that advances once every
    `period_days`, plus the pane's manual cycle `offset`."""
    return day.toordinal() // max(1, int(period_days)) + offset


# ── cache helpers ─────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    d = config.DATA_DIR / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_read(name: str) -> dict | None:
    try:
        return json.loads((_cache_dir() / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _cache_write(name: str, payload: dict) -> None:
    p = _cache_dir() / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, p)


# Consecutive failed fetches of one source before it is reported. Upstreams
# blip (Open-Meteo 503'd once on 2026-10-15 and was fine on the next try), and
# the stale copy covers a blip, so only a run of failures is worth a warning.
REPEATED_FAILURES = 3


def _cached_fetch(name: str, ttl_s: float, fetch, on_error_event: str):
    """Generic TTL cache with stale-on-error. `fetch` returns the data dict.
    The run of consecutive failures is kept in the cache entry, so it survives
    restarts; see REPEATED_FAILURES."""
    cached = _cache_read(name)
    if cached and (time.time() - cached.get("fetched_at", 0)) < ttl_s:
        return cached["data"]
    try:
        data = fetch()
    except Exception as e:
        failures = (cached.get("failures", 0) if cached else 0) + 1
        if cached is None or failures >= REPEATED_FAILURES:
            run = f" ({failures} in a row)" if failures > 1 else ""
            log.warning("%s fetch failed%s: %s", on_error_event, run, e)
            _log_event(f"{on_error_event} fetch failed{run}: {e}")
        else:
            # A one-off: the cached copy is still on the panel. Worded so the
            # nightly log review doesn't flag it; the warning above fires if
            # it keeps happening.
            log.info("%s upstream unavailable (%s), serving the cached copy; "
                     "%d in a row, reported at %d", on_error_event, type(e).__name__,
                     failures, REPEATED_FAILURES)
        if cached is None:
            return None
        _cache_write(name, {**cached, "failures": failures})
        return cached["data"]
    if cached and cached.get("failures", 0) >= REPEATED_FAILURES:
        log.info("%s fetch recovered after %d in a row", on_error_event, cached["failures"])
        _log_event(f"{on_error_event} fetch recovered after {cached['failures']} in a row")
    _cache_write(name, {"fetched_at": time.time(), "data": data})
    return data


# ── weather (Open-Meteo, keyless) ─────────────────────────────────────────────

WMO_KINDS = {
    # Open-Meteo WMO 4677 weather code → icon kind (keys of screens.WI_GLYPHS).
    # NB this is the 4677 table, *not* the 4680 one the Weather Icons font
    # ships classes for — the numbers overlap but the meanings differ, so the
    # mapping is written out rather than borrowed.
    0: "sun", 1: "sun",                                  # clear / mainly clear
    2: "partly", 3: "cloud",                             # partly / overcast
    45: "fog", 48: "fog",                                # fog, rime fog
    51: "drizzle", 53: "drizzle", 55: "drizzle",         # drizzle
    56: "sleet", 57: "sleet",                            # freezing drizzle
    61: "rain", 63: "rain", 65: "rain",                  # rain
    66: "sleet", 67: "sleet",                            # freezing rain
    71: "snow", 73: "snow", 75: "snow", 77: "snow",      # snow / grains
    80: "showers", 81: "showers", 82: "showers",         # rain showers
    85: "snow", 86: "snow",                              # snow showers
    95: "storm",                                         # thunderstorm
    96: "hail", 99: "hail",                              # thunderstorm w/ hail
}


def icon_kind(wmo_code) -> str:
    try:
        return WMO_KINDS.get(int(wmo_code), "cloud")
    except (TypeError, ValueError):
        return "cloud"


def get_weather(latitude: float, longitude: float) -> dict | None:
    """Current temp + 3 daily entries (today first). Cached 30 min."""
    def fetch():
        raw = _get_json(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude, "longitude": longitude,
                "current": "temperature_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset",
                "timezone": "auto", "forecast_days": 3,
            },
        )
        daily = raw["daily"]
        days = []
        for i in range(len(daily["time"])):
            days.append({
                "date": daily["time"][i],
                "code": daily["weather_code"][i],
                "tmax": daily["temperature_2m_max"][i],
                "tmin": daily["temperature_2m_min"][i],
                "sunrise": daily["sunrise"][i],
                "sunset": daily["sunset"][i],
            })
        return {
            "current_c": raw["current"]["temperature_2m"],
            "current_code": raw["current"]["weather_code"],
            "days": days,
        }

    # Coordinates are part of the cache key so a location change in the pane
    # takes effect at the next render, not after the old TTL expires.
    return _cached_fetch(f"weather_{latitude:.3f}_{longitude:.3f}.json",
                         config.WEATHER_TTL_S, fetch, "weather")


# ── snow report (Open-Meteo, seasonal) ────────────────────────────────────────

def get_snow(latitude: float, longitude: float) -> dict | None:
    """Snowpack and snowfall at a ski area: modelled depth now, snowfall over
    the last 72 h, and per-day snowfall + weather code for today and the next
    two days. Open-Meteo models a grid cell, so depth is an estimate, not the
    resort's reported base. Cached 1 h; stale-on-error like the weather."""
    from datetime import datetime, timedelta

    def fetch():
        raw = _get_json(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude, "longitude": longitude,
                "current": "snow_depth",
                "hourly": "snowfall",
                "daily": "snowfall_sum,weather_code",
                "timezone": "auto", "past_days": 3, "forecast_days": 3,
            },
        )
        now = datetime.fromisoformat(raw["current"]["time"])
        cutoff = now - timedelta(hours=72)
        hourly = raw["hourly"]
        recent = sum(v or 0 for t, v in zip(hourly["time"], hourly["snowfall"])
                     if cutoff < datetime.fromisoformat(t) <= now)
        daily = raw["daily"]
        today = now.date().isoformat()
        days = [{"date": t, "snow_cm": daily["snowfall_sum"][i] or 0,
                 "code": daily["weather_code"][i]}
                for i, t in enumerate(daily["time"]) if t >= today][:3]
        return {
            "depth_cm": (raw["current"]["snow_depth"] or 0) * 100,  # API gives metres
            "recent_cm": recent,
            "days": days,
        }

    return _cached_fetch(f"snow_{latitude:.3f}_{longitude:.3f}.json",
                         config.SNOW_TTL_S, fetch, "snow")


# ── daily animal print (Wikimedia Commons, several historical corpuses) ──────
# Each day picks an animal and a starting collection (both deterministic),
# then falls through the eligible collections until one has a usable plate.
# Latin genus terms where filenames are binomials; common names for the
# English-titled corpuses.

# (common name, Latin search term, native range, kind)
ANIMALS = [
    ("fox", "Vulpes", "the Northern Hemisphere", "mammal"),
    ("owl", "Strix", "Eurasia", "bird"),
    ("bear", "Ursus", "the Northern Hemisphere", "mammal"),
    ("hare", "Lepus", "the Northern Hemisphere", "mammal"),
    ("wolf", "Canis lupus", "Eurasia & North America", "mammal"),
    ("lion", "Felis leo", "Africa & India", "mammal"),
    ("tiger", "Panthera tigris", "Asia", "mammal"),
    ("elephant", "Elephas", "South Asia", "mammal"),
    ("rhinoceros", "Rhinoceros", "South Asia", "mammal"),
    ("hippopotamus", "Hippopotamus", "sub-Saharan Africa", "mammal"),
    ("giraffe", "Giraffa", "Africa", "mammal"),
    ("camel", "Camelus", "Central Asia & Arabia", "mammal"),
    ("zebra", "Equus zebra", "southern Africa", "mammal"),
    ("otter", "Lutra", "Eurasia", "mammal"),
    ("badger", "Meles", "Eurasia", "mammal"),
    ("hedgehog", "Erinaceus", "Europe", "mammal"),
    ("squirrel", "Sciurus", "Eurasia & the Americas", "mammal"),
    ("beaver", "Castor", "Eurasia & North America", "mammal"),
    ("porcupine", "Hystrix", "Africa & Asia", "mammal"),
    ("kangaroo", "Macropus", "Australia", "mammal"),
    ("sloth", "Bradypus", "Central & South America", "mammal"),
    ("armadillo", "Dasypus", "the Americas", "mammal"),
    ("pangolin", "Manis", "Asia", "mammal"),
    ("bat", "Vespertilio", "Eurasia", "mammal"),
    ("manatee", "Trichechus", "the Caribbean & Amazon", "mammal"),
    ("seal", "Phoca", "northern seas", "mammal"),
    ("paper nautilus", "Argonauta", "warm seas worldwide", "invert"),
    ("man o' war", "Physalia", "warm oceans", "invert"),
    ("walrus", "Odobenus", "the Arctic", "mammal"),
    ("orangutan", "Pongo", "Borneo & Sumatra", "mammal"),
    ("lemur", "Lemur", "Madagascar", "mammal"),
    ("tapir", "Tapirus", "South America & Asia", "mammal"),
    ("anteater", "Myrmecophaga", "Central & South America", "mammal"),
    ("opossum", "Didelphis", "the Americas", "mammal"),
    ("platypus", "Ornithorhynchus", "Australia", "mammal"),
    ("wombat", "Vombatus", "Australia", "mammal"),
    ("eagle", "Aquila", "the Northern Hemisphere", "bird"),
    ("falcon", "Falco", "every continent but Antarctica", "bird"),
    ("raven", "Corvus", "the Northern Hemisphere", "bird"),
    ("heron", "Ardea", "wetlands worldwide", "bird"),
    ("stork", "Ciconia", "Eurasia & Africa", "bird"),
    ("pelican", "Pelecanus", "warm coasts worldwide", "bird"),
    ("flamingo", "Phoenicopterus", "Africa & the Americas", "bird"),
    ("peacock", "Pavo", "South Asia", "bird"),
    ("parrot", "Psittacus", "West & Central Africa", "bird"),
    ("macaw", "Ara", "South America", "bird"),
    ("cockatoo", "Cacatua", "Australia & New Guinea", "bird"),
    ("toucan", "Ramphastos", "Central & South America", "bird"),
    ("hummingbird", "Trochilus", "the Americas", "bird"),
    ("butterflyfish", "Chaetodon", "tropical reefs", "fish"),
    ("cock-of-the-rock", "Rupicola", "the Amazon", "bird"),
    ("hoatzin", "Opisthocomus", "the Amazon", "bird"),
    ("harpy eagle", "Harpyia", "South America", "bird"),
    ("ibis", "Eudocimus", "the Americas", "bird"),
    ("condor", "Vultur", "the Andes", "bird"),
    ("bird-of-paradise", "Paradisea apoda", "New Guinea", "bird"),
    ("hornbill", "Buceros", "Southeast Asia", "bird"),
    ("cassowary", "Casuarius", "New Guinea & Australia", "bird"),
    ("lyrebird", "Menura", "Australia", "bird"),
    ("albatross", "Diomedea", "the Southern Ocean", "bird"),
    ("penguin", "Aptenodytes", "Antarctica", "bird"),
    ("ostrich", "Struthio", "Africa", "bird"),
    ("swan", "Cygnus", "the Northern Hemisphere", "bird"),
    ("kingfisher", "Alcedo", "Eurasia & North Africa", "bird"),
    ("woodpecker", "Picus", "Eurasia", "bird"),
    ("crocodile", "Crocodilus", "Africa & Asia", "reptile"),
    ("tortoise", "Testudo", "the Mediterranean", "reptile"),
    ("chameleon", "Chamaeleo", "Africa & Madagascar", "reptile"),
    ("python", "Python", "Africa & Asia", "reptile"),
    ("frog", "Rana", "the Northern Hemisphere", "amphibian"),
    ("salamander", "Salamandra", "Europe", "amphibian"),
    ("shark", "Carcharias", "oceans worldwide", "fish"),
    ("ray", "Raja", "the Atlantic", "fish"),
    ("seahorse", "Hippocampus", "coastal seas worldwide", "fish"),
    ("octopus", "Octopus", "oceans worldwide", "invert"),
    ("nautilus", "Nautilus", "the Indo-Pacific", "invert"),
    ("lobster", "Homarus", "the North Atlantic", "invert"),
    ("crab", "Cancer", "coastal seas", "invert"),
    ("beetle", "Scarabaeus", "Africa & the Mediterranean", "invert"),
    ("butterfly", "Papilio", "every continent but Antarctica", "invert"),
    ("dragonfly", "Libellula", "the Northern Hemisphere", "invert"),
    ("mantis", "Mantis", "warm regions worldwide", "invert"),
    # ── the ocean ─────────────────────────────────────────────────────────────
    ("john dory", "Zeus faber", "the Atlantic & Mediterranean", "fish"),
    ("triggerfish", "Balistes", "tropical reefs", "fish"),
    ("sea lion", "Otaria", "Pacific coasts", "mammal"),
    ("sea cucumber", "Holothuria", "sea floors worldwide", "invert"),
    ("dugong", "Dugong", "the Indian Ocean", "mammal"),
    ("sea turtle", "Chelonia", "tropical seas", "reptile"),
    ("swordfish", "Xiphias", "open oceans", "fish"),
    ("tuna", "Thynnus", "open oceans", "fish"),
    ("sunfish", "Orthagoriscus", "open oceans", "fish"),
    ("flying fish", "Exocoetus", "warm seas", "fish"),
    ("anglerfish", "Lophius", "the Atlantic", "fish"),
    ("pufferfish", "Diodon", "tropical seas", "fish"),
    ("sawfish", "Pristis", "warm coasts", "fish"),
    ("torpedo ray", "Torpedo", "the Mediterranean & Atlantic", "fish"),
    ("sturgeon", "Acipenser", "rivers of the Northern Hemisphere", "fish"),
    ("salmon", "Salmo", "the North Atlantic & its rivers", "fish"),
    ("pike", "Esox", "northern lakes & rivers", "fish"),
    ("carp", "Cyprinus", "Eurasia", "fish"),
    ("cod", "Gadus", "the North Atlantic", "fish"),
    ("herring", "Clupea", "the North Atlantic", "fish"),
    ("eel", "Anguilla", "Atlantic rivers & the Sargasso", "fish"),
    ("moray eel", "Muraena", "warm seas", "fish"),
    ("remora", "Echeneis", "warm seas", "fish"),
    ("squid", "Loligo", "oceans worldwide", "invert"),
    ("cuttlefish", "Sepia", "the Mediterranean & Atlantic", "invert"),
    ("jellyfish", "Medusa", "oceans worldwide", "invert"),
    ("starfish", "Asterias", "coastal seas", "invert"),
    ("sea urchin", "Echinus", "coastal seas", "invert"),
    ("sea anemone", "Actinia", "coastal rocks worldwide", "invert"),
    ("coral", "Madrepora", "tropical reefs", "invert"),
    ("barnacle", "Lepas", "oceans worldwide", "invert"),
    ("hermit crab", "Pagurus", "coastal seas", "invert"),
    ("shrimp", "Palaemon", "coastal seas", "invert"),
    ("horseshoe crab", "Limulus", "Atlantic coasts", "invert"),
    ("crayfish", "Astacus", "rivers & coasts", "invert"),
    ("pipefish", "Syngnathus", "coastal shallows", "fish"),
    ("lamprey", "Petromyzon", "northern rivers & coasts", "fish"),
    ("ratfish", "Chimaera", "deep northern seas", "fish"),
    ("wrasse", "Labrus", "the Atlantic & Mediterranean", "fish"),
    ("moon jelly", "Aurelia", "coastal seas worldwide", "invert"),
    ("by-the-wind sailor", "Velella", "open oceans", "invert"),
    ("sea slug", "Doris", "coastal seas worldwide", "invert"),
    ("feather star", "Comatula", "tropical reefs", "invert"),
    ("murex snail", "Murex", "the Mediterranean", "invert"),
    ("oyster", "Ostrea", "coastal seas worldwide", "invert"),
    ("scallop", "Pecten", "coastal seas worldwide", "invert"),
    ("sea worm", "Nereis", "sea floors worldwide", "invert"),
    # ── more beasts of the land ───────────────────────────────────────────────
    ("leopard", "Felis pardus", "Africa & Asia", "mammal"),
    ("lynx", "Felis lynx", "northern forests", "mammal"),
    ("hyena", "Hyaena", "Africa & Asia", "mammal"),
    ("jackal", "Canis aureus", "Africa & Asia", "mammal"),
    ("wild boar", "Sus scrofa", "Eurasia", "mammal"),
    ("red deer", "Cervus elaphus", "Eurasia", "mammal"),
    ("moose", "Cervus alces", "northern forests", "mammal"),
    ("reindeer", "Rangifer", "the Arctic", "mammal"),
    ("ibex", "Capra ibex", "the Alps", "mammal"),
    ("chamois", "Antilope rupicapra", "European mountains", "mammal"),
    ("antelope", "Antilope", "Africa & Asia", "mammal"),
    ("gazelle", "Antilope gazella", "Africa & Arabia", "mammal"),
    ("bison", "Bison", "North America & Europe", "mammal"),
    ("buffalo", "Bos bubalis", "South Asia", "mammal"),
    ("mole", "Talpa", "Europe", "mammal"),
    ("weasel", "Mustela", "the Northern Hemisphere", "mammal"),
    ("marten", "Martes", "northern forests", "mammal"),
    ("wolverine", "Gulo", "the far north", "mammal"),
    ("raccoon", "Procyon", "North America", "mammal"),
    ("skunk", "Mephitis", "the Americas", "mammal"),
    ("marmot", "Marmota", "mountain meadows", "mammal"),
    ("dormouse", "Myoxus", "Europe", "mammal"),
    ("jerboa", "Dipus", "Central Asian deserts", "mammal"),
    ("capybara", "Hydrochoerus", "South America", "mammal"),
    ("guinea pig", "Cavia", "the Andes", "mammal"),
    ("chinchilla", "Chinchilla", "the Andes", "mammal"),
    ("gorilla", "Gorilla", "Central Africa", "mammal"),
    ("gibbon", "Hylobates", "Southeast Asia", "mammal"),
    # Papio, not the period "Cynocephalus": that genus now names the colugo
    # (Cynocephalus volans), so the old term captioned a flying lemur as
    # a baboon. The colugo entry below keeps its own period name.
    ("baboon", "Papio", "Africa", "mammal"),
    ("aye-aye", "Cheiromys", "Madagascar", "mammal"),
    ("koala", "Phascolarctos", "Australia", "mammal"),
    ("thylacine", "Thylacinus", "Tasmania", "mammal"),
    ("bandicoot", "Perameles", "Australia", "mammal"),
    ("flying squirrel", "Pteromys", "northern forests", "mammal"),
    ("colugo", "Galeopithecus", "Southeast Asia", "mammal"),
    ("hyrax", "Hyrax", "Africa & Arabia", "mammal"),
    # ── more birds ────────────────────────────────────────────────────────────
    ("turkey", "Meleagris", "North America", "bird"),
    ("pheasant", "Phasianus", "Asia", "bird"),
    ("grouse", "Tetrao", "northern forests", "bird"),
    ("quail", "Coturnix", "Eurasia & Africa", "bird"),
    ("dove", "Columba", "worldwide", "bird"),
    ("cuckoo", "Cuculus", "Eurasia & Africa", "bird"),
    ("nightingale", "Luscinia", "Eurasia", "bird"),
    ("lark", "Alauda", "Eurasia & North Africa", "bird"),
    ("magpie", "Pica", "the Northern Hemisphere", "bird"),
    ("jay", "Garrulus", "Eurasia", "bird"),
    ("starling", "Sturnus", "Eurasia", "bird"),
    ("oriole", "Oriolus", "Eurasia & Africa", "bird"),
    ("bee-eater", "Merops", "Africa & Eurasia", "bird"),
    ("hoopoe", "Upupa", "Eurasia & Africa", "bird"),
    ("nightjar", "Caprimulgus", "worldwide", "bird"),
    ("crane", "Grus", "wetlands of Eurasia & America", "bird"),
    ("bustard", "Otis", "Eurasian steppes", "bird"),
    ("avocet", "Recurvirostra", "wetlands worldwide", "bird"),
    ("curlew", "Numenius", "coasts & moors", "bird"),
    ("lapwing", "Vanellus", "Eurasia", "bird"),
    ("puffin", "Fratercula", "the North Atlantic", "bird"),
    ("great auk", "Alca", "the North Atlantic", "bird"),
    ("gannet", "Sula", "Atlantic cliffs", "bird"),
    ("cormorant", "Phalacrocorax", "coasts worldwide", "bird"),
    ("frigatebird", "Fregata", "tropical oceans", "bird"),
    ("tropicbird", "Phaeton", "tropical oceans", "bird"),
    ("spoonbill", "Platalea", "wetlands of Eurasia & Africa", "bird"),
    ("kite", "Milvus", "Eurasia & Africa", "bird"),
    ("buzzard", "Buteo", "the Northern Hemisphere", "bird"),
    ("goshawk", "Astur", "northern forests", "bird"),
    ("emu", "Dromaius", "Australia", "bird"),
    ("kiwi", "Apteryx", "New Zealand", "bird"),
    ("dodo", "Didus", "Mauritius", "bird"),
    ("kookaburra", "Dacelo", "Australia", "bird"),
    ("sunbird", "Nectarinia", "Africa & Asia", "bird"),
    ("tanager", "Tanagra", "South America", "bird"),
    ("motmot", "Momotus", "Central & South America", "bird"),
    ("trogon", "Trogon", "tropical forests", "bird"),
    # ── more reptiles & amphibians ────────────────────────────────────────────
    ("iguana", "Iguana", "Central & South America", "reptile"),
    ("monitor lizard", "Varanus", "Africa, Asia & Australia", "reptile"),
    ("gecko", "Gecko", "warm regions worldwide", "reptile"),
    ("boa", "Boa", "South America", "reptile"),
    ("rattlesnake", "Crotalus", "the Americas", "reptile"),
    ("cobra", "Naja", "Africa & Asia", "reptile"),
    ("viper", "Vipera", "Eurasia", "reptile"),
    ("alligator", "Crocodilus lucius", "the American South", "reptile"),
    ("gharial", "Gavialis", "Indian rivers", "reptile"),
    ("toad", "Bufo", "worldwide", "amphibian"),
    ("tree frog", "Hyla", "the Americas & Eurasia", "amphibian"),
    ("axolotl", "Siredon", "Mexican lakes", "amphibian"),
    # ── more small things ─────────────────────────────────────────────────────
    ("bee", "Apis", "worldwide", "invert"),
    ("wasp", "Vespa", "the Northern Hemisphere", "invert"),
    ("ant", "Formica", "worldwide", "invert"),
    ("cicada", "Cicada", "warm regions", "invert"),
    ("locust", "Locusta", "Africa & Asia", "invert"),
    ("stag beetle", "Lucanus", "European forests", "invert"),
    ("scorpion", "Buthus", "warm regions worldwide", "invert"),
    ("spider", "Epeira", "worldwide", "invert"),
    ("centipede", "Scolopendra", "warm regions", "invert"),
    ("snail", "Helix", "worldwide", "invert"),
    ("hawk moth", "Sphinx", "worldwide", "invert"),
    ("silkworm", "Bombyx", "China", "invert"),
]

# Historical corpuses on Commons. `query` is a Cirrus search template
# ({latin}/{common}); `artist`/`place`/`years` back-fill the caption when the
# file's own metadata is silent; `kinds` (None = any) gates eligibility.
# `evidence` (case-insensitive regex) must match the file's title,
# description, categories, artist or credit: the search is full-text, so
# "John Gould quail" happily returns a light aircraft called the Quail and a
# 2023 photo of a live dove. `new_world` corpora drew only American fauna, so
# an Old World animal found there is a namesake (Catesby's "large Lark" is
# the meadowlark; Audubon's "Vulgo Pheasant" is the ruffed grouse).
COLLECTIONS = [
    {"key": "iz", "query": 'Iconographia Zoologica {latin}',
     "artist": None, "place": "Amsterdam", "years": "1700-1880", "kinds": None,
     "evidence": r"Iconographia Zoologica|UBA01|University of Amsterdam"},
    {"key": "gessner", "query": 'Gessner "Historiae animalium" {latin}',
     "artist": "Conrad Gessner", "place": "Zürich", "years": "1551-1558",
     "kinds": None, "evidence": r"Gess?ner|Historiae? animalium"},
    {"key": "buffon", "query": 'Buffon "Histoire naturelle" {latin}',
     "artist": "Buffon's engravers", "place": "Paris", "years": "1749-1788",
     "kinds": None, "evidence": r"Buffon|Histoire naturelle"},
    {"key": "brehm", "query": '"Brehms Tierleben" {latin}',
     "artist": "Alfred Brehm", "place": "Leipzig", "years": "1863-1869",
     "kinds": None, "evidence": r"Brehm"},
    {"key": "catesby", "query": 'Catesby "Natural History of Carolina" {common}',
     "artist": "Mark Catesby", "place": "England", "years": "1731-1743",
     "kinds": {"bird", "reptile", "fish", "invert"},
     "evidence": r"Catesby|natural history of Carolina", "new_world": True},
    {"key": "audubon", "query": 'Audubon "Birds of America" {common}',
     "artist": "John James Audubon", "place": "America", "years": "1827-1838",
     "kinds": {"bird"}, "evidence": r"Audubon|Birds of America",
     "new_world": True},
    {"key": "gould", "query": 'John Gould {common}',
     "artist": "John Gould", "place": "London", "years": "1840-1881",
     "kinds": {"bird"},
     # Not a bare "Gould": Jay Gould the financier is on Commons too.
     "evidence": r"John Gould|Elizabeth Gould|J\.? (?:and|&) E\.? Gould|"
                 r"\(Gould\)|Gould(?:Birds|'s)|H\. ?C\. Richter"},
]

# A native range rules out a New World corpus when it names only Old World
# places. Anything wide or vague ("worldwide", "the Northern Hemisphere",
# "coastal seas") keeps it in — the other filters still apply.
_OLD_WORLD = re.compile(
    r"Eurasia|Europe|Africa|Asia|Arabia|India|China|Australia|New Guinea|"
    r"New Zealand|Madagascar|Mauritius|Borneo|Sumatra|Mediterranean|"
    r"Indo-Pacific|Southern Ocean|Antarctica|steppes")
_NEW_WORLD_OR_WIDE = re.compile(
    r"(?i)America|Caribbean|Amazon|Andes|Mexic|Atlantic|Sargasso|worldwide|"
    r"Hemisphere|every continent|ocean|seas\b|coast|tropical|warm|northern|"
    r"wetlands|rivers|reefs|cliffs|moors")


def _could_be_new_world(native: str) -> bool:
    return not _OLD_WORLD.search(native) or bool(_NEW_WORLD_OR_WIDE.search(native))


# Titles that name an animal and depict another, where no general rule can
# tell (a period genus that leads the title looks exactly like a namesake's:
# "Phascolomys wombat" is the wombat, "Squilla mantis" the mantis shrimp).
# Common name -> regex over the title. Add to it when a veto finds one.
NAMESAKES = {
    "mantis": r"\bSquilla\b",
    "lion": r"(?i)chien[- ]lion|lion[- ]dog",
    # Catesby's turkey vulture: "Buteo species Gallinae Pavonis".
    "buzzard": r"(?i)turkey buzzard|Gallinae Pavonis",
}

ANIMAL_IMG = "animal.jpg"


_UNKNOWN_ARTIST = re.compile(
    r"^(unknown|unknown author|anonymous|anon|not stated|unspecified|\?)$", re.I)


def _clean_artist(s: str) -> str:
    """Normalise a Commons Artist value, or return "" if it names nobody.

    Two real-world shapes this handles: the field often repeats itself once
    (nested markup collapses to "Unknown authorUnknown author"), and a large
    share of plates are credited to "Unknown author" — which should read as
    no artist at all, so the caption falls back to the collection's.
    """
    words = s.split()
    half = len(words) // 2
    if words and len(words) % 2 == 0 and words[:half] == words[half:]:
        words = words[:half]
    s = " ".join(words)
    return "" if _UNKNOWN_ARTIST.match(s) else s


# A file is downloaded before anything can look at it, and some are nearly
# empty: a blank scan, a faint pencil sketch on a mount card. Ink coverage of
# the subject box (as the renderer finds it) separates them cleanly — those
# measured 0.2-3.3%, the faintest real plate 20%.
FAINT_INK = 0.05
FAINT_RETRIES = 3   # downloads per collection before falling through


def _too_faint(img: bytes) -> bool:
    """True when an image carries next to no ink, or isn't an image at all."""
    import io

    from PIL import Image, UnidentifiedImageError

    # Lazy: screens imports this module, and owns the ink analysis.
    import screens
    try:
        art = Image.open(io.BytesIO(img)).convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        log.warning("downloaded plate is not a readable image: %s", e)
        return True
    sheet, box, _ = screens._prep_plate(art)
    mask, (w, h) = screens._ink_mask(sheet.crop(box))
    return sum(1 for v in mask.getdata() if v) / (w * h) < FAINT_INK


def _try_collection(coll: dict, common: str, latin: str, slot: int,
                    vetoed=frozenset()) -> dict | None:
    """One collection attempt: search, filter, deterministic pick, download.
    `vetoed` holds Commons file titles banned from the pane.
    Returns the animal data dict (sans native/common) or None if unusable."""
    import re

    query = coll["query"].format(latin=latin, common=common) + " filetype:bitmap"
    raw = _get_json(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json",
            "generator": "search", "gsrsearch": query,
            "gsrlimit": 25, "gsrnamespace": 6,
            "prop": "imageinfo", "iiprop": "url|extmetadata",
            "iiurlwidth": config.ANIMAL_IMG_WIDTH,
        },
    )
    pages = list(raw.get("query", {}).get("pages", {}).values())

    def _meta(p, key):
        v = p["imageinfo"][0].get("extmetadata", {}).get(key, {}).get("value", "")
        # Tags become spaces, not nothing: Commons nests these in markup, and
        # stripping to nothing glues words together ("authorUnknown").
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(v))).strip()

    def _is_modern_repro(p) -> bool:
        """True for a modern *photograph of* one of these plates (someone's
        shot of a library book) rather than a scan of the plate itself. Every
        corpus here predates 1900, so a present-day DateTimeOriginal gives it
        away. Such files are usable — but Commons credits the photographer,
        so their Artist/date must never reach the caption: the credit belongs
        to whoever drew the thing."""
        years = [int(y) for y in re.findall(r"\b(1[5-9]\d\d|20\d\d)\b",
                                            _meta(p, "DateTimeOriginal"))]
        return bool(years) and max(years) >= 1900
    # Drop anatomical dissection plates (Dutch/French/English tags — a pile of
    # sloth stomachs is not wall art; IZ files a hippo's jaw as "kaak" and a
    # hornbill's beak as "snavel") and whole-book Internet Archive dumps.
    anatomical = ("ingewanden", "anatomie", "skelet", "schedel", "gebit",
                  "spieren", "embryo", "ontleed", "anatomy", "skeleton",
                  "squelette", "glandes", "organes", "crane", "crâne",
                  "skull", "kaak", "snavel", "tanden", "hoorns", "baarmoeder",
                  "geslachtsorganen", "maag", "darmen", "hersenen")
    def _about(p) -> str:
        """What the file says about itself beyond its title."""
        return " ".join(_meta(p, k) for k in
                        ("ImageDescription", "Categories", "Artist", "Credit"))

    def _taxa(p) -> list[str]:
        """Taxa Commons files the picture under ("Cathartes aura
        (illustrations)"), which name the animal as it is known today."""
        cats = p["imageinfo"][0].get("extmetadata", {}).get(
            "Categories", {}).get("value", "")
        return [c.removesuffix(" (illustrations)").strip()
                for c in str(cats).split("|") if c.endswith(" (illustrations)")]

    # The title *and* the file's own description and categories: a skull on a
    # plinth is titled "Hyrax Buffon 1789" and only its description says so.
    pages = [p for p in pages if p.get("imageinfo")
             and "(IA " not in p.get("title", "")
             and not any(t in (p.get("title", "") + " " + _about(p)).lower()
                         for t in anatomical)]
    # The file must belong to the corpus the search was aimed at.
    if coll.get("evidence"):
        ev = re.compile(coll["evidence"], re.I)
        pages = [p for p in pages if ev.search(p.get("title", "") + " " + _about(p))]
    # Require the beast's name in the file TITLE, not just the search match —
    # full-text search happily returns a page of sea monsters whose prose
    # merely mentions rhinos. A collection with no titled plate is skipped;
    # the IZ backstop titles by binomial, so nearly every animal resolves.
    # A two-word Latin term must match in full: genus alone is ambiguous
    # ("Canis" would happily label a jackal as the wolf). A one-word term
    # must stand as the *genus*: leading the file name, or capitalised and
    # followed by a lowercase epithet — never a substring, or "Papilio" (the
    # butterflies) catches the fish Pteraclis papilio. The common name must
    # be a whole word, or "butterfly" catches butterflyfish.
    latin_l = latin.lower()

    # A plate found by its common name alone is checked against the taxa
    # Commons files it under: "The Turkey Buzzard" names a turkey and is
    # filed under Cathartes aura. A Latin match is not — the period genus
    # often differs from today's (our colugo is Galeopithecus, Commons'
    # Galeopterus), so its taxa would reject the right beast.
    genus_l = latin.split()[0].lower()

    def names_beast(p) -> bool:
        t = p.get("title", "").removeprefix("File:")
        if " " in latin:
            if latin_l in t.lower():
                return True
        elif (re.match(rf"{re.escape(latin)}\b", t, re.I)
              or re.search(rf"\b{re.escape(latin)} [a-z]", t)):
            return True
        if re.search(rf"(?i)\b{re.escape(common)}\b", t) is None:
            return False
        if common in NAMESAKES and re.search(NAMESAKES[common], t):
            return False
        taxa = _taxa(p)
        return not taxa or any(x.split()[0].lower() == genus_l for x in taxa)

    pages = [p for p in pages if p["title"] not in vetoed]
    pool = [p for p in pages if names_beast(p)]
    if not pool:
        return None
    # Prefer a scan of the plate itself; fall back to a photograph of one.
    pool.sort(key=lambda p: p["title"])
    originals = [p for p in pool if not _is_modern_repro(p)]
    pool = originals or pool
    # The slot's pick, or the next ones in the pool if it turns out blank.
    for k in range(min(FAINT_RETRIES, len(pool))):
        pick = pool[(slot + k) % len(pool)]
        img = _get_bytes(pick["imageinfo"][0]["thumburl"])
        if not _too_faint(img):
            break
        log.info("skipped faint or blank plate %s", pick["title"])
    else:
        return None
    tmp = _cache_dir() / (ANIMAL_IMG + ".tmp")
    tmp.write_bytes(img)
    os.replace(tmp, _cache_dir() / ANIMAL_IMG)

    title = pick["title"].removeprefix("File:")
    title = re.sub(r"\.(tiff?|jpe?g|png)$", "", title, flags=re.I)
    # Binomial: the genus word plus its species epithet, wherever it appears —
    # but an English word trailing the genus ("Rhinoceros woodcut") is not one.
    not_epithets = {"woodcut", "print", "plate", "engraving", "lithograph",
                    "illustration", "drawing", "painting", "by", "from", "in",
                    "and", "with", "adult", "male", "female", "young",
                    # corpus authors show up lowercased in some filenames
                    "brehm", "brehms", "gould", "audubon", "catesby",
                    "gessner", "gesner", "buffon", "seba"}
    genus_proper = latin.split()[0].capitalize()
    m = re.search(rf"(?i)\b{re.escape(latin.split()[0])}(?:\s+([a-z][a-z-]+))?", title)
    sci_name = latin
    if m:
        epithet = m.group(1)
        sci_name = (f"{genus_proper} {epithet.lower()}"
                    if epithet and epithet.lower() not in not_epithets
                    else genus_proper)
    # Year: any 15xx-19xx (or range) in the title, else the collection's era.
    # Credit and date always describe the original work, never the
    # photographer or the day they pressed the shutter.
    repro = _is_modern_repro(pick)
    ym = re.search(r"\b(1[5-9]\d\d(?:-1[5-9]\d\d)?)\b", title)
    if ym:
        year = ym.group(1)
    else:
        dy = [] if repro else re.findall(r"\b1[5-9]\d\d\b", _meta(pick, "DateTimeOriginal"))
        year = dy[0] if dy else coll["years"]
    raw_artist = "" if repro else _meta(pick, "Artist")
    cleaned = _clean_artist(raw_artist)
    if raw_artist and not cleaned:
        # The file positively states the artist is unknown. Say nothing —
        # better a shorter caption than a confident guess, and far better
        # than printing the words "Unknown author" on the wall.
        artist = None
    elif 0 < len(cleaned) <= 40:
        artist = cleaned
    else:
        artist = coll["artist"]   # no Artist field at all: the corpus's own
    return {
        "path": str(_cache_dir() / ANIMAL_IMG),
        "file": pick["title"],  # identity for the pane's image veto
        "sci_name": sci_name,
        "year": year,
        "artist": artist,
        "place": coll["place"],
        "collection": coll["key"],
    }


def cached_animal() -> dict | None:
    """The engraving currently cached (i.e. the one on the panel), without
    fetching — cheap enough for the pane's 10 s state poll."""
    meta = _cache_read("animal.json")
    return meta.get("data") if meta else None


def get_animal(day: date | None = None, offset: int = 0,
               period_days: int = 1, vetoed=()) -> dict | None:
    """The current engraving, as a data dict for the caption: common/sci name,
    year, artist + artist's place (collection defaults when the file is
    silent), the animal's native range.

    Deterministic per content slot; one fetch per slot; the collection rotates
    and falls through until one has a titled plate; falls back to the last
    successful image on total failure. `period_days` sets how often the animal
    changes (1 = daily); `offset` advances the sequence — the pane's "next
    animal" button. `vetoed` holds Commons file titles: a vetoed image is
    never picked again, and the same animal is drawn from its next plate.
    """
    day = day or date.today()
    slot = slot_for(day, period_days, offset)
    vetoed = set(vetoed)
    meta = _cache_read("animal.json")
    # A cached entry without "file" predates image vetoes: refetch it once so
    # the pane can veto what's showing.
    if (meta and meta.get("data", {}).get("slot") == slot
            and meta["data"].get("file")
            and meta["data"]["file"] not in vetoed
            and (_cache_dir() / ANIMAL_IMG).exists()):
        return meta["data"]

    common, latin, native, kind = ANIMALS[slot % len(ANIMALS)]
    eligible = [c for c in COLLECTIONS
                if (c["kinds"] is None or kind in c["kinds"])
                and (not c.get("new_world") or _could_be_new_world(native))]
    rot = slot % len(eligible) if eligible else 0
    for coll in eligible[rot:] + eligible[:rot]:
        try:
            data = _try_collection(coll, common, latin, slot, vetoed)
        except Exception as e:
            log.warning("animal fetch via %s failed: %s", coll["key"], e)
            data = None
        if data:
            data.update({"common_name": common, "native": native,
                         "slot": slot, "for_date": day.isoformat()})
            _cache_write("animal.json", {"fetched_at": time.time(), "data": data})
            # The only record of what hung on the wall on a given day.
            log.info("animal for %s: %s via %s: %s", day.isoformat(), common,
                     coll["key"], data["file"])
            return data

    _log_event(f"animal illustration fetch failed for {common} in all collections")
    if (meta and (_cache_dir() / ANIMAL_IMG).exists()
            and meta["data"].get("file") not in vetoed):
        log.warning("animal illustration fetch failed for %s in all collections, "
                    "keeping the previous one", common)
        return meta["data"]  # yesterday's beast beats no beast (unless vetoed)
    log.error("animal illustration fetch failed for %s in all collections, "
              "no illustration to show", common)
    return None
