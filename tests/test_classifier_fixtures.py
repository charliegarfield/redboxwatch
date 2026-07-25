"""Live classifier acceptance test against labeled fixtures (spec acceptance #3).

Hits the real APIs of whatever models config.yaml selects (first_pass +
escalation, any provider), so it is gated behind REDBOX_LIVE_LLM=1 to keep the
default test run free and offline. Run explicitly:

    REDBOX_LIVE_LLM=1 python -m pytest tests/test_classifier_fixtures.py -v

Requires the API key(s) for the configured providers (loaded from .env via
redbox.config).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from redbox.classifier import Classifier, build_llm
from redbox.config import load_config

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "pages"

pytestmark = pytest.mark.skipif(
    os.environ.get("REDBOX_LIVE_LLM") != "1",
    reason="set REDBOX_LIVE_LLM=1 to run the live classifier acceptance test",
)


def _cases():
    labels = json.loads((FIXTURE_DIR / "labels.json").read_text())["labels"]
    return [(c["file"], tuple(c["accept"]), c.get("note", "")) for c in labels]


@pytest.fixture(scope="module")
def classifier():
    cfg = load_config()
    models = cfg.models
    llm = build_llm(
        first_pass=models.get("first_pass", "claude-haiku-4-5"),
        escalation=models.get("escalation", "claude-sonnet-4-6"),
        anthropic_api_key=cfg.anthropic_api_key,
        openai_api_key=cfg.openai_api_key,
        fireworks_api_key=cfg.fireworks_api_key,
        max_tokens=models.get("max_tokens", 1024))
    return Classifier(
        llm,
        first_pass_model=models.get("first_pass", "claude-haiku-4-5"),
        escalation_model=models.get("escalation", "claude-sonnet-4-6"),
        chunk_chars=models.get("chunk_chars", 12000),
        escalate_below=cfg.get("confidence_thresholds", {}).get("escalate_below", 0.75),
    )


@pytest.mark.parametrize("filename,accept,note", _cases())
def test_fixture_classification(classifier, filename, accept, note):
    text = (FIXTURE_DIR / filename).read_text()
    result = classifier.classify_text(text)
    assert result.classification in accept, (
        f"{filename}: got {result.classification} (conf={result.confidence}); "
        f"expected one of {accept}. rationale={result.rationale!r}"
    )
    # Positives must carry verbatim evidence quotes drawn from the page.
    if result.classification == "red_box_guidance":
        assert result.evidence, f"{filename}: positive with no evidence"
        for ev in result.evidence:
            assert ev["quote"].strip(), f"{filename}: empty evidence quote"
