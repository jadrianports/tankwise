"""Normalize + explicit alias table + Gazetteer centroid join (D-04).

Pass 2 of the two-pass geocoding ladder (D-01): everything the Census
addressbatch pass didn't match joins against the committed, trimmed
Gazetteer Places lookup (data/gazetteer_places_trimmed.csv) by
(normalized city name, state).

Matching is deliberately normalize + explicit-alias only -- NO fuzzy
matching (an unverifiable wrong match is worse than an honest `failed`
row here, D-04). Unmatched cities return None; the caller (geocode_stations,
Plan 04) sets geocode_status='failed'.

Pure module: no Django import, no DB access (D-23).
"""
import csv
import re
from pathlib import Path

DEFAULT_GAZETTEER_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "gazetteer_places_trimmed.csv"
)

# Explicit alias table (D-04) -- leading/standalone token substitutions only.
# NO fuzzy matching (no difflib/rapidfuzz/Levenshtein).
ALIAS = {
    "ST": "SAINT",
    "MT": "MOUNT",
    "FT": "FORT",
    "N": "NORTH",
    "S": "SOUTH",
    "E": "EAST",
    "W": "WEST",
}

# Census Gazetteer NAME values carry a trailing legal/statistical designator
# (e.g. "Fort Smith city", "Pinon Hills CDP") that the plain CSV City column
# never does (verified against the real source dataset). Stripping these
# suffixes is what makes normalize() actually agree on both sides of the
# join (Pitfall C) -- without it, the Gazetteer pass would fail to match
# the overwhelming majority of real station cities.
_LSAD_SUFFIXES = {
    "CITY",
    "TOWN",
    "VILLAGE",
    "BOROUGH",
    "CDP",
    "MUNICIPALITY",
    "GOVERNMENT",
    "COUNTY",
    "CORPORATION",
    "BALANCE",
}

_PUNCTUATION_RE = re.compile(r"[^\w\s]")


def normalize(name: str) -> str:
    """Uppercase, strip punctuation, collapse whitespace, apply the explicit
    alias table, and strip a trailing Gazetteer legal-designator suffix if
    present. Applied identically to both the CSV City column and the
    Gazetteer NAME column so the join keys agree (Pitfall C)."""
    if not name:
        return ""
    upper = name.upper()
    no_punct = _PUNCTUATION_RE.sub(" ", upper)
    tokens = no_punct.split()
    tokens = [ALIAS.get(token, token) for token in tokens]
    while tokens and tokens[-1] in _LSAD_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def load_gazetteer(path=DEFAULT_GAZETTEER_PATH) -> dict:
    """Read the committed (name, state, lat, lng) Gazetteer lookup into a
    dict keyed by (normalize(name), state) -> (lat, lng). Applies normalize()
    to the Gazetteer side of the join at load time (Pitfall C)."""
    lookup = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (normalize(row["name"]), row["state"])
            lookup[key] = (float(row["lat"]), float(row["lng"]))
    return lookup


_lookup_cache = None


def lookup_city(city: str, state: str, lookup: dict | None = None):
    """Return {'lat', 'lng', 'precision': 'city'} on a hit, or None on a miss
    (no fuzzy fallback -- caller sets geocode_status='failed').

    If `lookup` is omitted, lazily loads and caches the committed
    data/gazetteer_places_trimmed.csv lookup as a module-level singleton
    (loaded once per process) so repeated calls during a geocode run don't
    re-read the file. Pass an explicit `lookup` dict to bypass the cache
    (used by tests against small in-memory/fixture lookups).
    """
    global _lookup_cache
    if lookup is None:
        if _lookup_cache is None:
            _lookup_cache = load_gazetteer()
        lookup = _lookup_cache

    key = (normalize(city), state)
    hit = lookup.get(key)
    if hit is None:
        return None
    lat, lng = hit
    return {"lat": lat, "lng": lng, "precision": "city"}
