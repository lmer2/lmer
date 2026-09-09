"""Thread target validation shared by platform spawns and assistant settings.

The rule itself lives in :mod:`lmer_cli.threads`, with the parse it guards: the
host ``lmer`` process validates the same links (issue #414) and two readings of
what a permalink may contain is one reading too many.
"""

from lmer_cli.threads import MAX_THREAD_TARGETS, validate_thread_targets

__all__ = ["MAX_THREAD_TARGETS", "validate_thread_targets"]
