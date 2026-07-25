"""Cross-provider classifier eval against RedboxFinder ground truths.

Eval set:
  A. fixtures/pages (7, authoritative accept-lists)
  B. DB red_box_guidance detections (12, real NY red boxes -> must route to review)
  C. stratified sample of DB negatives (presumed no_guidance; disagreements dumped
     for manual inspection rather than auto-counted as errors)

Every model gets the EXACT production prompt (redbox.classifier.PROMPT), temp 0
where supported, JSON schema enforced where the provider allows.

Usage: set -a; source .env; set +a; .venv/bin/python scripts/eval_classifier_models.py [--models m1,m2] [--neg N]
Results: eval_results.jsonl + printed summary.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from redbox.classifier import OUTPUT_SCHEMA, PROMPT, _parse_json  # noqa: E402
from redbox import prefilter  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent.parent / "data" / "eval_results.jsonl"
CHUNK_CHARS = 40000

# ---------------------------------------------------------------- eval set
def build_eval_set(n_neg: int = 150, seed: int = 42) -> list[dict]:
    cases = []
    fdir = REPO / "fixtures" / "pages"
    for c in json.loads((fdir / "labels.json").read_text())["labels"]:
        cases.append({
            "id": f"fixture:{c['file']}", "source": "fixture",
            "text": (fdir / c["file"]).read_text()[:CHUNK_CHARS],
            "accept": c["accept"],
        })
    conn = sqlite3.connect(REPO / "data" / "redbox.sqlite")
    conn.row_factory = sqlite3.Row
    for r in conn.execute(
        """SELECT d.detection_id, s.url, s.raw_text FROM detections d
           JOIN scans s USING(scan_id) WHERE d.classification='red_box_guidance'"""):
        cases.append({
            "id": f"db_pos:{r['detection_id']}:{r['url']}", "source": "db_positive",
            "text": r["raw_text"][:CHUNK_CHARS],
            # operational requirement: must ROUTE TO REVIEW (red_box or ambiguous)
            "accept": ["red_box_guidance", "ambiguous"],
        })
    negs = [dict(r) for r in conn.execute(
        """SELECT d.detection_id, s.url, s.raw_text FROM detections d
           JOIN scans s USING(scan_id)
           WHERE d.classification='no_guidance_detected'
             AND s.raw_text IS NOT NULL AND length(s.raw_text) > 200""")]
    conn.close()
    hard = [n for n in negs if prefilter._is_media(n["url"]) or prefilter.signal_score(n["raw_text"]) > 0]
    easy = [n for n in negs if n not in hard]
    rng = random.Random(seed)
    n_hard = min(50, len(hard), n_neg)
    picked = (rng.sample(hard, n_hard)
              + rng.sample(easy, max(0, min(n_neg - n_hard, len(easy)))))
    for r in picked:
        cases.append({
            "id": f"db_neg:{r['detection_id']}:{r['url']}", "source": "db_negative",
            "text": r["raw_text"][:CHUNK_CHARS],
            "accept": ["no_guidance_detected"],   # presumed — disagreements inspected
        })
    return cases


# ---------------------------------------------------------------- providers
def call_anthropic(model: str, text: str) -> dict:
    import anthropic
    client = call_anthropic._client
    if client is None:
        client = call_anthropic._client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    resp = client.messages.create(
        model=model, max_tokens=1024, temperature=0,
        system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"PAGE TEXT:\n\n{text}"}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    raw = next((b.text for b in resp.content if b.type == "text"), "{}")
    u = resp.usage
    return {"raw": raw, "in_tok": u.input_tokens + (u.cache_read_input_tokens or 0)
            + (u.cache_creation_input_tokens or 0), "out_tok": u.output_tokens}
call_anthropic._client = None

_http = httpx.Client(timeout=120.0)
_lock = threading.Lock()

def _openai_compat(url: str, key: str, payload: dict) -> dict:
    for attempt in range(5):
        try:
            r = _http.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
        except httpx.HTTPError:
            time.sleep(2 * (attempt + 1)); continue
        if r.status_code == 400:
            raise RuntimeError(f"400: {r.text[:300]}")
        if r.status_code >= 429:
            time.sleep(3 * (attempt + 1)); continue
        r.raise_for_status()
        d = r.json()
        u = d.get("usage", {})
        return {"raw": d["choices"][0]["message"].get("content") or "{}",
                "in_tok": u.get("prompt_tokens", 0), "out_tok": u.get("completion_tokens", 0)}
    raise RuntimeError("retries exhausted")

def call_openai(model: str, text: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": f"PAGE TEXT:\n\n{text}"}],
        "max_completion_tokens": 4096,   # headroom: reasoning tokens count against this
        "reasoning_effort": "low",
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "redbox", "strict": True, "schema": OUTPUT_SCHEMA}},
    }
    try:
        return _openai_compat("https://api.openai.com/v1/chat/completions",
                              os.environ["OPENAI_API_KEY"], payload)
    except RuntimeError as e:
        if "400" in str(e) and "reasoning_effort" in str(e):
            payload.pop("reasoning_effort")
            return _openai_compat("https://api.openai.com/v1/chat/completions",
                                  os.environ["OPENAI_API_KEY"], payload)
        raise

def call_fireworks(model: str, text: str) -> dict:
    payload = {
        "model": f"accounts/fireworks/models/{model}",
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": f"PAGE TEXT:\n\n{text}"}],
        "max_tokens": 2048, "temperature": 0,
        "response_format": {"type": "json_object", "schema": OUTPUT_SCHEMA},
    }
    return _openai_compat("https://api.fireworks.ai/inference/v1/chat/completions",
                          os.environ["FIREWORKS_API_KEY"], payload)

MODELS = {
    "claude-haiku-4-5":  lambda t: call_anthropic("claude-haiku-4-5", t),
    "gpt-5.4-nano":      lambda t: call_openai("gpt-5.4-nano", t),
    "gpt-5.4-mini":      lambda t: call_openai("gpt-5.4-mini", t),
    "deepseek-v4-flash": lambda t: call_fireworks("deepseek-v4-flash", t),
    "deepseek-v4-pro":   lambda t: call_fireworks("deepseek-v4-pro", t),
    "gpt-oss-120b":      lambda t: call_fireworks("gpt-oss-120b", t),
}

# ---------------------------------------------------------------- run
def run_model(name: str, fn, cases: list[dict], fh) -> list[dict]:
    def one(case):
        t0 = time.time()
        try:
            resp = fn(case["text"])
            parsed = _parse_json(resp["raw"])
            row = {"model": name, "id": case["id"], "source": case["source"],
                   "accept": case["accept"],
                   "classification": parsed.get("classification"),
                   "confidence": parsed.get("confidence"),
                   "rationale": (parsed.get("rationale") or "")[:400],
                   "evidence_n": len(parsed.get("evidence") or []),
                   "ok": parsed.get("classification") in case["accept"],
                   "in_tok": resp["in_tok"], "out_tok": resp["out_tok"],
                   "latency_s": round(time.time() - t0, 2)}
        except Exception as e:
            row = {"model": name, "id": case["id"], "source": case["source"],
                   "accept": case["accept"], "classification": "ERROR",
                   "error": str(e)[:300], "ok": False,
                   "in_tok": 0, "out_tok": 0, "latency_s": round(time.time() - t0, 2)}
        with _lock:
            fh.write(json.dumps(row) + "\n"); fh.flush()
        return row
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, cases))

def summarize(rows: list[dict]) -> str:
    by = {}
    for r in rows:
        by.setdefault(r["model"], []).append(r)
    lines = [f"{'model':18s} {'fixtures':>9s} {'db_pos(review)':>14s} {'db_pos(strict)':>14s} "
             f"{'neg_agree':>9s} {'errors':>6s} {'tok_in/out':>14s} {'p50_lat':>7s}"]
    for m, rs in by.items():
        fx = [r for r in rs if r["source"] == "fixture"]
        pos = [r for r in rs if r["source"] == "db_positive"]
        neg = [r for r in rs if r["source"] == "db_negative"]
        errs = sum(1 for r in rs if r["classification"] == "ERROR")
        strict = sum(1 for r in pos if r["classification"] == "red_box_guidance")
        lats = sorted(r["latency_s"] for r in rs)
        lines.append(
            f"{m:18s} {sum(r['ok'] for r in fx)}/{len(fx):<7d} "
            f"{sum(r['ok'] for r in pos)}/{len(pos):<12d} {strict}/{len(pos):<12d} "
            f"{sum(r['ok'] for r in neg)}/{len(neg):<7d} {errs:>6d} "
            f"{sum(r['in_tok'] for r in rs)/1000:>6.0f}k/{sum(r['out_tok'] for r in rs)/1000:.0f}k "
            f"{lats[len(lats)//2] if lats else 0:>7.1f}")
    return "\n".join(lines)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--neg", type=int, default=150)
    args = ap.parse_args()
    cases = build_eval_set(n_neg=args.neg)
    n = {s: sum(1 for c in cases if c['source'] == s) for s in ('fixture', 'db_positive', 'db_negative')}
    print(f"eval set: {len(cases)} cases {n}")
    all_rows = []
    with OUT.open("a") as fh:
        for name in args.models.split(","):
            name = name.strip()
            print(f"--- {name} ---", flush=True)
            t0 = time.time()
            rows = run_model(name, MODELS[name], cases, fh)
            all_rows.extend(rows)
            print(f"    done in {time.time()-t0:.0f}s", flush=True)
    print()
    print(summarize(all_rows))
