"""Strict validation of Trading 212 JSON into typed records (lab/t212/INTEGRATION_SPEC.md, "Validation").

A response is accepted only if it matches the documented schema: every required field present with its documented JSON type, optional fields absent, null or of
their type, enumerations limited to their documented values, timestamps carrying a UTC offset, numbers finite, and no field the schema does not document.
Nothing is coerced (a quantity sent as the string "1" is an error) and nothing is defaulted. Each failure is a specific SchemaError subclass whose `path`
locates the offending value, `$` being the response body:

    MissingField       a required field is absent or null                 $.items[3].instrument.ticker: required field is absent
    UnexpectedField    a field the schema does not document               $.cash: undocumented field(s) ['bonus']
    WrongType          a value of another JSON type                       $.quantity: expected number, got string
    BadValue           a value outside the documented domain              $.status: 'DONE' is not a documented value
    DuplicateKey       an object that repeats a key (JSON parsers would keep the last one silently)
    NotJson            a body that is not UTF-8 JSON, or that holds NaN or Infinity
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Callable, Collection
from typing import NoReturn, TypeVar

T = TypeVar("T")
CURRENCY = re.compile(r"[A-Z]{3}")                          # ISO 4217 alphabetic code (Trading 212 also uses GBX, pence, which has the same shape)
SHOWN_CHOICES = 12                                          # documented values listed in a BadValue message before it is cut short


class SchemaError(ValueError):
    """A JSON value that does not match the documented schema; `path` locates it (`$` is the response body)."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


class MissingField(SchemaError):
    """A required field is absent or null."""


class UnexpectedField(SchemaError):
    """An object carries a field the schema does not document."""


class WrongType(SchemaError):
    """A value has another JSON type than the documented one."""


class BadValue(SchemaError):
    """A value of the right type outside the documented domain: an undocumented enumeration value, a date-time without offset, an empty identifier, fields
    that contradict each other."""


class DuplicateKey(SchemaError):
    """An object repeats a key."""


class NotJson(SchemaError):
    """The body is not UTF-8 JSON, or holds NaN or Infinity (not JSON, although Python's parser accepts them)."""


def json_type(value: object) -> str:
    """The JSON type name of a decoded value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise DuplicateKey("$", f"an object repeats the key {k!r}")
        out[k] = v
    return out


def _no_constants(name: str) -> NoReturn:
    raise NotJson("$", f"{name} is not a JSON number")


def loads(body: bytes) -> object:
    """The JSON value of a response body. Refuses a body that is not UTF-8, is not JSON, holds NaN or Infinity, or repeats a key in an object."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise NotJson("$", f"body is not UTF-8: {e.reason} at byte {e.start}") from e
    try:
        return json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constants)
    except json.JSONDecodeError as e:
        raise NotJson("$", f"body is not JSON: {e.msg} at line {e.lineno} column {e.colno}") from e


def as_text(value: object, path: str) -> str:
    """A non-empty string (an empty identifier, name or currency is as good as a missing one)."""
    if not isinstance(value, str):
        raise WrongType(path, f"expected string, got {json_type(value)}")
    if not value:
        raise BadValue(path, "empty string")
    return value


def as_number(value: object, path: str) -> float:
    """A finite JSON number as a float. A boolean is not a number here, although Python counts it as one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WrongType(path, f"expected number, got {json_type(value)}")
    try:
        out = float(value)
    except OverflowError as e:                              # an integer literal too large for a double
        raise BadValue(path, "number out of range") from e
    if not math.isfinite(out):
        raise BadValue(path, "number is not finite")
    return out


def as_integer(value: object, path: str) -> int:
    """A JSON number written as an integer (identifiers): 3.0 is refused, as is any boolean."""
    if isinstance(value, float):
        raise WrongType(path, f"expected integer, got number {value!r} (written with a fraction or an exponent)")
    if isinstance(value, bool) or not isinstance(value, int):
        raise WrongType(path, f"expected integer, got {json_type(value)}")
    return value


def as_flag(value: object, path: str) -> bool:
    """A JSON boolean."""
    if not isinstance(value, bool):
        raise WrongType(path, f"expected boolean, got {json_type(value)}")
    return value


def as_timestamp(value: object, path: str) -> dt.datetime:
    """An ISO 8601 date-time with a UTC offset, as an aware datetime in UTC (sub-microsecond digits are truncated by the parser). A date-time without an offset
    names no instant and is refused."""
    text = as_text(value, path)
    try:
        t = dt.datetime.fromisoformat(text)
    except ValueError as e:
        raise BadValue(path, f"not an ISO 8601 date-time: {text!r}") from e
    if t.utcoffset() is None:
        raise BadValue(path, f"date-time without a UTC offset: {text!r}")
    return t.astimezone(dt.UTC)


def as_date(value: object, path: str) -> dt.date:
    """An ISO 8601 calendar date (YYYY-MM-DD)."""
    text = as_text(value, path)
    try:
        return dt.date.fromisoformat(text)
    except ValueError as e:
        raise BadValue(path, f"not an ISO 8601 date: {text!r}") from e


def as_number_map(value: object, path: str) -> dict[str, float]:
    """An object whose values are all finite numbers, keyed by non-empty strings (a {ticker: shares} holding)."""
    if not isinstance(value, dict):
        raise WrongType(path, f"expected object, got {json_type(value)}")
    return {as_text(k, path): as_number(v, f"{path}.{k}") for k, v in value.items()}


def as_currency(value: object, path: str) -> str:
    """A three-letter currency code."""
    text = as_text(value, path)
    if not CURRENCY.fullmatch(text):
        raise BadValue(path, f"not a three-letter currency code: {text!r}")
    return text


def as_choice(value: object, path: str, allowed: Collection[str]) -> str:
    """A string among the documented values of an enumeration."""
    text = as_text(value, path)
    if text not in allowed:
        shown = sorted(allowed)
        listed = ", ".join(shown[:SHOWN_CHOICES]) + (f", ... ({len(shown)} in all)" if len(shown) > SHOWN_CHOICES else "")
        raise BadValue(path, f"{text!r} is not a documented value ({listed})")
    return text


class Fields:
    """Reads one JSON object field by field. Each accessor checks presence, type and domain and marks the field read; `close` then refuses every field that
    no accessor read. Accessors ending in `_or_none` accept an absent or null field (None); the others require a non-null value."""

    def __init__(self, value: object, path: str) -> None:
        if not isinstance(value, dict):
            raise WrongType(path, f"expected object, got {json_type(value)}")
        self._obj: dict[str, object] = value
        self.path = path
        self._read: set[str] = set()

    def at(self, name: str) -> str:
        """The JSON path of field `name` of this object."""
        return f"{self.path}.{name}"

    def _value(self, name: str, optional: bool) -> object:
        self._read.add(name)
        value = self._obj.get(name)
        if value is None and not optional:
            raise MissingField(self.at(name), "required field is absent" if name not in self._obj else "required field is null")
        return value

    def require_key(self, name: str) -> None:
        """Refuse an object without key `name`, even where its value may be null (a field documented as always present)."""
        if name not in self._obj:
            raise MissingField(self.at(name), "required field is absent")

    def text(self, name: str) -> str:
        return as_text(self._value(name, False), self.at(name))

    def text_or_none(self, name: str) -> str | None:
        v = self._value(name, True)
        return None if v is None else as_text(v, self.at(name))

    def number(self, name: str) -> float:
        return as_number(self._value(name, False), self.at(name))

    def number_or_none(self, name: str) -> float | None:
        v = self._value(name, True)
        return None if v is None else as_number(v, self.at(name))

    def integer(self, name: str) -> int:
        return as_integer(self._value(name, False), self.at(name))

    def integer_or_none(self, name: str) -> int | None:
        v = self._value(name, True)
        return None if v is None else as_integer(v, self.at(name))

    def flag(self, name: str) -> bool:
        return as_flag(self._value(name, False), self.at(name))

    def timestamp(self, name: str) -> dt.datetime:
        return as_timestamp(self._value(name, False), self.at(name))

    def date_or_none(self, name: str) -> dt.date | None:
        v = self._value(name, True)
        return None if v is None else as_date(v, self.at(name))

    def number_map(self, name: str) -> dict[str, float]:
        return as_number_map(self._value(name, False), self.at(name))

    def number_map_or_none(self, name: str) -> dict[str, float] | None:
        v = self._value(name, True)
        return None if v is None else as_number_map(v, self.at(name))

    def value(self, name: str) -> object:
        """A required field taken as it is (any JSON type but null), for a value read by its own parser later (a broker answer an engine record holds)."""
        return self._value(name, False)

    def skip(self, *names: str) -> None:
        """Accept these fields, if present, without reading them: documented fields recorded for information only."""
        self._read.update(names)

    def currency(self, name: str) -> str:
        return as_currency(self._value(name, False), self.at(name))

    def choice(self, name: str, allowed: Collection[str]) -> str:
        return as_choice(self._value(name, False), self.at(name), allowed)

    def choice_or_none(self, name: str, allowed: Collection[str]) -> str | None:
        v = self._value(name, True)
        return None if v is None else as_choice(v, self.at(name), allowed)

    def record(self, name: str, parse: Callable[[Fields], T]) -> T:
        return parse_object(self._value(name, False), parse, self.at(name))

    def record_or_none(self, name: str, parse: Callable[[Fields], T]) -> T | None:
        v = self._value(name, True)
        return None if v is None else parse_object(v, parse, self.at(name))

    def items(self, name: str, parse: Callable[[Fields], T]) -> tuple[T, ...]:
        """A required array of objects, each read with `parse`."""
        return parse_array(self._value(name, False), parse, self.at(name))

    def fail(self, message: str) -> NoReturn:
        """Refuse this object for a reason that involves several fields (BadValue at the object's path)."""
        raise BadValue(self.path, message)

    def close(self) -> None:
        """Refuse the object if it has fields no accessor read."""
        extra = sorted(set(self._obj) - self._read)
        if extra:
            raise UnexpectedField(self.path, f"undocumented field(s) {extra}")


def parse_object(value: object, parse: Callable[[Fields], T], path: str = "$") -> T:
    """One JSON object read by `parse`, then closed: a field `parse` did not read is refused."""
    fields = Fields(value, path)
    out = parse(fields)
    fields.close()
    return out


def parse_array(value: object, parse: Callable[[Fields], T], path: str = "$") -> tuple[T, ...]:
    """A JSON array of objects, each read by `parse` and closed."""
    if not isinstance(value, list):
        raise WrongType(path, f"expected array, got {json_type(value)}")
    return tuple(parse_object(v, parse, f"{path}[{i}]") for i, v in enumerate(value))
