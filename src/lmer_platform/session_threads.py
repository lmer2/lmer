"""A run's own chat threads: opening one, connecting one, letting one go (#429).

Two doors into the same work. The platform's route is one — the UI's thread menu
and ``lmer-ctl threads`` behind it — and the run itself is the other, asking its
host side over the servicer because the Slack and matrix credentials are there
and never in its container (docs/COMMS.md §12, "Who actually does it"). One
implementation, so a connect made from a container and one made from the UI
cannot come to mean different things.

A connect is three steps rather than one, which is why this module exists at
all: uber lmer's standing launch list has to be checked *before* the connection
is written, the connection is :mod:`lmer_platform.connectors`'s, and that
standing list has to be moved afterwards so a thread connected from the chat
pane survives a rotation. Opening adds a root message in front of those; the
transports are :func:`lmer_cli.thread_access.open_thread`'s half, and what
belongs here is the root's own text, which names the run.

``docs/COMMS.md`` §9 has the operator-facing shape and the refusals.
"""
from __future__ import annotations

import logging
from typing import Optional

from lmer_cli import runref, thread_access
from lmer_cli.comms import assistant_display_name
from lmer_cli.thread_posts import Poster
from lmer_cli.threads import ThreadError

from . import config as cfg
from . import connectors, meta, projects, registry, runs
from .projects import session_operation
from . import project_threads

logger = logging.getLogger(__name__)

__all__ = [
    "BY_AGENT", "SessionThreads", "connect", "disconnect", "disconnect_link",
    "open_for", "root_message", "run_label", "speaker_label", "assistant_label",
]

#: What a connection made by the run itself is recorded as having been made by.
#: The bus's own name for the session's side of a conversation, so a record's
#: ``by`` reads the same as the source tag of everything else it said.
BY_AGENT = "agent"


@session_operation
def connect(session_id: str, permalink: object, *, by: str) -> dict:
    """Connect *session_id* to the thread *permalink* names, and record it."""
    from . import assistant

    component = connectors.ensure_connectors()
    try:
        # Before the connect, not after: on the assistant this write also extends
        # its standing launch list, and a list that goes over the bound is
        # *tolerated* on the way into config.json and then resolves to nothing at
        # all (lmer_platform.assistant.refuse_unusable_thread_list).
        assistant.refuse_unusable_thread_list(session_id, permalink)
    except assistant.AssistantError as exc:
        raise _refused(exc) from None
    record = component.connect(session_id, permalink, by=by)
    project_threads.remember(session_id, record["permalink"])
    assistant.note_thread_change(session_id, record["permalink"], connected=True)
    return record


def _refused(exc: Exception) -> ThreadError:
    """An assistant refusal as the thread refusal every caller here raises."""
    return ThreadError(str(exc), status=getattr(exc, "status", 400))


@session_operation
def disconnect(session_id: str, thread_id: str) -> dict:
    """Stop mirroring one thread, by the id a listing reports."""
    from . import assistant

    record = connectors.ensure_connectors().disconnect(session_id, thread_id)
    assistant.note_thread_change(session_id, record["permalink"], connected=False)
    return record


@session_operation
def disconnect_link(session_id: str, permalink: object) -> dict:
    """The same, by permalink — which is what a run holds rather than an id.

    The link is resolved the way a connect resolves it, since that resolution is
    what the connection was keyed by: a matrix alias and a Slack link naming a
    message inside the thread otherwise address nothing.
    """
    component = connectors.ensure_connectors()
    ref = component.prepare(permalink, checks=False)
    if not any(record["id"] == ref.id for record in component.records(session_id)):
        raise ThreadError(
            f"this run is not connected to {ref.permalink}", status=404,
        )
    return disconnect(session_id, ref.id)


@session_operation
def open_for(
    session_id: str,
    channel: object,
    *,
    by: str,
    title: object = None,
    env: Optional[dict] = None,
    where: str = thread_access.DAEMON_ENVIRONMENT,
    poster: Optional[Poster] = None,
) -> dict:
    """Open a thread in *channel* for *session_id*, and connect it.

    One step for the caller and two writes underneath, in an order the failures
    decide: **everything** refusable without touching Slack or the homeserver is
    refused first, because a root message in a channel cannot be unposted — the
    control plane, this run's own thread bound (:meth:`require_room`), and on
    uber lmer the room in its standing launch list, an independent bound that
    was reached only after the post until !299's review measured it.
    """
    from . import assistant

    component = connectors.ensure_connectors()
    component.require_room(session_id)
    try:
        assistant.refuse_full_thread_list(session_id)
    except assistant.AssistantError as exc:
        raise _refused(exc) from None
    opened = thread_access.open_thread(
        channel, root_message(session_id, title), env=env, where=where,
        poster=poster,
    )
    permalink = opened["thread"]["permalink"]
    try:
        return connect(session_id, permalink, by=by)
    except ThreadError as exc:
        raise ThreadError(
            f"the thread is open at {permalink} — the root message is posted — "
            f"but connecting this run to it failed: {exc}",
            status=exc.status,
        ) from None


def root_message(session_id: str, title: object = None) -> str:
    """What a freshly opened thread's root says.

    Three lines, for a person who finds this thread in their channel and has to
    work out what it is: which run is talking, the key that finds that run
    anywhere, and where to watch it. *title* replaces the first line — an
    operator naming the thread, or a run's own opening words — and never the two
    below it, which are what make the thread traceable to a run at all.

    The key line is omitted for a session no run owns (a shell ``lmer chat``),
    and the link for a daemon whose bind address nobody else can dial
    (:func:`lmer_platform.config.control_ui_url`).
    """
    lines = [_cleaned_title(title) or run_label(session_id)]
    identity = _identity(session_id)
    if identity is not None:
        lines.append(runref.compose(*identity))
    try:
        link = cfg.control_ui_url(cfg.load())
    except Exception as exc:
        logger.warning("platform_thread_root_link_unknown error=%s", exc)
        link = None
    if link:
        lines.append(link)
    return "\n".join(lines)


def run_label(session_id: str) -> str:
    """How a thread is told which run is talking to it: the title, else the slug.

    Not :func:`lmer_platform.connectors._run_label`, whose repository
    coordinates are composed for an operator reading a refusal in their own
    fleet view. This is the name that goes on **every** mirrored line — the
    ``[label]`` prefix of each post — where the key would be noise repeated all
    day. A thread's root says the key once instead (:func:`root_message`), which
    is what makes the thread traceable to a run at all.
    """
    try:
        return _run_name(session_id)
    except Exception:
        return f"lmer session {session_id}"


def _run_name(session_id: str) -> str:
    identity = runs.run_for_session(session_id)
    if identity is None:
        return f"lmer session {session_id}"
    return meta.read(*identity).title or identity[2]


def assistant_label(workspace_id: Optional[str], *, name: Optional[str] = None) -> str:
    """``<assistant> · <project>``, or the bare assistant name without
    projects (#574).

    *name* is the resolved display name (``config.assistant_name``) when the
    caller has it; without it the environment decides, which the daemon and
    its connector children are given (#581).
    """
    speaker = (name or "").strip() or assistant_display_name()
    try:
        return _assistant_name(workspace_id, speaker)
    except Exception:
        return speaker


def _assistant_name(workspace_id: Optional[str], speaker: str) -> str:
    if not workspace_id:
        return speaker
    name = " ".join(str(projects.get(workspace_id)["name"]).split())
    return f"{speaker} · {name}" if name else speaker


def speaker_label(session_id: str, *, known_only: bool = False) -> Optional[str]:
    """The ``[speaker]`` tag every post this session makes into a thread
    carries (#574): :func:`assistant_label` for the assistant, :func:`run_label`
    for a run.

    A lookup that fails falls back to a guess. With *known_only* it returns
    ``None`` instead, so a caller already holding a good tag can keep it: a
    rotated assistant's entry is gone before its last posts are.
    """
    try:
        entry = registry.read_session(session_id)
    except Exception:
        entry = None
    if entry is None:
        return None if known_only else run_label(session_id)
    assistant = entry.get("kind") == registry.ASSISTANT_KIND
    try:
        if assistant:
            return _assistant_name(projects.session_home(session_id), assistant_display_name())
        return _run_name(session_id)
    except Exception:
        if known_only:
            return None
        return assistant_display_name() if assistant else f"lmer session {session_id}"


def _identity(session_id: str):
    try:
        return runs.run_for_session(session_id)
    except Exception:
        return None


def _cleaned_title(value: object) -> str:
    """One line, bounded — the cleaner a run's own title goes through.

    :mod:`lmer_platform.references` reaches for the same one for the same
    reason: two cleaners meant to agree and separately maintained are two
    cleaners that will stop agreeing.
    """
    if value is None:
        return ""
    try:
        return meta._cleaned(
            value, field="title", limit=meta.MAX_TITLE_CHARS, one_line=True
        ) or ""
    except meta.MetaError as exc:
        raise ThreadError(str(exc), status=400) from None


class SessionThreads:
    """One session's threads, bound so the servicer can hand a run the verbs.

    The servicer holds one of these per live session and calls exactly three
    methods on it; everything else about a thread the run is not connected to is
    :mod:`lmer_cli.thread_access`'s. A host side with no such object — the shell
    ``lmer`` process, whose connectors are launch arguments — refuses the three
    by name instead.
    """

    def __init__(
        self,
        session_id: str,
        *,
        by: str = BY_AGENT,
        env: Optional[dict] = None,
        where: str = thread_access.DAEMON_ENVIRONMENT,
    ) -> None:
        self.session_id = session_id
        self.by = by
        self.env = env
        self.where = where

    def open(
        self, channel: object, *, text: str = "", poster: Optional[Poster] = None,
    ) -> dict:
        return {
            "thread": open_for(
                self.session_id, channel, by=self.by, title=text or None,
                env=self.env, where=self.where, poster=poster,
            )
        }

    def conversation_ids(self) -> list:
        """Conversations a DM may be sent into without consulting the allow list.

        The transport-side id of each thread this session is connected to — the
        Slack channel or matrix room it resolved to — but **only those somebody
        other than the run connected**. Every record carries who made it, and
        the run's own `lmer-signal thread connect` is the one stamped
        :data:`BY_AGENT`.

        The filter is the whole safety of the exemption (MR !315, review round
        1). `thread open` takes any Slack channel string with no `:` or `/`,
        which includes a bare user id, and Slack resolves that to the user's IM
        — so without this a run could connect a DM with an unlisted person
        itself and then message them, with the transport allow list never
        consulted. #441 grants the exemption to threads *the human* opened, and
        `by` is the record of which those are.
        """
        records = connectors.ensure_connectors().records(self.session_id)
        found = []
        for record in records:
            if record.get("by") == BY_AGENT:
                continue
            key = record.get("key") or {}
            value = key.get("channel") or key.get("room")
            if value:
                found.append(value)
        return found

    def connect(self, permalink: object) -> dict:
        return {"thread": connect(self.session_id, permalink, by=self.by)}

    def disconnect(self, permalink: object) -> dict:
        return {"thread": disconnect_link(self.session_id, permalink)}
