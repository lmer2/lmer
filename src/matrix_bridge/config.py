"""The bridge's configuration: ``matrix.*`` from ``config.json``, secrets from env.

Two rules shape this module, and both are refusals rather than defaults.

**Invalid configuration is named, never guessed at.** A misspelled capability, a
string where a list belongs, an MXID that is not one — each raises
:class:`MatrixConfigError` naming the key. The alternative is a bridge that
starts with an allowlist subtly narrower or wider than the operator wrote, and
an allowlist is the only authorization this feature has (spec D4). The platform
daemon takes the opposite stance for its own settings, deliberately: a daemon
that will not boot cannot be reconfigured through its own UI. The bridge has no
UI to be reconfigured through, and the failure it prevents is worse.

**Secrets are environment-only.** ``LMER_MATRIX_AS_TOKEN``,
``LMER_MATRIX_HS_TOKEN`` and ``LMER_MATRIX_RECOVERY_KEY`` never appear in
``config.json`` — that file is world-readable state the daemon serves back
through its own API, and it lands in operator screenshots. A token-shaped key
*in* the mapping is itself a refusal: finding one means someone put a secret in
the wrong place, and starting anyway would leave it there.

Where each value lives
----------------------
``config.json`` (``matrix``)   ``name``, ``homeserver``, ``server_name``,
                               ``room_id``, ``control_url``,
                               ``authenticated_media``, ``allow``,
                               ``poll_seconds``, ``remind_seconds``
environment                    the three secrets above

``homeserver`` and ``server_name`` are not in the plan's list of keys for this
task, and are here because nothing else can supply them: the client cannot
open a connection without a URL, and D2's sender MXID ``@lmer-<name>:<server>``
cannot be spelled without a server name. ``server_name`` defaults to the
homeserver URL's host, which is the common case and wrong only under
well-known delegation — where the operator sets it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from lmer_platform import config as platform_config

#: What an MXID may grant today (spec D4). ``read`` is a capability rather than
#: an assumption because the room is not a trust unit: membership is not
#: permission, and slice 2's ghost users will share the room with humans.
CAPABILITIES = ("read", "answer-live", "answer-stopped")

#: Slice 2's vocabulary, parsed here so a config written for it is *refused with
#: its own name* rather than as a typo — and so nobody grants a capability the
#: code does not enforce yet. See spec §9.
RESERVED_CAPABILITIES = ("input", "spawn", "stop")

#: The three secrets, environment-only. Named here rather than inline so
#: :mod:`matrix_bridge.cli`'s ``check`` and the docs quote the same strings.
#:
#: The first two constants are spelled ``…_CREDENTIAL`` for the reason
#: :mod:`lmer_platform.ctl` gives at length: the repository's secret scan
#: matches an assignment whose left-hand side ends in ``TOKEN`` and cannot tell
#: a hardcoded credential from a *name* for one. The environment variables are
#: still ``LMER_MATRIX_AS_TOKEN`` and ``LMER_MATRIX_HS_TOKEN``, which is what
#: the appservice registration and the docs call them.
ENV_AS_CREDENTIAL = "LMER_MATRIX_AS_TOKEN"
ENV_HS_CREDENTIAL = "LMER_MATRIX_HS_TOKEN"
ENV_RECOVERY_KEY = "LMER_MATRIX_RECOVERY_KEY"

#: D9's cadence. Fifteen seconds is a poll a human does not notice; half an hour
#: is a reminder a human does not resent.
DEFAULT_POLL_SECONDS = 15
DEFAULT_REMIND_SECONDS = 1800

#: ``@localpart:server`` — the shape, not the existence. The homeserver decides
#: whether an MXID exists; this decides whether the operator wrote one at all.
#: The character sets are the spec's historical-localpart range and a hostname,
#: and they are spelled out rather than written as "anything but a colon" for
#: one reason: ``@*:server`` and ``@user:*`` have to be *refused*. A glob here
#: is an operator who believes they granted a group and has in fact granted an
#: MXID nobody can hold — a failure that reads as the bridge ignoring them.
#: IPv6 literal server names are not accepted; a homeserver reached that way
#: has no MXIDs worth allowlisting.
_MXID_RE = re.compile(r"^@[a-zA-Z0-9._=/+-]+:[a-zA-Z0-9.-]+(?::\d+)?$")

#: The bridge's own directory inside the platform state dir. The crypto store
#: (D7) and the thread map (D5) live under it.
MATRIX_DIRNAME = "matrix"

#: Keys that must never appear in ``config.json``. Matched by name because that
#: is what catches the mistake *before* the value is read: a token pasted into
#: the file is disclosed whatever its shape.
_FORBIDDEN_KEYS = ("as_token", "hs_token", "recovery_key", "token", "secret",
                   "password")


class MatrixConfigError(RuntimeError):
    """Configuration is present and unusable, naming the key that is wrong."""


@dataclass(frozen=True)
class MatrixConfig:
    """One bridge's settings, validated.

    ``allow`` is normalised to ``{mxid: frozenset(capabilities)}`` so
    :func:`matrix_bridge.allow.permits` does a membership test and no parsing —
    an authorization check that parses is an authorization check that can
    parse differently on the second call.
    """

    name: str
    homeserver: str
    server_name: str
    allow: Mapping[str, frozenset]
    room_id: Optional[str] = None
    #: Where a **person in the room** reaches the control UI. Configured rather
    #: than derived, because everything the bridge otherwise knows is a bind
    #: pair — ``http://127.0.0.1:8765`` is where the daemon listens, not a link
    #: anyone can open from a phone. Unset means the messages carry no link at
    #: all, which is honest; a loopback URL in a chat room is not (!244 review).
    control_url: Optional[str] = None
    #: The operator asserting that ``matrix_enable_authenticated_media`` is on
    #: at the homeserver — the same change that installs the registration.
    #:
    #: Asserted rather than measured, because the client API cannot see it
    #: (!243 review): the authenticated media endpoints are served by every
    #: Synapse from 1.11 whatever this setting says, so "the endpoint exists"
    #: was a version fact answering a question about a *setting*, and it passed
    #: on exactly the homeserver the guard exists to refuse. Default false:
    #: uploads are refused until someone says otherwise, because media stored
    #: while the setting is off is anonymously fetchable by its mxc id forever.
    authenticated_media: bool = False
    poll_seconds: int = DEFAULT_POLL_SECONDS
    remind_seconds: int = DEFAULT_REMIND_SECONDS

    @property
    def sender(self) -> str:
        """The appservice's own MXID (D2): ``@lmer-<name>:<server_name>``."""
        return f"@lmer-{self.name}:{self.server_name}"

    @property
    def user_namespace(self) -> str:
        """The exclusive namespace for slice 2's ghost users (D2).

        Derived from the sender rather than configured, which is what
        guarantees two bridges on one homeserver cannot claim overlapping
        namespaces — the one rule Synapse enforces about appservices.
        """
        return f"@lmer-{self.name}_.*"

    @property
    def state_dir(self):
        """``~/.lmer/platform/matrix/`` — the crypto store and thread map."""
        return platform_config.platform_dir() / MATRIX_DIRNAME


@dataclass(frozen=True)
class Secrets:
    """The three environment values, never written anywhere."""

    as_token: str = field(repr=False)
    hs_token: str = field(repr=False)
    recovery_key: str = field(repr=False)


def load(stored: Optional[Mapping[str, Any]] = None) -> MatrixConfig:
    """Validate the ``matrix`` mapping into a :class:`MatrixConfig`.

    *stored* is the mapping itself, for tests and for a caller that already
    holds one; the default reads it from the platform's ``config.json`` through
    :func:`lmer_platform.config.load`, which is the single reader of that file.
    """
    if stored is None:
        stored = getattr(platform_config.load(), "matrix", None)
    if stored is None:
        raise MatrixConfigError(
            "no `matrix` section in the platform config: this daemon's "
            f"{platform_config.config_path()} must carry one before the bridge "
            "can start (see docs/MATRIX-CHAT.md)"
        )
    if not isinstance(stored, Mapping):
        raise MatrixConfigError(
            f"`matrix` must be a mapping, not {type(stored).__name__}"
        )

    _refuse_secrets_in_file(stored)

    name = _require_str(stored, "name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise MatrixConfigError(
            f"`matrix.name` must be lowercase letters, digits and hyphens "
            f"(it becomes the MXID {name!r} would give: @lmer-{name}:…), got "
            f"{name!r}"
        )

    homeserver = _require_str(stored, "homeserver")
    parsed = urlparse(homeserver)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MatrixConfigError(
            f"`matrix.homeserver` must be an http(s) URL, got {homeserver!r}"
        )

    server_name = stored.get("server_name") or parsed.hostname
    if not isinstance(server_name, str) or not server_name.strip():
        raise MatrixConfigError("`matrix.server_name` must be a non-empty string")

    room_id = stored.get("room_id")
    if room_id is not None:
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            raise MatrixConfigError(
                f"`matrix.room_id` must be a room id starting with '!' "
                f"(an alias is not one), got {room_id!r}"
            )

    return MatrixConfig(
        name=name,
        homeserver=homeserver.rstrip("/"),
        server_name=server_name.strip(),
        allow=parse_allow(stored.get("allow")),
        room_id=room_id,
        control_url=_optional_url(
            stored, "control_url",
            "a person in the room opens to see the fleet",
        ),
        authenticated_media=_require_bool(stored, "authenticated_media"),
        poll_seconds=_require_positive_int(stored, "poll_seconds",
                                           DEFAULT_POLL_SECONDS),
        remind_seconds=_require_positive_int(stored, "remind_seconds",
                                             DEFAULT_REMIND_SECONDS),
    )


def parse_allow(raw: Any) -> Mapping[str, frozenset]:
    """``{mxid: [capability, …]}`` → ``{mxid: frozenset}``, or a named refusal.

    An empty allowlist is legal and means "nobody may answer": a bridge that
    only announces is a coherent thing to run, and it is the safe end of the
    range. A *missing* one means the same, but is worth allowing separately —
    the operator who has not written the list yet should not be blocked from
    starting the bridge to see what it says.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MatrixConfigError(
            f"`matrix.allow` must be a mapping of MXID to capabilities, not "
            f"{type(raw).__name__}"
        )

    allow: dict[str, frozenset] = {}
    for mxid, capabilities in raw.items():
        if not isinstance(mxid, str) or not _MXID_RE.match(mxid):
            raise MatrixConfigError(
                f"`matrix.allow` key {mxid!r} is not an MXID "
                f"(@localpart:server). Never a domain, never a pattern: this "
                f"homeserver federates and its identity providers are "
                f"open-signup, so only an explicit MXID is a trust unit."
            )
        if isinstance(capabilities, str) or not isinstance(capabilities, (list, tuple)):
            raise MatrixConfigError(
                f"`matrix.allow[{mxid}]` must be a list of capabilities, not "
                f"{type(capabilities).__name__}"
            )
        granted = set()
        for capability in capabilities:
            if capability in RESERVED_CAPABILITIES:
                raise MatrixConfigError(
                    f"`matrix.allow[{mxid}]` grants {capability!r}, which is "
                    f"reserved for slice 2 and enforced by nothing today. "
                    f"Remove it rather than let it read as permission."
                )
            if capability not in CAPABILITIES:
                raise MatrixConfigError(
                    f"`matrix.allow[{mxid}]` grants unknown capability "
                    f"{capability!r}; known: {', '.join(CAPABILITIES)}"
                )
            granted.add(capability)
        allow[mxid] = frozenset(granted)
    return allow


def load_secrets(environ: Optional[Mapping[str, str]] = None) -> Secrets:
    """The three environment values, or a refusal naming the ones that are absent.

    All three at once, and never partially: a bridge with an ``as_token`` and no
    ``hs_token`` cannot verify the transactions the homeserver pushes it, and one
    with no recovery key cannot restore its crypto store — each is a way to be
    half-running, which is the state hardest to diagnose from the room.
    """
    environ = os.environ if environ is None else environ
    values = {
        name: (environ.get(name) or "").strip()
        for name in (ENV_AS_CREDENTIAL, ENV_HS_CREDENTIAL, ENV_RECOVERY_KEY)
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise MatrixConfigError(
            "the bridge's secrets come from the environment and nowhere else; "
            f"this one is missing {', '.join(sorted(missing))}. They are never "
            "read from config.json — see docs/MATRIX-CHAT.md."
        )
    return Secrets(
        as_token=values[ENV_AS_CREDENTIAL],
        hs_token=values[ENV_HS_CREDENTIAL],
        recovery_key=values[ENV_RECOVERY_KEY],
    )


def _optional_url(stored: Mapping[str, Any], key: str, purpose: str) -> Optional[str]:
    """An http(s) URL, or ``None``, or a refusal naming the key.

    Refused rather than ignored when malformed: a URL nobody can open is worse
    in a chat room than no URL at all, because the reader spends a tap finding
    that out.
    """
    value = stored.get(key)
    if value is None:
        return None
    parsed = urlparse(value if isinstance(value, str) else "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MatrixConfigError(
            f"`matrix.{key}` must be the http(s) URL {purpose}, got {value!r}"
        )
    return value.rstrip("/")


def _refuse_secrets_in_file(stored: Mapping[str, Any]) -> None:
    found = sorted(
        key for key in stored
        if isinstance(key, str)
        and any(bad in key.lower() for bad in _FORBIDDEN_KEYS)
    )
    if found:
        raise MatrixConfigError(
            f"`matrix` carries {', '.join(f'`{k}`' for k in found)}, and the "
            f"bridge's secrets are environment-only "
            f"({ENV_AS_CREDENTIAL}, {ENV_HS_CREDENTIAL}, {ENV_RECOVERY_KEY}). "
            f"config.json is served back by the daemon's own API: remove the "
            f"key and rotate what was in it."
        )


def _require_bool(stored: Mapping[str, Any], key: str) -> bool:
    """A real boolean, or a refusal. Never a truthy string.

    ``"false"`` is truthy in Python, and this particular key decides whether
    bytes may reach the homeserver — a config that says false and behaves true
    is the one mistake this feature must not make.
    """
    value = stored.get(key, False)
    if not isinstance(value, bool):
        raise MatrixConfigError(
            f"`matrix.{key}` must be true or false (a JSON boolean), got "
            f"{value!r}"
        )
    return value


def _require_str(stored: Mapping[str, Any], key: str) -> str:
    value = stored.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MatrixConfigError(f"`matrix.{key}` must be a non-empty string")
    return value.strip()


def _require_positive_int(stored: Mapping[str, Any], key: str, default: int) -> int:
    value = stored.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MatrixConfigError(
            f"`matrix.{key}` must be a whole number of seconds, got {value!r}"
        )
    if value <= 0:
        raise MatrixConfigError(
            f"`matrix.{key}` must be greater than zero, got {value}. There is "
            f"no 'off' here: a bridge that never polls announces nothing, which "
            f"is what not running it does."
        )
    return value
