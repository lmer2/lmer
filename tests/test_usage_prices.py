"""Read-time pricing: an operator-supplied table, no shipped defaults, null without one."""

from __future__ import annotations

import hashlib

import pytest

from lmer_usage import buckets, prices

TABLE = """\
currency: USD
models:
  claude-opus-4-1:
    input: 15
    cache_create: 18.75
    cache_read: 1.5
    output: 75
  "claude-*":
    input: 3
    cache_create: 3.75
    cache_read: 0.3
    output: 15
  "claude-opus-*":
    input: 5
    cache_create: 6.25
    cache_read: 0.5
    output: 25
"""


def _row(model, **counts):
    return {"harness": "claude", "model": model, "agent": None, "status": "collected",
            **buckets.make(**counts)}


@pytest.fixture
def table(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text(TABLE)
    return prices.load_table(str(path))


def test_no_table_means_null_cost(tmp_path):
    assert prices.load_table("") is None
    assert prices.load_table(None) is None
    assert prices.load_table(str(tmp_path / "absent.yaml")) is None
    assert prices.cost(None, [_row("claude-opus-4-1", output=1000)]) == {
        "currency": None, "total": None, "partial": False, "table_hash": None,
        "unpriced_models": [],
    }


def test_exact_id_beats_glob_and_longest_glob_wins(table):
    assert table.price_for("claude-opus-4-1")["output"] == 75
    assert table.price_for("claude-opus-4-5")["output"] == 25
    assert table.price_for("claude-sonnet-4-5")["output"] == 15
    assert table.price_for("gpt-5.5-codex") is None
    assert table.price_for(None) is None


def test_cost_sums_every_bucket_per_million(table):
    rows = [_row("claude-opus-4-1", input=1_000_000, cache_create=1_000_000,
                 cache_read=1_000_000, output=1_000_000, thinking=500_000),
            _row("claude-sonnet-4-5", output=1)]
    result = prices.cost(table, rows)
    assert result["currency"] == "USD"
    assert result["total"] == round(15 + 18.75 + 1.5 + 75 + 15 / 1e6, 6)
    assert result["partial"] is False and result["unpriced_models"] == []
    assert result["table_hash"] == table.hash


def test_uncovered_models_make_the_cost_partial(table):
    rows = [_row("claude-opus-4-1", output=1_000_000), _row("gpt-5.5-codex", output=10),
            _row("gpt-5.5-codex", input=3), _row("o9", input=1)]
    result = prices.cost(table, rows)
    assert result["partial"] is True
    assert result["total"] == 75.0
    assert result["unpriced_models"] == ["gpt-5.5-codex", "o9"]


def test_no_covered_row_leaves_total_null(table):
    result = prices.cost(table, [_row("gpt-5.5-codex", output=10)])
    assert result["total"] is None and result["partial"] is True
    assert result["currency"] == "USD" and result["table_hash"] == table.hash


def test_no_rows_costs_nothing(table):
    assert prices.cost(table, [])["total"] == 0.0


def test_total_is_rounded_to_six_decimals(table):
    assert prices.cost(table, [_row("claude-opus-4-1", output=1)])["total"] == 0.000075
    assert prices.cost(table, [_row("claude-sonnet-4-5", cache_read=1)])["total"] == 0.0


def test_table_hash_names_the_file_bytes(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text(TABLE)
    first = prices.load_table(str(path))
    assert first.hash == "sha256:" + hashlib.sha256(TABLE.encode()).hexdigest()
    path.write_text(TABLE.replace("output: 75", "output: 80"))
    second = prices.load_table(str(path))
    assert second.hash != first.hash
    assert second.price_for("claude-opus-4-1")["output"] == 80


def test_a_stored_cost_is_never_read(table):
    clean = [_row("claude-opus-4-1", output=1000)]
    bogus = [{**clean[0], "cost": 999.0, "cost_estimate": 999.0,
              "harness_reported_cost": [{"currency": "USD", "total": 999.0}]}]
    assert prices.cost(table, bogus) == prices.cost(table, clean)


def test_not_collected_rows_are_ignored(table):
    rows = [_row("claude-opus-4-1", output=1_000_000),
            {"harness": "claude", "model": "gpt-5.5-codex", "agent": None,
             "status": "not_collected", "reason": "no usage", **buckets.zero()}]
    result = prices.cost(table, rows)
    assert result["total"] == 75.0
    assert result["partial"] is False and result["unpriced_models"] == []


@pytest.mark.parametrize("text,needle", [
    ("- a\n- b\n", "not a mapping"),
    ("", "not a mapping"),
    ("currency: [\n", "not valid YAML"),
    ("models: {}\n", "currency"),
    ("currency: ''\nmodels: {}\n", "currency"),
    ("currency: USD\n", "models"),
    ("currency: USD\nmodels: [a]\n", "models"),
    ("currency: USD\nmodels:\n  m: 3\n", "'m'"),
    ("currency: USD\nmodels:\n  m: {output: -1}\n", "negative"),
    ("currency: USD\nmodels:\n  m: {output: lots}\n", "not a number"),
    ("currency: USD\nmodels:\n  m: {output: true}\n", "not a number"),
    ("currency: USD\nmodels:\n  m: {output: .inf}\n", "not a number"),
    ("currency: USD\nmodels:\n  m: {thinking: 1}\n", "unknown bucket 'thinking'"),
    ("currency: USD\nmodels:\n  m: {tokens: 1}\n", "unknown bucket 'tokens'"),
    ("currency: USD\nmodels:\n  7: {output: 1}\n", "model id"),
])
def test_malformed_tables_raise(tmp_path, text, needle):
    path = tmp_path / "prices.yaml"
    path.write_text(text)
    with pytest.raises(prices.PriceTableError) as raised:
        prices.load_table(str(path))
    assert needle in str(raised.value)
    assert str(path) in str(raised.value)


def test_an_omitted_bucket_is_priced_at_zero(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text("currency: EUR\nmodels:\n  m: {output: 2}\n")
    table = prices.load_table(str(path))
    assert table.price_for("m") == {"input": 0.0, "cache_create": 0.0, "cache_read": 0.0, "output": 2.0}
    assert prices.cost(table, [_row("m", input=10**6, output=10**6)])["total"] == 2.0


def test_unpriced_weight_leaves_cache_reads_out():
    counters = buckets.make(input=3, cache_create=5, cache_read=1_000_000, output=7, thinking=2)
    assert prices.unpriced_weight(counters) == 15
    assert prices.unpriced_weight({}) == 0
