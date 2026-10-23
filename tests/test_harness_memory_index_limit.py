"""The memory index load limit is one fact, declared on the harness (#435).

Claude Code loads only the head of ``MEMORY.md`` at session start. The number
lives on the claude :class:`~lmer_cli.harness.Harness` and ``work memory
persist`` reads its budget from there, so the two can never drift apart.
"""

import dataclasses

from lmer_cli import harness
from lmer_cli.harness import (
    CLAUDE_MEMORY_INDEX_LIMIT,
    HARNESSES,
    MemoryIndexLimit,
    memory_index_budget,
)
from work_repo import memory


def test_claude_declares_the_documented_head_load():
    # code.claude.com/docs/en/memory: "The first 200 lines of MEMORY.md, or
    # the first 25KB, whichever comes first".
    assert CLAUDE_MEMORY_INDEX_LIMIT == MemoryIndexLimit(max_bytes=25_000, max_lines=200)
    assert HARNESSES["claude"].memory_index_limit is CLAUDE_MEMORY_INDEX_LIMIT


def test_harnesses_without_a_native_memory_load_declare_no_limit():
    for name, entry in HARNESSES.items():
        if name != "claude":
            assert entry.memory_index_limit is None, name


def test_the_budget_is_the_strictest_declared_limit(monkeypatch):
    tighter = MemoryIndexLimit(max_bytes=30_000, max_lines=50)
    monkeypatch.setitem(
        HARNESSES,
        "codex",
        dataclasses.replace(HARNESSES["codex"], memory_index_limit=tighter),
    )
    assert memory_index_budget() == MemoryIndexLimit(max_bytes=25_000, max_lines=50)


def test_no_declared_limit_means_no_budget(monkeypatch):
    monkeypatch.setattr(
        harness,
        "HARNESSES",
        {name: entry for name, entry in HARNESSES.items() if name != "claude"},
    )
    assert memory_index_budget() is None


def test_persist_reads_its_budget_from_the_harness():
    assert memory._index_budget() == memory_index_budget()
