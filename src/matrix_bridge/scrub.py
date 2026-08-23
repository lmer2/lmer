"""Everything that leaves for the room passes through here first.

The bridge quotes machine output into a chat room: a run's title, an attention
note, a question a session asked, and — under D8's three conditions — a file. A
transcript can carry a token as easily as a screenshot can, so both are scrubbed
with the *same* rule.

The project has **two** existing scrubs, and this path needs both — the !243
review was right to ask which, and the answer is not one of them:

- :func:`work_repo.utils.redact_secrets` is **value-based**: it redacts the
  values of *this host's* secret-named environment variables, plus known token
  prefixes and URL userinfo. It catches the bridge leaking its own environment —
  ``LMER_MATRIX_AS_TOKEN``, a work-repo token — into a room.
- :func:`lmer_platform.transcripts.scrub_credentials` is **shape-based**: it
  masks credential *shapes* in text (an ``Authorization`` header, a
  ``--token X`` argument, ``NAME=<secret>``) regardless of whether this process
  has ever held the value. It catches what the bridge is mostly made of —
  quoted machine output from another host's session.

Neither subsumes the other, and the bridge's text is exactly where both apply,
so both run. Reused rather than reimplemented for a reason worth stating: a
third secret-pattern regex is a third thing to keep current, and the one that
falls behind is always the one guarding the newest surface — which would be
this one. (``scrub_credentials`` is a public alias added for this; importing a
private name across modules is what the wave-0 client promotion existed to
avoid.)

Text and bytes are different problems, though, and this module is where the
difference is decided:

- **Text** is redacted in place. A token becomes ``***REDACTED***`` and the
  message still says what it was for.
- **Bytes that are text** (``text/*``, JSON, YAML, logs) are decoded, redacted
  and re-encoded.
- **Bytes that are not text** — a PNG, a PDF — cannot be redacted in place: a
  regex substitution inside a compressed stream produces a corrupt file, not a
  safe one. So if a known secret *value* appears in them, the attachment is
  **refused** rather than sent. Refusing loses a screenshot; sending would leak
  a live credential into a room's history, which is forever.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lmer_platform.transcripts import scrub_credentials
from work_repo.utils import redact_secrets

#: What :func:`work_repo.utils.redact_secrets` leaves behind. Quoted in the
#: room's one-line note so an operator reading the message knows the text was
#: touched, not truncated.
REDACTION_MARKER = "***REDACTED***"

#: MIME types whose bytes are text and can therefore be redacted in place.
#: Everything else is refused when it contains a secret, rather than mangled.
_TEXT_PREFIXES = ("text/",)
_TEXT_TYPES = (
    "application/json",
    "application/x-yaml",
    "application/yaml",
    "application/xml",
    "application/x-ndjson",
)


@dataclass(frozen=True)
class ScrubbedBytes:
    """The result of scrubbing an attachment.

    ``data`` is ``None`` when the attachment must not be sent at all — the
    caller drops it and says so in the room. ``reason`` is the log line, and it
    never quotes what was found.
    """

    data: Optional[bytes]
    changed: bool = False
    reason: Optional[str] = None

    @property
    def refused(self) -> bool:
        return self.data is None


def scrub_text(text: str) -> str:
    """Redact secrets in *text*: both of the project's rules, in order.

    Value-based first (this host's own secrets, by their values), then
    shape-based (credential shapes in quoted machine output). Order matters
    only for what the log says: a value already masked cannot match a shape.
    """
    if not text:
        return text
    return scrub_credentials(redact_secrets(text))


def is_text_mime(mime: Optional[str]) -> bool:
    """Can bytes of this type be redacted in place without corrupting them?"""
    if not mime:
        return False
    mime = mime.split(";", 1)[0].strip().lower()
    return mime.startswith(_TEXT_PREFIXES) or mime in _TEXT_TYPES


def scrub_bytes(data: bytes, mime: Optional[str]) -> ScrubbedBytes:
    """Scrub an attachment, or refuse it.

    Text-typed bytes are decoded (replacing anything undecodable, since the
    point is to *inspect* them rather than to preserve every byte), redacted and
    re-encoded. Anything else is scanned: if redacting its text rendering would
    have changed something, a secret is in there and the attachment is refused.
    """
    if not data:
        return ScrubbedBytes(data)

    if is_text_mime(mime):
        text = data.decode("utf-8", errors="replace")
        cleaned = scrub_text(text)
        return ScrubbedBytes(cleaned.encode("utf-8"), changed=cleaned != text)

    # Latin-1 round-trips every byte, so the scan sees the file's literal bytes
    # as characters without inventing or losing any. It finds a token that was
    # stored verbatim — the case that matters, a credential visible in a
    # screenshot's metadata or a log embedded in an archive — and does not
    # pretend to find one inside a compressed stream.
    probe = data.decode("latin-1")
    if scrub_text(probe) != probe:
        return ScrubbedBytes(
            None,
            reason=(
                f"attachment refused: a secret was found in {mime or 'binary'} "
                f"data, which cannot be redacted in place without corrupting it"
            ),
        )
    return ScrubbedBytes(data)
