"""The time-tracking settings a person can change without editing ``config.json``
(issue #668, docs/TIME-TRACKING.md#settings): the person registry, the
``time_tracking`` switch, ``default_person`` and the platform-wide time defaults.

Each edit is a function from the stored ``config.json`` mapping to the changes
to store, run inside :func:`lmer_platform.config.mutate_stored`'s lock. A
result the daemon would reject at its next boot is refused here, naming every
problem, so a write can never turn tracking off by surprise.
"""

from __future__ import annotations

from typing import Optional

from .config import (
    TIME_INT_SETTINGS, TIME_SETTING_KEYS, TIME_STR_SETTINGS, _int_setting_reason, _time_text_reason,
    time_settings,
)
from .people import _TRANSPORTS, parse_registry

__all__ = ["SettingsRefused", "DEFAULT_KEYS", "add_person", "edit_person", "remove_person",
           "patch_defaults", "map_identity", "parse_identity", "view"]

#: Request key -> ``config.json`` field, for :func:`patch_defaults`.
DEFAULT_KEYS = {
    "tracking": "time_tracking",
    "default_person": "default_person",
    **{key: spec[0] for key, spec in TIME_SETTING_KEYS.items()},
}
_PERSON_KEYS = ("key", "name", "slack", "matrix")


class SettingsRefused(Exception):
    """A refused settings edit: ``status``, ``code``, ``message`` and ``problems``."""

    def __init__(self, status: int, code: str, message: str, problems=()) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.problems = list(problems)


def _stored_people(current: dict) -> list:
    people = current.get("people")
    if people is None:
        return []
    if not isinstance(people, list):
        raise SettingsRefused(409, "time_settings_unreadable",
                              "people in config.json is not a list; fix the file by hand first.")
    return [dict(entry) if isinstance(entry, dict) else entry for entry in people]


def _checked(people: list, default_person: object) -> dict:
    """``{"people": people}``, refused when the registry would not parse."""
    _persons, _default, problems = parse_registry(people, default_person)
    if problems:
        raise SettingsRefused(400, "time_settings_invalid", "; ".join(problems), problems)
    return {"people": people}


def _index(people: list, key: str) -> int:
    for index, entry in enumerate(people):
        if isinstance(entry, dict) and entry.get("key") == key:
            return index
    raise SettingsRefused(404, "time_person_unknown", f"No person {key!r} is listed.")


def _ids(body: dict, transport: str) -> Optional[list]:
    if transport not in body:
        return None
    value = body[transport]
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SettingsRefused(400, "time_settings_invalid", f"{transport} must be a list of ids.")
    return [item.strip() for item in value if item.strip()]


def parse_identity(text: object) -> tuple:
    """``(transport, raw_id)`` from ``slack:U…`` or ``matrix:@user:server``."""
    if isinstance(text, str) and ":" in text:
        transport, raw = text.split(":", 1)
        if transport in _TRANSPORTS and raw.strip():
            return transport, raw.strip()
    raise SettingsRefused(400, "time_settings_invalid",
                          f"{text!r} is not an identity: write slack:<user id> or matrix:<mxid>.")


def _person(body: dict, *, key: str) -> dict:
    unknown = set(body) - set(_PERSON_KEYS) - {"add"}
    if unknown:
        raise SettingsRefused(400, "time_settings_invalid",
                              f"unknown person field(s): {', '.join(sorted(unknown))}")
    entry = {"key": key}
    name = body.get("name")
    if name is not None:
        if not isinstance(name, str):
            raise SettingsRefused(400, "time_settings_invalid", "name must be text.")
        if name.strip():
            entry["name"] = name.strip()
    for transport in _TRANSPORTS:
        ids = _ids(body, transport)
        if ids:
            entry[transport] = ids
    return entry


def add_person(current: dict, body: dict) -> dict:
    """Add the person *body* describes (``key``, ``name``, ``slack``, ``matrix``)."""
    if not isinstance(body, dict):
        raise SettingsRefused(400, "time_settings_invalid", "send the person as an object.")
    people = _stored_people(current)
    key = body.get("key")
    key = key.strip() if isinstance(key, str) else key
    if any(isinstance(entry, dict) and entry.get("key") == key for entry in people):
        raise SettingsRefused(409, "time_person_exists", f"A person {key!r} is already listed.")
    sole = [entry.get("key") for entry in people if isinstance(entry, dict)]
    people.append(_person(body, key=key))
    changes = _checked(people, current.get("default_person"))
    if len(sole) == 1 and not current.get("default_person"):
        # One listed person is the default without being named; a second would end
        # that silently and leave the operator credential acting for nobody.
        changes["default_person"] = sole[0]
    return changes


def edit_person(current: dict, key: str, body: dict) -> dict:
    """Change person *key*'s name or identity lists; ``add`` appends identities.

    A list given replaces the stored one, and a field left out is kept. An
    identity added here moves from any other person who had it, so one click
    on an unmapped id can never leave the registry unusable.
    """
    if not isinstance(body, dict) or "key" in body:
        raise SettingsRefused(400, "time_settings_invalid",
                              "send the fields to change; a person's key cannot be changed.")
    people = _stored_people(current)
    index = _index(people, key)
    entry = dict(people[index])
    changed = _person(body, key=key)
    if "name" in body:
        entry.pop("name", None)
        entry.update({"name": changed["name"]} if "name" in changed else {})
    for transport in _TRANSPORTS:
        if transport in body:
            entry.pop(transport, None)
            if changed.get(transport):
                entry[transport] = changed[transport]
    for item in body.get("add") or ():
        transport, raw = parse_identity(item)
        for other in people:
            if isinstance(other, dict) and other is not people[index] and raw in (other.get(transport) or ()):
                other[transport] = [value for value in other[transport] if value != raw]
        if raw not in (entry.get(transport) or ()):
            entry[transport] = [*(entry.get(transport) or ()), raw]
    people[index] = entry
    return _checked(people, current.get("default_person"))


def remove_person(current: dict, key: str) -> dict:
    """Remove person *key*; a ``default_person`` that named them is cleared."""
    people = _stored_people(current)
    people.pop(_index(people, key))
    changes = {}
    default = current.get("default_person")
    if default == key:
        default = None
        changes["default_person"] = None
    return {**_checked(people, default), **changes}


def patch_defaults(current: dict, body: dict) -> dict:
    """The ``config.json`` changes for *body* (:data:`DEFAULT_KEYS`); ``null``
    or ``""`` clears a key so the layer below shows through."""
    if not isinstance(body, dict) or not body:
        raise SettingsRefused(400, "time_settings_invalid",
                              f"name at least one setting: {', '.join(DEFAULT_KEYS)}")
    unknown = set(body) - set(DEFAULT_KEYS)
    if unknown:
        raise SettingsRefused(400, "time_settings_invalid",
                              f"unknown time setting(s) {', '.join(sorted(unknown))}: "
                              f"expected {', '.join(DEFAULT_KEYS)}")
    changes = {}
    for key, value in body.items():
        field = DEFAULT_KEYS[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            changes[field] = None
            continue
        if field == "time_tracking":
            if not isinstance(value, bool):
                raise SettingsRefused(400, "time_settings_invalid", "tracking must be true or false.")
            changes[field] = value
        elif field == "default_person":
            changes[field] = value.strip() if isinstance(value, str) else value
        elif field in TIME_INT_SETTINGS:
            changes[field] = _int_value(field, value)
        else:
            reason = _time_text_reason(value, field=field)
            if reason:
                raise SettingsRefused(400, "time_settings_invalid", f"{key}: {reason}")
            changes[field] = value.strip()
    if "default_person" in changes:
        _checked(_stored_people(current), changes["default_person"])
    return changes


def _int_value(field: str, value: object) -> int:
    if isinstance(value, bool):
        raise SettingsRefused(400, "time_settings_invalid", f"{field} must be a whole number.")
    try:
        number = int(str(value).strip()) if not isinstance(value, int) else value
    except ValueError:
        raise SettingsRefused(400, "time_settings_invalid", f"{field} must be a whole number.") from None
    reason = _int_setting_reason(number, field=field)
    if reason:
        raise SettingsRefused(400, "time_settings_invalid", f"{field}: {reason}")
    return number


def map_identity(current: dict, identity: tuple, *, key: Optional[str], name: Optional[str]) -> tuple:
    """``(changes, person_key, created)`` for an assistant mapping the asker.

    *identity* is ``(transport, raw_id)``, taken by the caller from the request
    or record, never from the body. An identity already listed may only rename
    its own person. A new one becomes a new person *key*; a key another person
    holds is refused, so an asker can never attach themselves to someone else.
    """
    transport, raw = identity
    people = _stored_people(current)
    owner = next((entry for entry in people if isinstance(entry, dict)
                  and raw in (entry.get(transport) or ())), None)
    if owner is not None:
        if key is not None and key != owner.get("key"):
            raise SettingsRefused(409, "time_identity_listed",
                                  f"{transport}:{raw} is already listed as {owner.get('key')!r}.")
        if name is None:
            return {}, owner["key"], False
        changes = edit_person(current, owner["key"], {"name": name})
        return changes, owner["key"], False
    if not people:
        raise SettingsRefused(409, "time_registry_empty",
                              "Nobody is listed yet, and the first person listed is the one the "
                              "operator credential acts for: the operator lists them in Platform "
                              "settings → Time tracking.")
    if not key:
        raise SettingsRefused(400, "time_settings_invalid",
                              "name the person key to list this identity under (key).")
    body = {"key": key, transport: [raw]}
    if name is not None:
        body["name"] = name
    return add_person(current, body), key.strip(), True


def view(config, stored: dict) -> dict:
    """``GET /api/time/settings`` without the project and unmapped parts."""
    resolved = time_settings()
    defaults = {}
    for key, (field, env) in TIME_SETTING_KEYS.items():
        setting = resolved[key]
        defaults[key] = {
            "value": setting.value, "source": setting.source, "stored": stored.get(field),
            "env": env, "kind": "int" if field in TIME_INT_SETTINGS else "text",
            "default": (TIME_INT_SETTINGS[field].default if field in TIME_INT_SETTINGS
                        else TIME_STR_SETTINGS[field]),
        }
        if field in TIME_INT_SETTINGS:
            defaults[key]["minimum"] = TIME_INT_SETTINGS[field].minimum
    _persons, effective, _problems = parse_registry(config.people, config.default_person)
    return {
        "tracking": {"switch": config.time_tracking is True, "runs": config.time_tracking_runs,
                     "problems": list(config.time_problems)},
        "people": list(config.people) if isinstance(config.people, list) else [],
        "default_person": {"stored": stored.get("default_person"), "effective": effective},
        "defaults": defaults,
    }
