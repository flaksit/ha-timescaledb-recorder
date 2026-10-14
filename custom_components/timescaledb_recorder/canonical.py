"""Canonical JSON form of registry entry fields stored in `extra` and `labels`.

No Home Assistant imports: repair_scd2.py runs as a plain script and the
dimension migration uses the same definitions as the live write path.
"""
import ast
import json


def _sort_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def canonical(value: object) -> object:
    """JSON-ready copy of `value` with every set as a sorted list.

    str(set) and set iteration order depend on PYTHONHASHSEED, so a set
    serialized as-is reads as a change on every restart.
    """
    if isinstance(value, (set, frozenset)):
        return sorted((canonical(v) for v in value), key=_sort_key)
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    return value


def canonical_legacy(value: object) -> object:
    """`value` with str(set) strings written by older versions replaced by `canonical` lists.

    Only strings shaped like a set repr are parsed, and only a set result is
    accepted; anything else stays a string. literal_eval evaluates no code.
    """
    if isinstance(value, dict):
        return {k: canonical_legacy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical_legacy(v) for v in value]
    if not isinstance(value, str):
        return value
    text = value
    if text.startswith("frozenset(") and text.endswith(")"):
        text = text[len("frozenset("):-1] or "set()"
    elif not (text == "set()" or (text.startswith("{") and text.endswith("}"))):
        return value
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return value
    if not isinstance(parsed, (set, frozenset)):
        return value
    return canonical(parsed)


def canonical_labels(labels: list[str] | None) -> list[str] | None:
    """Labels in the order the write path stores them (code point order)."""
    return None if labels is None else sorted(labels)
