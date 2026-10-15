"""Run tuning for the realtor scrapers (waits, parallelism), read from tuning.toml.

Edit tuning.toml and rerun; no code changes needed. Every key must be present and well formed
(seconds >= 0, counts >= 1, ranges [min, max] with 0 <= min <= max). Anything missing, unknown
or malformed stops the script at start-up instead of quietly using some other value.
"""
import os
import random
import tomllib
from types import SimpleNamespace

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuning.toml")

# section -> key -> kind
SCHEMA = {
    "scroll": {"step_px": "range", "step_pause": "range"},
    "inventory": {
        "hesitation": "range", "poll_interval": "seconds", "poll_retries": "count",
        "page_timeout": "seconds", "restart_pause": "seconds", "between_pages": "range",
        "between_searches": "seconds", "between_locations": "seconds",
    },
    "details": {
        "workers": "count", "record_wait": "seconds", "hesitation": "range",
        "expand_click_pause": "seconds", "expand_settle": "seconds",
        "capture_timeout": "seconds", "between_pages": "range",
    },
    "postprocess": {"concurrency": "count"},
}

class TuningError(ValueError):
    """tuning.toml is missing a key, has an unknown one, or holds a value of the wrong kind."""

def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def _checked(where, kind, v):
    if kind == "seconds" and _number(v) and v >= 0:
        return v
    if kind == "count" and isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        return v
    if kind == "range" and isinstance(v, list) and len(v) == 2 and all(_number(x) for x in v) and 0 <= v[0] <= v[1]:
        return tuple(v)
    raise TuningError(f"{where} = {v!r} is not a valid {kind}")

def load(path=PATH):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    if set(data) != set(SCHEMA):
        raise TuningError(f"{path}: sections {sorted(data)}, expected {sorted(SCHEMA)}")
    sections = {}
    for name, keys in SCHEMA.items():
        given = data[name]
        if not isinstance(given, dict) or set(given) != set(keys):
            have = set(given) if isinstance(given, dict) else set()
            raise TuningError(f"{path} [{name}]: missing {sorted(set(keys) - have)}, unknown {sorted(have - set(keys))}")
        sections[name] = SimpleNamespace(**{k: _checked(f"[{name}] {k}", kind, given[k]) for k, kind in keys.items()})
    return SimpleNamespace(**sections)

def wait(span):
    """A random wait within a (min, max) range."""
    return random.uniform(*span)

TUNING = load()
