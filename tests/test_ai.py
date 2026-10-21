import json
import math

import pytest

from tradingagent.ai import (HistoricalEventModel, LLMAnalyst, LLMConfig, LLMError,
                             MarketContext, context_as_of, evidence_report)
from tradingagent.models import Candle, ScoreBreakdown, StrategySignal


def candles(count=500):
    rows = []
    price = 100.0
    for i in range(count):
        # Deterministic prices exist only as unit-test fixtures, never as training results.
        price *= 1.001 if (i // 30) % 2 == 0 else 0.999
        rows.append(Candle(i * 60_000, price, price * 1.004, price * .997,
                           price, 100 + (i % 13), "fixture-exchange", True))
    return rows


def test_ml_train_artifact_is_reproducible_and_has_holdout_metrics(tmp_path):
    path = tmp_path / "model.json"
    report = HistoricalEventModel().train(candles(), "BTC/USDT", str(path))
    artifact = json.loads(path.read_text())
    assert report.samples["design"] > 0 and report.samples["validation"] > 0
    assert report.samples["final_test"] > 0
    assert report.metrics["model"]["sample_size"] == report.samples["final_test"]
    assert "brier" in report.metrics["model"] and artifact["dataset_hash"] == report.dataset_hash
    assert artifact["feature_version"] == report.feature_version


def test_ml_requires_history_supported_symbol_and_single_source():
    with pytest.raises(ValueError):
        HistoricalEventModel().train(candles(80), "BTC/USDT")
    with pytest.raises(ValueError):
        HistoricalEventModel().train(candles(), "DOGE/USDT")
    rows = candles()
    rows[-1] = Candle(**{**{k: getattr(rows[-1], k) for k in
                         ("timestamp_ms", "open", "high", "low", "close", "volume", "complete")},
                         "source": "different"})
    with pytest.raises(ValueError):
        HistoricalEventModel().train(rows, "ETH/USDT")


def test_context_excludes_future_and_preserves_provenance():
    items = (MarketContext("news", "headline", 100, "wire", "unverified"),
             MarketContext("funding", "0.01", 101, "exchange", "high", True))
    assert context_as_of(items, 100) == items[:1]


def test_llm_schema_cost_limit_and_fail_open():
    config = LLMConfig("https://example.test/v1/chat/completions", "test-model", "secret",
                       max_cost_per_call=1)
    def transport(_body):
        return {"choices": [{"message": {"content": json.dumps({
            "summary": "Subiektywna interpretacja.", "conflicting_evidence": [],
            "missing_data": [], "uncertainty": ["mała próba"]})}}]}
    analyst = LLMAnalyst(config, transport)
    result = analyst.explain({"fact": "value"})
    assert result["interpretation_is_subjective"] and result["summary"]
    malformed = LLMAnalyst(config, lambda _: {"choices": [{"message": {"content": "{}"}}]})
    assert malformed.explain_safely({"fact": 1}) is None
    with pytest.raises(LLMError):
        LLMAnalyst(LLMConfig(config.endpoint, config.model, "secret", max_input_chars=100), transport).explain(
            {"long": "x" * 200})


def test_evidence_sections_do_not_conflate_ai_interpretation():
    score = ScoreBreakdown(*(0.0 for _ in range(9)))
    signal = StrategySignal("BTC/USDT", "binance", __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        "trend", "NO TRADE", "observation", None, None, None, None, None, (), None, None, None,
        ("15m",), (), (), (), 0.0, score)
    result = evidence_report(signal, {"close": 100}, {"rsi": 50}, "trend is unclear", ["sample size"])
    assert result["facts"]["close"] == 100 and result["interpretation_is_subjective"]
