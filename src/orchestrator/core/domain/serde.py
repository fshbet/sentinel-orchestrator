"""Dependency-free (de)serialisation for the dataclass domain model.

The core deliberately avoids a validation library (ADR-011), so this module
supplies the small amount of reflection needed to round-trip dataclasses
through JSON: enums become their string value, ``datetime`` becomes ISO-8601,
and typed fields are reconstructed on the way back in.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import types
import typing
from enum import Enum
from typing import Any, TypeVar, get_args, get_origin

T = TypeVar("T")

_TYPE_HINT_CACHE: dict[type, dict[str, Any]] = {}


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def to_jsonable(value: Any) -> Any:
    """Convert domain objects into JSON-serialisable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.UTC)
        return value.astimezone(_dt.UTC).isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.metadata.get("transient") is not True
        }
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return str(value)


def _hints(cls: type) -> dict[str, Any]:
    cached = _TYPE_HINT_CACHE.get(cls)
    if cached is None:
        import sys

        module = sys.modules.get(cls.__module__)
        globalns = vars(module) if module else {}
        cached = typing.get_type_hints(cls, globalns=globalns)
        _TYPE_HINT_CACHE[cls] = cached
    return cached


def _coerce(hint: Any, value: Any) -> Any:
    if hint is Any or hint is None:
        return value

    origin = get_origin(hint)

    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        if value is None:
            return None
        # Try each member; fall back to the raw value rather than guessing.
        for arg in args:
            try:
                return _coerce(arg, value)
            except Exception:  # noqa: BLE001, S112 - deliberate best-effort union walk
                continue
        return value

    if origin in (list, set, frozenset, tuple):
        (item_hint,) = get_args(hint) or (Any,)
        items = [_coerce(item_hint, v) for v in (value or [])]
        if origin is set:
            return set(items)
        if origin is frozenset:
            return frozenset(items)
        if origin is tuple:
            return tuple(items)
        return items

    if origin is dict:
        dict_args = get_args(hint) or (str, Any)
        key_hint, val_hint = dict_args[0], dict_args[1]
        return {
            _coerce(key_hint, k): _coerce(val_hint, v) for k, v in (value or {}).items()
        }

    if isinstance(hint, type):
        if issubclass(hint, Enum):
            return hint(value)
        if issubclass(hint, _dt.datetime):
            if isinstance(value, _dt.datetime):
                return value
            parsed = _dt.datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.UTC)
            return parsed
        if dataclasses.is_dataclass(hint):
            return from_dict(hint, value)

    return value


def from_dict(cls: type[T], data: dict[str, Any] | None) -> T:
    """Rebuild a dataclass instance from a plain dict, ignoring unknown keys."""
    if data is None:
        raise ValueError(f"cannot build {cls.__name__} from None")
    if isinstance(data, cls):  # already the right thing
        return data
    hints = _hints(cls)
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):  # type: ignore[arg-type]
        if field.name not in data:
            continue
        kwargs[field.name] = _coerce(hints.get(field.name, Any), data[field.name])
    return cls(**kwargs)  # type: ignore[return-value]
