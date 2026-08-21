"""Stable markers that identify text typed by platform machinery.

The producer and transcript reader both need these values, so they live below
either subsystem rather than making lifecycle and transcript imports circular.
"""

__all__ = ["PLATFORM_PREFIX"]


#: Marks input the platform typed rather than the operator. The scrollback of a
#: session that received one of these prompts is read later by someone trying to
#: work out why it acted, and "who said this" is the first question. ASCII on
#: purpose: this is written into a PTY belonging to a harness whose input
#: handling is not ours.
PLATFORM_PREFIX = "[lmer platform]"
