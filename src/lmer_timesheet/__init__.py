"""The ``timesheet.d/`` schema: entries, handler definitions and sync ledgers.

This is the one shared schema module of spec D15. The tracker, the timesheet
writer and the entries view in ``lmer_platform`` use it, and a handler's sync
process outside lmer can import it too: it depends only on the standard
library and PyYAML.

The public surface is this package's ``__all__``, re-exported flat from the
four submodules:

- ``lmer_timesheet.entry`` — the entry format, the ``timesheet.d/`` layout
  and entry ids (D15);
- ``lmer_timesheet.handlers`` — handler definitions and required-field
  validation (D19);
- ``lmer_timesheet.ledger`` — the sync ledger line and an entry's sync
  status (D20);
- ``lmer_timesheet.issues`` — a handler's issue map (#665 D25).
"""

from lmer_timesheet.entry import (
    DEFAULT_HANDLER,
    RESERVED_PERSON_KEYS,
    SOURCES,
    TIMESHEET_DIR,
    TRIGGERS,
    Entry,
    SessionRef,
    Trigger,
    entry_path,
    format_timestamp,
    is_entry_id,
    issue_label,
    is_person_key,
    is_project_key,
    is_slug,
    new_entry_id,
    normalise_issue_url,
    parse_entry,
    parse_timestamp,
)
from lmer_timesheet.handlers import (
    HANDLERS_DIR,
    TIMESHEET,
    HandlerDefinition,
    HandlerField,
    load_handlers,
    parse_handler,
    validate_fields,
)
from lmer_timesheet.issues import (
    ISSUE_MAP_NAME,
    issue_map_path,
    load_issue_map,
    parse_issue_map,
)
from lmer_timesheet.ledger import (
    SYNC_STATUSES,
    LedgerLine,
    entry_hash,
    ledger_path,
    parse_ledger,
    sync_status,
)

__all__ = [
    "DEFAULT_HANDLER",
    "HANDLERS_DIR",
    "ISSUE_MAP_NAME",
    "RESERVED_PERSON_KEYS",
    "SOURCES",
    "SYNC_STATUSES",
    "TIMESHEET",
    "TIMESHEET_DIR",
    "TRIGGERS",
    "Entry",
    "HandlerDefinition",
    "HandlerField",
    "LedgerLine",
    "SessionRef",
    "Trigger",
    "entry_hash",
    "entry_path",
    "format_timestamp",
    "is_entry_id",
    "is_person_key",
    "is_project_key",
    "is_slug",
    "issue_label",
    "issue_map_path",
    "ledger_path",
    "load_handlers",
    "load_issue_map",
    "new_entry_id",
    "normalise_issue_url",
    "parse_entry",
    "parse_handler",
    "parse_issue_map",
    "parse_ledger",
    "parse_timestamp",
    "sync_status",
    "validate_fields",
]
