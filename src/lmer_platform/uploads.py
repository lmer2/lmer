"""Files the operator hands a session through its chat (issue #246).

The chat's write path types bytes at a PTY (``POST …/input``) and an attachment
has nowhere to go in it. So a file travels the way every other piece of
non-typed data reaches a session: the daemon writes it into a host directory
bind-mounted into the container, and the message names the path. The same shape
as the ask channel (:mod:`lmer_platform.ask`), for the same reasons — the host
is not addressable from inside a container without a runtime-specific gateway
address, and a URL would mean handing every session the platform's secret.

Two stores, one mechanism (operator clarifications 3 and 4 on the issue):

* **A worker's is per session**, beside its ask channel and transcript under
  ``logs/``, and the *run* decides what becomes of an upload: it copies what is
  worth keeping into its work dir's ``uploads/`` and commits or discards it.
  The daemon cannot do that itself — ``/work`` is cloned *inside* the container
  (``lmer_cli.container.clone_and_exec``), so there is no host-side working tree
  to write into.
* **uber lmer's is per host**, so an incarnation can be handed a file the last
  one was given, and it is the assistant's own to organise and delete
  (:mod:`lmer_platform.memory` keeps its store the same way and for the same
  reason).

Which store a session gets is decided by its ``kind``, at the one place that
knows it: the spawn for the mount, the registry entry for the route. Two
implementations of "what a pasted file is" would drift, and the worker case is
the one where a screenshot helps most.

What is here that the ask channel has no need for is the **type** decision. The
browser's declared MIME type and the filename's extension are the sender's
claims about its own content; what this module accepts or refuses on is the
bytes (:func:`sniff`). An allowed-type list a rename could defeat would not be
a policy.

Nothing here pushes anything anywhere. An upload can hold whatever was on the
operator's screen, credentials included: the directories are 0700, the files
0600, and the only copy that ever leaves the host is one a run committed on
purpose.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import registry, store
from .store import logs_dir, platform_dir

logger = logging.getLogger("lmer_platform.uploads")

__all__ = [
    "CONTAINER_UPLOADS_DIR", "UPLOADS_DIR_ENV", "SESSION_DIR_SUFFIX",
    "ASSISTANT_DIRNAME", "DIR_MODE", "FILE_MODE", "KNOWN_TYPES",
    "DEFAULT_TYPES", "DEFAULT_MAX_BYTES", "REFERENCE_PREFIX", "MAX_NAME_CHARS",
    "UploadError", "StoreUnavailable", "UploadTooLarge", "UploadTypeRefused",
    "UploadNotFound", "UploadRejected",
    "StoredUpload", "UploadKind",
    "session_upload_dir", "assistant_upload_dir", "upload_dir_for",
    "prepare_upload_dir", "mount_flags", "registry_pointer", "mounted_store",
    "readable_store",
    "decode_payload", "sniff", "stored_name", "store_upload", "open_stored",
    "HEAD_BYTES",
    "reference_line",
]

#: Suffix of a worker session's store, beside its PTY log, transcript and ask
#: channel in ``logs/``. Derived from the session id, so resolving one needs no
#: recorded state — the property that makes these directories a mount and
#: nothing else.
SESSION_DIR_SUFFIX = ".uploads"

#: uber lmer's store: one per host, like its memory (:mod:`lmer_platform.memory`).
ASSISTANT_DIRNAME = "assistant-uploads"

#: Where either store is bound inside the container, and the value
#: :data:`UPLOADS_DIR_ENV` carries. Bound at its declared path rather than staged
#: (#293/#290): the parent ``/home/developer`` ships in the image, which is the
#: condition the staging area exists to cover.
CONTAINER_UPLOADS_DIR = "/home/developer/.lmer-uploads"

#: The variable that tells a session it has a store — and, through the prompt
#: fragment gated on it, what to do with what lands there. Set only when the
#: mount was actually prepared, exactly as ``LMER_ASK_DIR`` is.
UPLOADS_DIR_ENV = "LMER_UPLOADS_DIR"

#: Owner-only, like the rest of the platform's state tree.
DIR_MODE = store.STATE_DIR_MODE

#: Owner-only for the file too: an upload is operator content of unknown
#: sensitivity, and it sits in a directory other accounts must not read out of.
FILE_MODE = 0o600


@dataclass(frozen=True)
class UploadKind:
    """One file type this module can recognise *from its bytes*.

    ``magic`` is the signature; ``text`` marks the type that has none and is
    identified by decoding instead. ``inline`` says whether the read route may
    let a browser render it in place — true for images, which is what a
    thumbnail needs, and false for everything else, because "render this in the
    operator's browser" is a decision to take per type rather than by default.
    """

    name: str
    content_type: str
    extension: str
    magic: tuple = ()
    text: bool = False
    inline: bool = False


#: Every type that can be *enabled*, which is deliberately wider than the set
#: enabled by default: a configurable allowlist an operator can only narrow is
#: not much of a knob. Each entry is recognisable from the bytes — a type this
#: module cannot identify has no business on the list, since the allowlist would
#: then rest on the sender's word for it.
KNOWN_TYPES = {
    kind.name: kind
    for kind in (
        UploadKind(
            "png", "image/png", ".png",
            magic=(b"\x89PNG\r\n\x1a\n",), inline=True,
        ),
        UploadKind(
            "jpeg", "image/jpeg", ".jpg", magic=(b"\xff\xd8\xff",), inline=True,
        ),
        UploadKind(
            "gif", "image/gif", ".gif",
            magic=(b"GIF87a", b"GIF89a"), inline=True,
        ),
        # RIFF containers name their form at offset 8; matched by
        # :func:`sniff` rather than by a prefix, hence no ``magic`` here.
        UploadKind("webp", "image/webp", ".webp", inline=True),
        UploadKind("pdf", "application/pdf", ".pdf", magic=(b"%PDF-",)),
        # Last: it is the fallback, and a file with any of the signatures above
        # is that type even when it also happens to decode as UTF-8.
        UploadKind("txt", "text/plain; charset=utf-8", ".txt", text=True),
    )
}

#: What the operator asked for as the default (clarification 6).
DEFAULT_TYPES = ("png", "jpeg", "txt")

#: Per file, and generous for what this is for: a phone screenshot is a few
#: hundred KB. The cap exists so a mistake (or a paste of something enormous)
#: is refused with a sentence rather than filling a state directory.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: How long a stored name may get before the operator's part of it is trimmed.
#: Long enough to stay recognisable in a chat message, short enough that the
#: path in that message does not wrap on a phone.
MAX_NAME_CHARS = 96

#: Timestamp prefix of a stored name. Compact rather than
#: :data:`lmer_platform.store.TS_FORMAT`, because this one goes in a filename
#: and in a path the operator reads: colons in a path are a shell quoting
#: problem for the agent that has to open it.
NAME_TS_FORMAT = "%Y%m%d-%H%M%S"

#: How an upload is named in the message the session receives. The wording lives
#: here, on the writing side, so the client is handed the line rather than
#: composing its own — see :func:`reference_line`.
REFERENCE_PREFIX = "[lmer upload]"

#: The control characters no text file carries: everything below space except
#: tab, newline, vertical tab, form feed and carriage return — **and** DEL plus
#: the C1 block above it, which the first cut of this pattern left out while its
#: comment claimed otherwise, so a file of nothing but DEL bytes was filed as
#: ``txt`` (!272 review). C1 is expressed as code points because the scan runs
#: over decoded text: U+0085 and its neighbours arrive as two UTF-8 bytes each.
#: A compiled pattern rather than a scan, because the payload it runs over can be
#: megabytes and this runs on the request thread.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")

#: How much of a file the signature check looks at. Every signature is a prefix
#: (WebP's form marker sits at offset 8), so this is generous already — and it
#: bounds the read the *read* path makes, which is looking at a file the session
#: may have replaced with something enormous.
HEAD_BYTES = 64

#: Anything else in a filename is replaced. Deliberately narrow: the name ends
#: up in a path that an agent will type at a shell.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class UploadError(RuntimeError):
    """Base refusal, carrying the HTTP status a route should answer.

    The status rides on the exception as it does in
    :mod:`lmer_platform.session_io` and :mod:`lmer_platform.ask`: one handler per
    route, and a refusal added later arrives with a code instead of a 500.
    """

    status = 400


class UploadRejected(UploadError):
    """The request itself is not usable — no name, not base64, empty payload."""

    status = 400


class StoreUnavailable(UploadError):
    """This session has no upload store mounted, so a file cannot reach it.

    409 rather than 404: the session exists and is answering, and the request
    would be fine against one started since this feature shipped. Sessions
    already running when it did have no mount, and no amount of retrying adds
    one to a container that is already up — so the refusal names the restart
    instead. Never accepted-and-filed: a file stored where nothing is looking is
    worse than a refusal, because the operator would be told it was delivered.
    """

    status = 409


class UploadTooLarge(UploadError):
    """Over the configured per-file cap."""

    status = 413


class UploadTypeRefused(UploadError):
    """The bytes are not one of the types this platform accepts."""

    status = 415


class UploadNotFound(UploadError):
    """No such file in this session's own store."""

    status = 404


@dataclass(frozen=True)
class StoredUpload:
    """One file as it now exists on the host, and how to refer to it."""

    session_id: str
    name: str
    path: Path
    bytes: int
    kind: str
    content_type: str
    container_path: str

    def to_dict(self) -> dict:
        return {
            "session": self.session_id,
            "name": self.name,
            "bytes": self.bytes,
            "kind": self.kind,
            "content_type": self.content_type,
            "container_path": self.container_path,
            "reference": reference_line(self),
        }


def _validated(session_id: str) -> str:
    """Reject an id that could not name a session before it reaches a path.

    Borrowed from the registry rather than written again, as
    :mod:`lmer_platform.ask` and :mod:`lmer_platform.transcripts` do: two
    different notions of a legal session id is how ``..`` eventually gets past
    one of them.
    """
    try:
        registry.session_path(session_id)
    except registry.RegistryError as exc:
        raise UploadNotFound(str(exc)) from exc
    return session_id


def session_upload_dir(session_id: str) -> Path:
    """Host store of one worker session."""
    return logs_dir() / f"{_validated(session_id)}{SESSION_DIR_SUFFIX}"


def assistant_upload_dir() -> Path:
    """uber lmer's store: ``<platform state>/assistant-uploads``."""
    return platform_dir() / ASSISTANT_DIRNAME


def upload_dir_for(session_id: str, kind: str) -> Path:
    """The store a session of *kind* uses.

    The one place the per-kind choice is made, called by the spawn (which knows
    the kind it is starting) and by the routes (which read it off the registry
    entry). An unknown kind takes the per-session store: a store shared by every
    incarnation is a thing to opt into, not to fall back to.
    """
    if kind == registry.ASSISTANT_KIND:
        return assistant_upload_dir()
    return session_upload_dir(session_id)


def prepare_upload_dir(directory: Path) -> Optional[Path]:
    """Create *directory* owner-only and return it, or ``None`` if unusable.

    Fail-soft, like the ask channel and the memory store: a session with no
    upload store is exactly today's behaviour, so it warns and skips the mount
    rather than refusing to start a session someone is waiting for. The failure
    is honest on the other side too — with no directory the environment variable
    is not set, so no prompt fragment tells the agent to look in a place nothing
    was mounted at, and the route refuses the upload instead of writing it
    somewhere unread.
    """
    try:
        # Through the store so every level takes DIR_MODE: ``mkdir(mode=…)`` is
        # leaf-only, which is how the transcript root ended up at the umask
        # (the T93 finding).
        store.ensure_state_dir(directory)
    except OSError as exc:
        logger.warning(
            "platform_upload_dir_unusable path=%s error=%s — this session runs "
            "without an upload store; files the operator attaches to its chat "
            "are refused rather than stored where nothing reads them",
            directory, exc,
        )
        return None
    # Checked, not assumed: ``ensure_state_dir`` only clears bits *outside*
    # DIR_MODE, so a pre-existing store that this user cannot write comes back
    # untouched — and mounting one produces the "every upload fails" symptom at
    # the far end of the feature instead of here.
    if not os.access(directory, os.W_OK | os.X_OK):
        logger.warning(
            "platform_upload_store_unwritable path=%s — the store exists but "
            "this user cannot write it; not mounted",
            directory,
        )
        return None
    return directory


def mount_flags(directory: Path) -> list:
    """``lmer`` flags binding *directory* in as the session's upload store.

    ``rw``: the daemon writes what the operator attaches, and the session side
    moves, renames and deletes — uber lmer manages its store, and a run copies
    out of this one into its work dir. The destination comes from
    :data:`CONTAINER_UPLOADS_DIR`, the same constant :data:`UPLOADS_DIR_ENV`
    carries, so the mount and what the agent is told cannot drift.
    """
    return ["--mount-dir", f"{directory}:{CONTAINER_UPLOADS_DIR}:rw"]


def registry_pointer(directory: Optional[Path]) -> dict:
    """What the spawn records about the mount it just made.

    The registry entry is where a route learns whether a session *has* a store,
    and this is why it is recorded rather than inferred: the assistant's store
    exists on the host as soon as one spawn has made it, so its presence says
    nothing about whether the incarnation now running was started with the mount.
    The spawner is the only actor that knows, so the spawner writes it down.

    ``{}`` for a session with no store, which is also what an entry written by
    an older daemon carries — the two are the same fact and get the same
    refusal.
    """
    if directory is None:
        return {}
    return {"path": str(directory), "container_path": CONTAINER_UPLOADS_DIR}


def mounted_store(session_id: str, entry: Optional[dict]) -> Path:
    """The store a live session's registry *entry* says it was given.

    Raises :class:`StoreUnavailable` when the entry records none, which covers
    both a session started before this feature and one whose store could not be
    prepared. The path is re-derived from the session id and kind rather than
    trusted from the entry: the entry is a file on disk, and a path read out of
    it would be a path chosen by whoever could write it.
    """
    pointer = (entry or {}).get("uploads")
    if not isinstance(pointer, dict) or not pointer.get("path"):
        raise StoreUnavailable(
            "this session has no upload store: it was started before file "
            "upload existed, or its store could not be created. Restart the "
            "session (uber lmer: restart it from the chat header) and the next "
            "one will accept files."
        )
    directory = upload_dir_for(session_id, str((entry or {}).get("kind") or ""))
    if not directory.is_dir():
        raise StoreUnavailable(
            f"this session's upload store is gone from the host ({directory}); "
            "restart the session to get a fresh one."
        )
    return directory


def readable_store(session_id: str, entry: Optional[dict]) -> Path:
    """The store a *read* looks in, which outlives the session the write needed.

    A worker's store sits beside its transcript and its PTY log and is kept for
    the same reason: the conversation stays readable after the container is gone,
    and a screenshot in it is part of that conversation. So a session whose entry
    has been reaped — the normal end state of a clean exit — still serves what it
    was sent, from the directory its id names.

    What does not survive is uber lmer's: its store is the host's, named by no
    session, so once an incarnation's entry is gone there is nothing that says
    which files were handed to *it*. The pane degrades to the path line the turn
    already carries.
    """
    pointer = (entry or {}).get("uploads")
    if isinstance(pointer, dict) and pointer.get("path"):
        return mounted_store(session_id, entry)
    directory = session_upload_dir(session_id)
    if directory.is_dir():
        return directory
    raise UploadNotFound(
        f"session {session_id!r} has no upload store on this host"
    )


def decode_payload(data: object, limit: int) -> bytes:
    """Base64 text to bytes, refusing an over-cap payload before decoding it.

    The length check is done on the *encoded* text, before decoding, and what it
    avoids is precisely the **decoded** buffer: by the time this runs the request
    body has already been read and parsed into a ``str`` by Starlette and
    ``json.loads``, and nothing in this application bounds *that* — there is no
    body-size middleware, and a ``Content-Length`` guard would be a separate
    decision at the ASGI layer. The earlier wording here claimed an oversized
    payload was "refused without materialising it", which was never true of the
    body (!272 review).

    Base64 is 4 characters per 3 bytes, plus padding, plus whatever whitespace
    the encoder left in — hence the slack, which is deliberately loose: this
    bound exists to stop something enormous, and :func:`store_upload` checks the
    decoded length exactly.
    """
    if not isinstance(data, str) or not data.strip():
        raise UploadRejected("upload needs a base64 'data' field")
    # Line breaks taken out before anything else: ``base64`` wraps its output at
    # 76 columns by default, so a caller piping a file through it sent a file
    # rather than garbage. Whitespace is the one thing forgiven — see below.
    payload = "".join(data.split())
    encoded_cap = ((limit + 2) // 3) * 4 + 1024
    if len(payload) > encoded_cap:
        raise UploadTooLarge(
            f"that file is too big: this platform accepts {limit} bytes per "
            "file (LMER_PLATFORM_UPLOAD_MAX_BYTES)"
        )
    try:
        # Strict, so a payload that is not base64 is a 400 naming the field
        # rather than bytes nobody meant to send: ``validate=False`` silently
        # discards every character outside the alphabet, which turns a truncated
        # or mistyped payload into a shorter file.
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadRejected(f"'data' is not valid base64 ({exc})") from exc
    if not raw:
        raise UploadRejected("that file is empty")
    return raw


def _signature_kind(head: bytes, names=None) -> Optional[UploadKind]:
    """The type *head* carries the signature of, or ``None``.

    The one place the signatures are matched. It used to be written twice — once
    in :func:`sniff` over the whole payload and once in the read path over the
    first 64 bytes — and the copies could not see the same bytes, so a signature
    added past that window would land in one of them and the symptom would be an
    image quietly served as ``text/plain`` (!272 review). Every signature this
    module knows is a prefix, so a head is all either caller ever needed.

    *names* narrows the search to an allowlist; ``None`` searches every known
    type, which is what the read path wants — it is describing a file that is
    already stored, not deciding whether to accept one.
    """
    for name in (KNOWN_TYPES if names is None else names):
        kind = KNOWN_TYPES[name]
        if kind.magic and any(head.startswith(prefix) for prefix in kind.magic):
            return kind
        # RIFF containers name their form at offset 8 rather than at the start,
        # which is why this is not expressible as a prefix.
        if kind.name == "webp" and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return kind
    return None


def sniff(raw: bytes, allowed) -> UploadKind:
    """Which allowed type *raw* actually is, or a refusal.

    The bytes decide. A browser's ``File.type`` and the filename's extension are
    the sender's claims about its own content, and an allowlist that could be
    got past by renaming a file would not be one — so neither is consulted here.

    ``txt`` is last and is the only type without a signature, so what stands in
    for one is :func:`_is_text` — still a measurement of the bytes rather than a
    guess. Anything carrying one of the signatures above is that type even when
    it would also pass as text.
    """
    names = [name for name in allowed if name in KNOWN_TYPES]
    signature = _signature_kind(raw[:HEAD_BYTES], names)
    if signature is not None:
        return signature
    for name in names:
        kind = KNOWN_TYPES[name]
        if kind.text and _is_text(raw):
            return kind
    raise UploadTypeRefused(
        "that is not a file type this platform accepts. Allowed: "
        f"{', '.join(names) or 'nothing (LMER_PLATFORM_UPLOAD_TYPES is empty)'} "
        "— decided by the file's contents, not its name."
    )


def _is_text(raw: bytes) -> bool:
    """Whether *raw* is text, decided the way ``git`` decides it.

    Decoding as UTF-8 is necessary and not sufficient: a run of NUL bytes decodes
    perfectly, so "it decoded" would make ``txt`` — which is on by default — an
    allowlist entry that accepts arbitrary binary, which is the hole the whole
    type check exists to close. A control character no text file carries
    disqualifies it, whitespace excepted.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not _CONTROL_CHARS.search(text)


def stored_name(name: object, kind: UploadKind, *, now: Optional[datetime] = None) -> str:
    """The name an upload is filed under: a timestamp, then the operator's name.

    The operator's filename is kept because they will read it back in a chat
    message, and sanitised because an agent is going to type this path at a
    shell. The timestamp is what makes two pastes of ``image.png`` two files:
    overwriting the first would lose a file the session may not have read yet,
    and it is invisible from the sending end.

    The extension is the *sniffed* type's, appended when the sanitised name does
    not already end in it — so a PNG called ``notes.txt`` is stored as
    ``notes.txt.png`` rather than under a name that lies about its contents.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime(NAME_TS_FORMAT)
    text = _UNSAFE_NAME_CHARS.sub("-", str(name or "").strip())
    # The replacement is a placeholder for characters, not a separator to keep
    # beside one: ``my shot!.png`` reads better as ``my-shot.png`` than as
    # ``my-shot-.png``, and the name is something the operator will look at.
    text = re.sub(r"-*\.-*", ".", text).strip("-._")
    if not text:
        text = "upload"
    # Room for the stamp, its separator and the extension, so the bound holds
    # for the whole name rather than for the part the operator chose.
    room = MAX_NAME_CHARS - len(stamp) - 1 - len(kind.extension)
    if len(text) > room:
        text = text[:room].rstrip("-._") or "upload"
    if not text.lower().endswith(kind.extension):
        text = f"{text}{kind.extension}"
    return f"{stamp}-{text}"


def _collision_name(filename: str, kind: UploadKind, attempt: int) -> str:
    """*filename* with an attempt marker, still one name and still bounded.

    Two uploads of the same name in the same second is what gets here — the
    stored name carries a timestamp — so this is rare and still has to produce a
    name of the same shape as every other. Appending to the whole name gave
    ``…aaa.png.2.png``: over :data:`MAX_NAME_CHARS` and carrying the extension
    twice, which is a name an operator reads and an agent types (!272 review).
    The marker goes before the extension, and the stem is trimmed to make room.
    """
    stem = filename[: -len(kind.extension)] if filename.endswith(kind.extension) else filename
    marker = f"-{attempt}"
    room = MAX_NAME_CHARS - len(marker) - len(kind.extension)
    return f"{stem[:room].rstrip('-._')}{marker}{kind.extension}"


def store_upload(
    session_id: str,
    entry: Optional[dict],
    name: object,
    data: object,
    *,
    allowed=DEFAULT_TYPES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> StoredUpload:
    """Write one attachment into the session's store and describe it.

    Order matters: the store is resolved first (a session that cannot receive a
    file is told so before its payload is decoded), then the size, then the
    type, and only then is anything written. The file is created owner-only with
    ``O_EXCL`` — the name carries a timestamp to the second, so a collision means
    two uploads in the same second, and the second gets its own name rather than
    overwriting the first.
    """
    directory = mounted_store(session_id, entry)
    raw = decode_payload(data, max_bytes)
    if len(raw) > max_bytes:
        raise UploadTooLarge(
            f"that file is {len(raw)} bytes; this platform accepts {max_bytes} "
            "per file (LMER_PLATFORM_UPLOAD_MAX_BYTES)"
        )
    kind = sniff(raw, allowed)
    filename = stored_name(name, kind)
    path = directory / filename
    attempt = 1
    while True:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE
            )
        except FileExistsError:
            attempt += 1
            path = directory / _collision_name(filename, kind, attempt)
            continue
        except OSError as exc:
            raise UploadError(
                f"the upload store would not take the file ({exc})"
            ) from exc
        break
    try:
        with os.fdopen(descriptor, "wb") as handle:
            # Through the descriptor, never by name: this store is bind-mounted
            # ``rw`` into a container an agent drives, and the write above takes
            # long enough for that side to rename the file and leave a symlink at
            # the path. An ``os.chmod(path, …)`` then resolves the *name* again
            # and applies the mode to whatever it now points at — measured
            # setting a daemon-owned file outside the store to 0600, which aimed
            # at a directory takes the platform's state tree out (!272 review).
            # ``O_EXCL`` protects the creation and nothing after it; ``fchmod``
            # is what makes the mode exact under any umask without a second
            # resolution.
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(raw)
    except OSError as exc:
        # A half-written file in a directory an agent is told to read is worse
        # than none: the agent would open a truncated image and report on it.
        # Unlinked by name, which is safe in the way the chmod above was not: the
        # worst a swapped name costs is that the file this created stays and a
        # link the container planted goes.
        path.unlink(missing_ok=True)
        raise UploadError(f"the upload could not be written ({exc})") from exc
    logger.info(
        "platform_upload_stored id=%s name=%s kind=%s bytes=%s",
        session_id, path.name, kind.name, len(raw),
    )
    return StoredUpload(
        session_id=session_id,
        name=path.name,
        path=path,
        bytes=len(raw),
        kind=kind.name,
        content_type=kind.content_type,
        container_path=f"{CONTAINER_UPLOADS_DIR}/{path.name}",
    )


def open_stored(session_id: str, entry: Optional[dict], name: str) -> tuple:
    """``(open file, UploadKind, stored name)`` for one file in this store.

    **A handle, not a path, and that is the whole of it.** The name is matched
    against what the store actually holds rather than joined onto its path — so
    ``..`` and an absolute path do not "escape", they simply are not in the
    listing — but that match is a snapshot of a directory the container has
    ``rw`` on. The earlier version returned the path, which meant the *decision*
    was made on one resolution of the name and the *bytes* were read on two more:
    once here and once when Starlette's ``FileResponse`` opened it to send.
    Measured: the container renames the file and leaves a symlink at that name
    after this returns, and the response then serves whatever the daemon user can
    read, under the ``Content-Type`` the earlier sniff decided (!272 review).

    So the name is resolved exactly once and the guarantees ride on the
    descriptor:

    * ``O_NOFOLLOW`` — a symlink at that name is ``ELOOP`` rather than a read of
      its target. That covers the fixed-symlink case the tests already had *and*
      the swapped-in-between one they could not.
    * ``fstat`` — a directory or a device that got the name instead is refused on
      what was opened rather than on what was listed.
    * ``O_NONBLOCK`` — a FIFO planted in the store would otherwise hold the
      request thread inside ``open`` until something wrote to it. It means nothing
      for the regular files this goes on to serve.

    The type is sniffed from that same descriptor and the handle rewound, so the
    ``Content-Type`` describes the bytes about to be sent rather than a guess
    recorded at upload time — a session can replace a file through the mount, and
    this way that is a different answer rather than a wrong one.

    The caller owns the handle: it is handed to a streaming response, which closes
    it. Every failure path here closes it first.
    """
    directory = readable_store(session_id, entry)
    wanted = str(name or "")
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise UploadNotFound(f"the upload store cannot be read ({exc})") from exc
    for candidate in entries:
        if candidate.name != wanted:
            continue
        if not candidate.is_file(follow_symlinks=False):
            break
        try:
            descriptor = os.open(
                candidate.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except OSError as exc:
            # ELOOP (a symlink now at that name), ENOENT (deleted since the
            # listing) and EACCES all say the same thing to a caller: there is no
            # upload of that name to serve.
            raise UploadNotFound(f"that upload cannot be read ({exc})") from exc
        handle = os.fdopen(descriptor, "rb")
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UploadNotFound(
                    f"{wanted!r} is not a file this store can serve"
                )
            head = handle.read(HEAD_BYTES)
            handle.seek(0)
        except Exception:
            handle.close()
            raise
        # Whatever it is now: with no signature it is not an image, and is served
        # as a download rather than rendered — which is what a file of unknown
        # shape should be.
        return handle, _signature_kind(head) or KNOWN_TYPES["txt"], candidate.name
    raise UploadNotFound(f"no upload named {wanted!r} in this session's store")


def reference_line(upload: StoredUpload) -> str:
    """How an upload is named in the message the session receives.

    Composed here rather than in the browser so there is one wording, and handed
    back to the client to include verbatim: the pane also *recognises* these
    lines when the turn comes back from the transcript, to put a thumbnail under
    it, and a format the two ends spell separately is a format they will
    eventually spell differently.

    Nothing automated hangs off the match. What the harness writes into its
    transcript for a user turn is the harness's business; if it rewrites the
    text there is no thumbnail and the path is still there in full.
    """
    return (
        f"{REFERENCE_PREFIX} {upload.container_path} "
        f"({upload.content_type}, {_human_bytes(upload.bytes)})"
    )


def _human_bytes(count: int) -> str:
    """A size an operator reads, not one they parse. Kibibytes, like ``ls -h``."""
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / (1024 * 1024):.1f} MB"
