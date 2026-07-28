"""Offline unit tests for classifier logic (spec §3.5). No live API calls."""
from __future__ import annotations

from redbox.classifier import Classifier, _parse_json


class FakeLLM:
    """Deterministic stand-in. `script` maps (model) -> result for each call,
    or a callable(text, model) -> dict for content-aware behaviour."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[tuple[str, str]] = []

    def classify_chunk(self, text, *, model):
        self.calls.append((model, text))
        if callable(self.behaviour):
            return self.behaviour(text, model)
        return self.behaviour[model]


def _result(cls, conf=0.9, evidence=None, rationale="r"):
    return {"classification": cls, "confidence": conf,
            "evidence": evidence or [], "rationale": rationale}


def test_high_confidence_first_pass_does_not_escalate():
    llm = FakeLLM(lambda t, m: _result("no_guidance_detected", 0.95))
    c = Classifier(llm)
    res = c.classify_text("a normal press kit")
    assert res.classification == "no_guidance_detected"
    assert res.escalated is False
    assert [m for m, _ in llm.calls] == ["claude-haiku-4-5"]   # only first pass


def test_ambiguous_first_pass_escalates_to_sonnet():
    behaviour = {
        "claude-haiku-4-5": _result("ambiguous", 0.5),
        "claude-sonnet-4-6": _result("red_box_guidance", 0.92,
                                      [{"quote": "should see", "why": "directive"}]),
    }
    llm = FakeLLM(behaviour)
    c = Classifier(llm)
    res = c.classify_text("borderline page")
    assert res.classification == "red_box_guidance"
    assert res.escalated is True
    assert res.model == "claude-sonnet-4-6"
    assert [m for m, _ in llm.calls] == ["claude-haiku-4-5", "claude-sonnet-4-6"]


def test_low_confidence_positive_also_escalates():
    behaviour = {
        "claude-haiku-4-5": _result("red_box_guidance", 0.4),   # below escalate_below
        "claude-sonnet-4-6": _result("red_box_guidance", 0.97),
    }
    c = Classifier(FakeLLM(behaviour))
    res = c.classify_text("x")
    assert res.escalated is True
    assert res.confidence == 0.97


def test_long_page_chunks_and_takes_max_severity():
    # Force two chunks; second chunk contains the guidance.
    def behaviour(text, model):
        if "DIRECTIVE" in text:
            return _result("red_box_guidance", 0.9,
                           [{"quote": "DIRECTIVE", "why": "directive"}])
        return _result("no_guidance_detected", 0.95)
    llm = FakeLLM(behaviour)
    c = Classifier(llm, chunk_chars=50)
    text = ("normal content paragraph one\n\n" * 3) + "\n\nDIRECTIVE guidance here"
    res = c.classify_text(text)
    assert res.classification == "red_box_guidance"     # max severity wins
    assert any(e["quote"] == "DIRECTIVE" for e in res.evidence)
    assert len(llm.calls) >= 2                            # actually chunked


def test_large_chunk_size_collapses_to_single_call():
    # A page under chunk_chars must be one LLM call (no needless splitting).
    llm = FakeLLM(lambda t, m: _result("no_guidance_detected", 0.95))
    c = Classifier(llm, chunk_chars=40000)
    c.classify_text("x" * 30000)
    assert len(llm.calls) == 1


def test_concurrent_chunks_preserve_max_severity_and_union():
    # Many chunks, one positive — order-insensitive combine must still work.
    def behaviour(text, model):
        if "HIT" in text:
            return _result("red_box_guidance", 0.95, [{"quote": "HIT", "why": "x"}])
        return _result("no_guidance_detected", 0.95)
    llm = FakeLLM(behaviour)
    c = Classifier(llm, chunk_chars=20, concurrency=8)
    text = "\n\n".join(["filler block here"] * 10 + ["the HIT block"])
    res = c.classify_text(text)
    assert res.classification == "red_box_guidance"
    assert any(e["quote"] == "HIT" for e in res.evidence)


def test_chunks_actually_run_in_parallel():
    # With a slow LLM, N concurrent chunks finish in ~1 unit, not N units.
    import time

    class SlowLLM:
        def classify_chunk(self, text, *, model):
            time.sleep(0.2)
            return _result("no_guidance_detected", 0.95)
    c = Classifier(SlowLLM(), chunk_chars=10, concurrency=8)
    text = "\n\n".join(["aaaaaaaa"] * 8)        # 8 chunks
    t = time.time()
    c.classify_text(text)
    elapsed = time.time() - t
    assert elapsed < 0.8, f"expected concurrent (~0.2s), got {elapsed:.2f}s"


def test_shared_executor_classifies_multi_chunk_page():
    # A shared pool passed in must be used for chunk fan-out and still combine
    # to max severity.
    from concurrent.futures import ThreadPoolExecutor

    def behaviour(text, model):
        if "HIT" in text:
            return _result("red_box_guidance", 0.95, [{"quote": "HIT", "why": "x"}])
        return _result("no_guidance_detected", 0.95)
    with ThreadPoolExecutor(max_workers=3) as pool:
        c = Classifier(FakeLLM(behaviour), chunk_chars=20, executor=pool)
        text = "\n\n".join(["filler block here"] * 6 + ["the HIT block"])
        res = c.classify_text(text)
    assert res.classification == "red_box_guidance"
    assert any(e["quote"] == "HIT" for e in res.evidence)


def test_shared_executor_bounds_global_concurrency():
    # One shared pool (size 2) across several concurrent classify_text calls must
    # cap total in-flight chunk calls at 2 — not workers x concurrency.
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    class TrackingLLM:
        def __init__(self):
            self.lock = threading.Lock()
            self.cur = self.peak = 0

        def classify_chunk(self, text, *, model):
            with self.lock:
                self.cur += 1
                self.peak = max(self.peak, self.cur)
            time.sleep(0.05)
            with self.lock:
                self.cur -= 1
            return _result("no_guidance_detected", 0.95)

    llm = TrackingLLM()
    text = "\n\n".join(["aaaaaaaa"] * 6)        # multi-chunk page
    with ThreadPoolExecutor(max_workers=2) as chunk_pool:
        c = Classifier(llm, chunk_chars=10, executor=chunk_pool)
        # 4 "candidates" classify concurrently, all sharing the one chunk pool.
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: c.classify_text(text), range(4)))
    assert llm.peak <= 2, f"shared pool did not bound concurrency: peak={llm.peak}"


def test_routes_to_review_for_positive_and_ambiguous():
    assert Classifier(FakeLLM(lambda t, m: _result("red_box_guidance"))).classify_text("x").routes_to_review
    amb = {"claude-haiku-4-5": _result("ambiguous", 0.5),
           "claude-sonnet-4-6": _result("ambiguous", 0.5)}
    assert Classifier(FakeLLM(amb)).classify_text("x").routes_to_review
    neg = Classifier(FakeLLM(lambda t, m: _result("no_guidance_detected"))).classify_text("x")
    assert neg.routes_to_review is False


def test_router_passes_full_model_name_to_every_backend():
    from redbox.classifier import RouterLLM

    fw = FakeLLM(lambda t, m: _result("no_guidance_detected", 0.95))
    an = FakeLLM(lambda t, m: _result("ambiguous", 0.5))
    router = RouterLLM({"fireworks": fw, "anthropic": an})

    router.classify_chunk("x", model="fireworks/deepseek-v4-flash")
    assert fw.calls == [("fireworks/deepseek-v4-flash", "x")]  # full name passed through

    router.classify_chunk("y", model="claude-haiku-4-5")       # unprefixed -> anthropic
    router.classify_chunk("z", model="anthropic/claude-haiku-4-5")
    # Full config-aligned names reach the backend — stripping here broke
    # token metering (buckets are keyed by config strings); each backend
    # strips the prefix itself for the wire.
    assert [m for m, _ in an.calls] == ["claude-haiku-4-5", "anthropic/claude-haiku-4-5"]


def test_anthropic_llm_meters_config_name_and_strips_prefix_for_wire():
    from redbox.classifier import AnthropicLLM

    metered = []

    class Limiter:
        def acquire(self, n, *, model=None):
            metered.append(model)

    llm = AnthropicLLM("test-key", rate_limiter=Limiter())
    sent = {}

    class _Blk:
        type = "text"
        text = ('{"classification":"no_guidance_detected","confidence":0.9,'
                '"evidence":[],"rationale":"r"}')

    class _Resp:
        content = [_Blk()]

    llm._client.messages.create = lambda **kw: (sent.update(kw), _Resp())[1]
    llm.classify_chunk("some page text", model="anthropic/claude-haiku-4-5")
    assert metered == ["anthropic/claude-haiku-4-5"]   # bucket key = config string
    assert sent["model"] == "claude-haiku-4-5"         # bare name on the wire


def test_router_missing_backend_raises():
    import pytest

    from redbox.classifier import RouterLLM

    with pytest.raises(ValueError, match="no LLM backend"):
        RouterLLM({}).classify_chunk("x", model="openai/gpt-5.4-mini")


def test_openai_compat_llm_fireworks_payload_and_parse():
    from redbox.classifier import OUTPUT_SCHEMA, OpenAICompatLLM

    sent = {}
    def post(payload):
        sent.update(payload)
        return {"choices": [{"message": {"content":
            '{"classification":"red_box_guidance","confidence":0.9,"evidence":[],"rationale":"r"}'}}]}

    llm = OpenAICompatLLM("https://api.fireworks.ai/inference/v1", "k",
                          dialect="fireworks", max_tokens=1024, post=post)
    out = llm.classify_chunk("PAGE", model="fireworks/deepseek-v4-flash")
    assert out["classification"] == "red_box_guidance"
    assert sent["model"] == "accounts/fireworks/models/deepseek-v4-flash"
    assert sent["temperature"] == 0 and sent["max_tokens"] == 1024
    assert sent["response_format"] == {"type": "json_object", "schema": OUTPUT_SCHEMA}
    assert sent["messages"][1]["content"].endswith("PAGE")


def test_openai_compat_llm_openai_payload_retries_without_reasoning_effort():
    from redbox.classifier import OpenAICompatLLM

    calls = []
    def post(payload):
        calls.append(dict(payload))
        if "reasoning_effort" in payload:
            raise RuntimeError("400: Unrecognized request argument: reasoning_effort")
        return {"choices": [{"message": {"content":
            '{"classification":"no_guidance_detected","confidence":0.95,"evidence":[],"rationale":"r"}'}}]}

    llm = OpenAICompatLLM("https://api.openai.com/v1", "k", dialect="openai", post=post)
    out = llm.classify_chunk("PAGE", model="openai/gpt-5.4-mini")
    assert out["classification"] == "no_guidance_detected"
    assert len(calls) == 2
    assert calls[0]["model"] == "gpt-5.4-mini"            # prefix stripped for wire
    assert "reasoning_effort" not in calls[1]
    assert "max_completion_tokens" in calls[0] and "temperature" not in calls[0]
    assert calls[0]["response_format"]["json_schema"]["strict"] is True


def test_openai_compat_llm_meters_with_full_model_name():
    from redbox.classifier import OpenAICompatLLM

    metered = []
    class Limiter:
        def acquire(self, n, *, model=None): metered.append((n, model))
    def post(payload):
        return {"choices": [{"message": {"content":
            '{"classification":"no_guidance_detected","confidence":0.9,"evidence":[],"rationale":"r"}'}}]}

    llm = OpenAICompatLLM("http://x", "k", dialect="fireworks",
                          rate_limiter=Limiter(), post=post)
    llm.classify_chunk("some page text", model="fireworks/deepseek-v4-flash")
    # metering key keeps the config-facing prefixed name
    assert metered and metered[0][1] == "fireworks/deepseek-v4-flash"


def test_build_llm_single_anthropic_returns_plain_backend(monkeypatch):
    from redbox import classifier as cmod

    class StubAnthropic:
        def __init__(self, *a, **k): pass
    monkeypatch.setattr(cmod, "AnthropicLLM", StubAnthropic)
    llm = cmod.build_llm(first_pass="claude-haiku-4-5", escalation="claude-sonnet-4-6",
                         anthropic_api_key="k")
    assert isinstance(llm, StubAnthropic)      # no router for pure-Anthropic configs


def test_build_llm_mixed_providers_returns_router(monkeypatch):
    import pytest

    from redbox import classifier as cmod

    class StubAnthropic:
        def __init__(self, *a, **k): pass
    monkeypatch.setattr(cmod, "AnthropicLLM", StubAnthropic)
    llm = cmod.build_llm(first_pass="fireworks/deepseek-v4-flash",
                         escalation="claude-sonnet-4-6",
                         anthropic_api_key="k", fireworks_api_key="fk")
    assert isinstance(llm, cmod.RouterLLM)
    assert set(llm._backends) == {"anthropic", "fireworks"}
    assert llm._backends["fireworks"].dialect == "fireworks"

    with pytest.raises(ValueError, match="unknown LLM provider"):
        cmod.build_llm(first_pass="together/llama", escalation="claude-sonnet-4-6")


def test_classifier_end_to_end_with_router_escalation():
    # flash first-pass low confidence -> escalates to the Anthropic backend.
    from redbox.classifier import RouterLLM

    fw = FakeLLM(lambda t, m: _result("ambiguous", 0.5))
    an = FakeLLM(lambda t, m: _result("red_box_guidance", 0.95,
                                      [{"quote": "q", "why": "w"}]))
    c = Classifier(RouterLLM({"fireworks": fw, "anthropic": an}),
                   first_pass_model="fireworks/deepseek-v4-flash",
                   escalation_model="claude-sonnet-4-6")
    res = c.classify_text("borderline")
    assert res.classification == "red_box_guidance"
    assert res.escalated is True
    assert res.model == "claude-sonnet-4-6"
    assert fw.calls and an.calls


def test_parse_json_tolerates_prose_wrapping_and_falls_back():
    assert _parse_json('{"classification":"ambiguous","confidence":0.5,"evidence":[],"rationale":"x"}')["classification"] == "ambiguous"
    wrapped = _parse_json('Here is the JSON:\n{"classification":"no_guidance_detected","confidence":0.9,"evidence":[],"rationale":"x"} done')
    assert wrapped["classification"] == "no_guidance_detected"
    # Unparseable -> ambiguous (routes to review, not silently negative)
    assert _parse_json("not json at all")["classification"] == "ambiguous"
