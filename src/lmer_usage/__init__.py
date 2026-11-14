"""AI usage from harness transcripts (#315, #647).

A neutral package: it imports nothing from ``lmer_platform`` or ``work_repo``,
because both import it. The run's own writer (``work usage``) and the daemon's
live view read transcripts through the same adapters, so the durable record and
the live numbers cannot drift apart. See ``docs/USAGE-TRACKING.md``.
"""

from .summarise import Summary, summarise

__all__ = ["Summary", "summarise"]
