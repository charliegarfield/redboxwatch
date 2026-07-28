"""Classifier — detect the functional red-box pattern (spec §3.5).

LLM classification over extracted page text via the Anthropic Messages API.
Cheap model first pass (claude-haiku-4-5), escalate ambiguous/low-confidence
results to a stronger model (claude-sonnet-4-6). Temperature 0, strict JSON.

Design notes:
- The base system/classifier prompt (PROMPT) is loaded from the untracked
  ``data/prompts/classifier.txt`` when present, falling back to a generic
  starter prompt. The production prompt is deliberately not published:
  publishing the exact operational wording would let campaigns adversarially
  test evasions against it. The starter prompt is functional — tune your own
  against the labeled fixtures (``tests/test_classifier_fixtures.py``).
- Strict JSON is enforced with the Messages API ``output_config.format``
  (json_schema) so the model cannot emit prose; we still defensively parse.
- The system prompt is marked cacheable. NB: at ~500 tokens it is below the
  4096-token cache minimum for haiku/sonnet, so caching won't engage until the
  prompt grows — the marker is correct and harmless either way.
- Long pages are chunked; each chunk is classified and results are combined by
  max severity with unioned evidence (spec §3.5).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .util import now_iso

# --- base classifier prompt --------------------------------------------------
# The operational prompt is loaded from this untracked file when present; the
# fallback below is a functional generic starter (see module docstring).
PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "prompts" / "classifier.txt"
)

_STARTER_PROMPT = """You are an analyst detecting "red-boxing" in U.S. political campaign web content.
Red-boxing is publicly posted messaging or media-buy guidance whose function is to
direct an outside group (e.g., a super PAC) on what advertising to run, for whom,
where, when, and with what message — without a private conversation.

Classify the page by its FUNCTION, not its styling. Red-box guidance pairs
segmented audiences with media-buy directives: which voters should see or read
what, on which channels or in which geographies, with what message themes or ad
assets. Guidance addressed to a different audience is NOT red-boxing: standard
press kits, donation or volunteer appeals, issue pages that persuade voters
directly, and internal notes to the campaign's own press shop or to journalists.
Ask who the intended reader is — a page is red_box_guidance only when its
directives are plausibly addressed to an outside spender who will run paid
advertising. When the audience is genuinely unclear, use "ambiguous".

Return ONLY a JSON object, no prose, with this schema:
{
  "classification": "red_box_guidance" | "ambiguous" | "no_guidance_detected",
  "confidence": <float 0-1>,
  "evidence": [ { "quote": "<verbatim span from the page>", "why": "<which signal>" } ],
  "rationale": "<2-3 sentence explanation>"
}
Quote only short spans actually present in the input. If the page mixes a normal
press kit with directive guidance, classify on the directive guidance."""

PROMPT = (
    PROMPT_PATH.read_text() if PROMPT_PATH.exists() else _STARTER_PROMPT
)

# JSON schema for strict output (output_config.format). Note: structured outputs
# don't support numeric min/max, so confidence is a plain number.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["red_box_guidance", "ambiguous", "no_guidance_detected"],
        },
        "confidence": {"type": "number"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["quote", "why"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["classification", "confidence", "evidence", "rationale"],
    "additionalProperties": False,
}

SEVERITY = {"no_guidance_detected": 0, "ambiguous": 1, "red_box_guidance": 2}


@dataclass
class Classification:
    classification: str
    confidence: float
    evidence: list[dict[str, str]]
    rationale: str
    model: str = ""
    escalated: bool = False
    classified_at: str = field(default_factory=now_iso)

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.classification, 0)

    @property
    def routes_to_review(self) -> bool:
        # red_box_guidance and ambiguous -> archive + review (spec §3.5).
        return self.classification in ("red_box_guidance", "ambiguous")


class LLM(Protocol):
    """Minimal interface the classifier needs; satisfied by AnthropicLLM and fakes."""

    def classify_chunk(self, text: str, *, model: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
class AnthropicLLM:
    """Real Messages API backend."""

    def __init__(self, api_key: str, *, max_tokens: int = 1024, max_retries: int = 5,
                 rate_limiter=None):
        import anthropic

        # Raise the SDK's retry ceiling (default 2): it backs off on connection
        # errors, timeouts, 429s and 5xx, so a transient DNS/network blip mid-scan
        # is absorbed here. A failure that survives all retries is caught at the
        # pipeline level (the page is skipped and retried next scan, not fatal).
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
        self.max_tokens = max_tokens
        # Optional process-wide token-rate limiter (tokens/min ceiling). Shared
        # across all scan-all worker threads so sustained throughput stays under
        # the org's per-model limit rather than relying on 429 retries.
        self._rate_limiter = rate_limiter

    def classify_chunk(self, text: str, *, model: str) -> dict[str, Any]:
        if self._rate_limiter is not None:
            from .ratelimit_tokens import estimate_input_tokens
            # Meter against THIS model's bucket — first-pass and escalation have
            # separate org ceilings (a no-op for a single-bucket limiter).
            # Metered with the FULL config string (an 'anthropic/' prefix
            # included): limiter buckets are keyed by config model names, and
            # metering the bare wire name silently missed the bucket, running
            # the scan unthrottled. Same convention as OpenAICompatLLM.
            self._rate_limiter.acquire(estimate_input_tokens(text), model=model)
        wire_model = model.split("/", 1)[1] if model.startswith("anthropic/") else model
        resp = self._client.messages.create(
            model=wire_model,
            max_tokens=self.max_tokens,
            temperature=0,
            system=[{
                "type": "text",
                "text": PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": f"PAGE TEXT:\n\n{text}"}],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        raw = next((b.text for b in resp.content if b.type == "text"), "{}")
        return _parse_json(raw)


class OpenAICompatLLM:
    """OpenAI-compatible chat.completions backend (Fireworks.ai, OpenAI).

    Same ``classify_chunk`` interface as :class:`AnthropicLLM`, over the
    /chat/completions wire shape. Two dialects:

    - ``fireworks``: ``temperature 0`` + ``max_tokens`` +
      ``response_format {type: json_object, schema}``. Model names are wrapped
      to ``accounts/fireworks/models/<name>`` on the wire.
    - ``openai``: ``max_completion_tokens`` (reasoning tokens count against it,
      so it gets extra headroom) + strict ``json_schema`` response_format +
      ``reasoning_effort: low``; ``temperature`` is omitted (GPT-5.x models
      reject non-default values). If the endpoint rejects ``reasoning_effort``
      (non-reasoning models), the call retries once without it.

    ``model`` may arrive prefixed (``fireworks/<model>``) from the
    router — the prefix is stripped for the wire but KEPT for token metering,
    so ``tokens_per_minute_by_model`` keys match config model strings.
    Inject ``post`` (payload -> parsed response dict) to test offline.
    """

    def __init__(self, base_url: str, api_key: str | None, *, dialect: str = "openai",
                 max_tokens: int = 1024, max_retries: int = 5, timeout: float = 120.0,
                 rate_limiter=None, post: Any = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dialect = dialect
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.timeout = timeout
        self._rate_limiter = rate_limiter
        self._post = post or self._http_post

    def _http_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import time

        import httpx

        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = httpx.post(f"{self.base_url}/chat/completions",
                               headers={"Authorization": f"Bearer {self.api_key}"},
                               json=payload, timeout=self.timeout)
            except httpx.HTTPError as e:      # connection blip / timeout: retry
                last = e
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(3.0 * (attempt + 1))
                continue
            if r.status_code >= 400:          # other 4xx: not retryable — raise
                # with the body so callers can inspect the rejected parameter
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
            return r.json()
        raise last or RuntimeError("retries exhausted")

    def _payload(self, text: str, wire_model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": wire_model,
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": f"PAGE TEXT:\n\n{text}"}],
        }
        if self.dialect == "fireworks":
            payload["temperature"] = 0
            payload["max_tokens"] = self.max_tokens
            payload["response_format"] = {"type": "json_object", "schema": OUTPUT_SCHEMA}
        else:
            # Headroom: OpenAI reasoning tokens draw from max_completion_tokens.
            payload["max_completion_tokens"] = max(4096, self.max_tokens)
            payload["reasoning_effort"] = "low"
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "redbox_classification", "strict": True, "schema": OUTPUT_SCHEMA}}
        return payload

    def _wire_model(self, model: str) -> str:
        bare = model.split("/", 1)[1] if "/" in model else model
        if self.dialect == "fireworks":
            return f"accounts/fireworks/models/{bare}"
        return bare

    def classify_chunk(self, text: str, *, model: str) -> dict[str, Any]:
        if self._rate_limiter is not None:
            from .ratelimit_tokens import estimate_input_tokens
            self._rate_limiter.acquire(estimate_input_tokens(text), model=model)
        payload = self._payload(text, self._wire_model(model))
        try:
            resp = self._post(payload)
        except Exception as e:
            # Non-reasoning OpenAI models reject reasoning_effort; retry without.
            if self.dialect == "openai" and "reasoning_effort" in str(e):
                payload.pop("reasoning_effort", None)
                resp = self._post(payload)
            else:
                raise
        raw = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or "{}"
        return _parse_json(raw)


class RouterLLM:
    """Dispatch ``classify_chunk`` to a provider backend by model-name prefix.

    Config model strings are ``provider/model`` (``fireworks/<model>``,
    ``openai/gpt-5.4-mini``); an unprefixed name means Anthropic — so existing
    configs (``claude-haiku-4-5``) keep working unchanged. The full prefixed
    string is passed through to the backend (metering keys stay config-aligned);
    backends strip the prefix for the wire.
    """

    def __init__(self, backends: dict[str, Any]):
        self._backends = backends

    @staticmethod
    def provider_of(model: str) -> str:
        return model.split("/", 1)[0] if "/" in model else "anthropic"

    def classify_chunk(self, text: str, *, model: str) -> dict[str, Any]:
        provider = self.provider_of(model)
        backend = self._backends.get(provider)
        if backend is None:
            raise ValueError(f"no LLM backend configured for provider {provider!r} "
                             f"(model {model!r}) — is its API key set?")
        # The full prefixed string goes through: every backend meters with the
        # config-aligned name and strips the prefix itself for the wire.
        return backend.classify_chunk(text, model=model)


def build_llm(*, first_pass: str, escalation: str, anthropic_api_key: str | None = None,
              openai_api_key: str | None = None, fireworks_api_key: str | None = None,
              max_tokens: int = 1024, rate_limiter=None):
    """Construct the LLM backend(s) the configured models actually need.

    Single-provider Anthropic configs get a plain :class:`AnthropicLLM`
    (unchanged behavior); mixed-provider configs get a :class:`RouterLLM`.
    """
    providers = {RouterLLM.provider_of(first_pass), RouterLLM.provider_of(escalation)}
    if providers == {"anthropic"}:
        return AnthropicLLM(anthropic_api_key, max_tokens=max_tokens,
                            rate_limiter=rate_limiter)
    backends: dict[str, Any] = {}
    if "anthropic" in providers:
        backends["anthropic"] = AnthropicLLM(anthropic_api_key, max_tokens=max_tokens,
                                             rate_limiter=rate_limiter)
    if "fireworks" in providers:
        backends["fireworks"] = OpenAICompatLLM(
            "https://api.fireworks.ai/inference/v1", fireworks_api_key,
            dialect="fireworks", max_tokens=max_tokens, rate_limiter=rate_limiter)
    if "openai" in providers:
        backends["openai"] = OpenAICompatLLM(
            "https://api.openai.com/v1", openai_api_key,
            dialect="openai", max_tokens=max_tokens, rate_limiter=rate_limiter)
    unknown = providers - {"anthropic", "fireworks", "openai"}
    if unknown:
        raise ValueError(f"unknown LLM provider prefix(es): {sorted(unknown)} — "
                         f"supported: anthropic (unprefixed), fireworks/, openai/")
    return RouterLLM(backends)


def _parse_json(raw: str) -> dict[str, Any]:
    """Parse model output; tolerate stray prose by extracting the JSON object."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
    # Could not parse — treat as ambiguous so it goes to review rather than
    # being silently dropped as a negative.
    return {
        "classification": "ambiguous",
        "confidence": 0.0,
        "evidence": [],
        "rationale": "Classifier output could not be parsed as JSON.",
    }


# ---------------------------------------------------------------------------
class Classifier:
    def __init__(
        self,
        llm: LLM,
        *,
        first_pass_model: str = "claude-haiku-4-5",
        escalation_model: str = "claude-sonnet-4-6",
        chunk_chars: int = 40000,
        escalate_below: float = 0.75,
        concurrency: int = 8,
        executor=None,
    ) -> None:
        self.llm = llm
        self.first_pass_model = first_pass_model
        self.escalation_model = escalation_model
        self.chunk_chars = chunk_chars
        self.escalate_below = escalate_below
        # Max chunks classified concurrently (LLM calls are I/O-bound HTTP).
        self.concurrency = max(1, concurrency)
        # Optional SHARED chunk-classification pool. When many candidates are
        # scanned at once, a private per-page pool means up to
        # workers x concurrency in-flight LLM calls; a single shared executor
        # passed in by scan-all caps the global fan-out instead. None -> each
        # multi-chunk page uses its own short-lived pool (fine for one candidate).
        self._executor = executor

    # --- chunking ----------------------------------------------------
    def _chunks(self, text: str) -> list[str]:
        text = text or ""
        if len(text) <= self.chunk_chars:
            return [text] if text.strip() else [""]
        # Split on paragraph boundaries, packing up to chunk_chars per chunk.
        paras = text.split("\n\n")
        chunks, cur = [], ""
        for p in paras:
            if len(cur) + len(p) + 2 > self.chunk_chars and cur:
                chunks.append(cur)
                cur = ""
            cur += (p + "\n\n")
            # A single oversized paragraph is hard-split.
            while len(cur) > self.chunk_chars:
                chunks.append(cur[: self.chunk_chars])
                cur = cur[self.chunk_chars:]
        if cur.strip():
            chunks.append(cur)
        return chunks or [""]

    def _should_escalate(self, result: dict[str, Any]) -> bool:
        cls = result.get("classification")
        conf = float(result.get("confidence") or 0.0)
        if cls == "ambiguous":
            return True
        # Low-confidence positives/negatives also escalate (spec §3.5).
        return conf < self.escalate_below

    def _classify_chunk(self, chunk: str) -> tuple[dict[str, Any], str, bool]:
        """First pass + optional escalation for one chunk. Independent of others."""
        first = self.llm.classify_chunk(chunk, model=self.first_pass_model)
        if self._should_escalate(first):
            second = self.llm.classify_chunk(chunk, model=self.escalation_model)
            return second, self.escalation_model, True
        return first, self.first_pass_model, False

    # --- public API --------------------------------------------------
    def classify_text(self, text: str) -> Classification:
        """Classify a full page: chunk, classify chunks (concurrently), combine.

        Chunks are independent and the result is order-insensitive (max severity),
        so chunks are classified in a bounded thread pool. A single chunk runs
        inline to avoid pool overhead on the common case.
        """
        chunks = self._chunks(text)
        if len(chunks) == 1:
            results = [self._classify_chunk(chunks[0])]
        elif self._executor is not None:
            # Shared, globally-bounded pool (scan-all). Safe from deadlock: chunk
            # tasks never submit back into this pool, so the calling worker only
            # ever waits on tasks that can make progress.
            results = list(self._executor.map(self._classify_chunk, chunks))
        else:
            from concurrent.futures import ThreadPoolExecutor

            workers = min(self.concurrency, len(chunks))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(self._classify_chunk, chunks))
        return self._combine(results)

    @staticmethod
    def _combine(results: list[tuple[dict[str, Any], str, bool]]) -> Classification:
        # Max-severity wins; union evidence across all chunks of that severity.
        best = max(results, key=lambda r: SEVERITY.get(r[0].get("classification"), 0))
        best_sev = SEVERITY.get(best[0].get("classification"), 0)
        evidence: list[dict[str, str]] = []
        confidences: list[float] = []
        escalated_any = False
        for res, _model, escalated in results:
            escalated_any = escalated_any or escalated
            if SEVERITY.get(res.get("classification"), 0) == best_sev:
                evidence.extend(res.get("evidence") or [])
                confidences.append(float(res.get("confidence") or 0.0))
        return Classification(
            classification=best[0].get("classification", "no_guidance_detected"),
            confidence=max(confidences) if confidences else 0.0,
            evidence=evidence,
            rationale=best[0].get("rationale", ""),
            model=best[1],
            escalated=escalated_any,
        )
