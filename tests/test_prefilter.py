"""Tests for the cheap pre-filter (spec: cost/speed without losing recall).

The core invariant: the filter must never skip a real red box. Media pages and
PDFs always scan; a page is skipped ONLY when its URL is boilerplate AND it has
zero red-box signal.
"""
from __future__ import annotations

from redbox.prefilter import decide, signal_score

# Synthetic phrasing modeled on the functional pattern of live /media red boxes.
PROSE_MEDIA = ("Younger primary voters should see this on the go and in their "
               "mailboxes. Voters likely to vote in the primary should read it. "
               "B-roll and updated stills are available.")
SEGMENT_MEDIA = ("District residents age 25 or older need to see on YouTube, voters "
                 "who have voted need to read mail and see on Meta, CTV, and broadcast "
                 "TV. All video content should tell voters what the opponent's vote did.")
PRESSKIT = ("Press & Media Kit. Downloads: campaign logo, candidate headshots, "
            "approved biography, press-release archives, media-contact info.")
DONATE = "Chip in $25 today to help us reach voters before the primary. Donate now."


def test_media_path_always_scans_even_if_empty():
    # A media page with no text on this crawl still must scan (box may be hidden).
    assert decide("https://x.com/media", "", "text/html").scan
    assert decide("https://x.com/media-kit", "nothing here", None).scan
    assert decide("https://x.com/press", "", None).reason == "media_or_pdf"


def test_pdf_always_scans():
    assert decide("https://x.com/s/Medicaid-Vote-Press-Release.pdf", "", None).scan
    assert decide("https://x.com/doc", "x", "application/pdf").scan


def test_real_redbox_text_scores_and_scans():
    assert signal_score(PROSE_MEDIA) >= 2
    assert signal_score(SEGMENT_MEDIA) >= 2
    # Even at a non-media URL, signal forces a scan.
    assert decide("https://x.com/somepage", SEGMENT_MEDIA, None).scan
    assert decide("https://x.com/whatever", PROSE_MEDIA, None).reason == "signal"


def test_boilerplate_with_no_signal_is_skipped():
    for url in ["https://x.com/donate", "https://x.com/privacy-policy",
                "https://x.com/volunteer", "https://x.com/cart",
                "https://x.com/careers/field-organizer", "https://x.com/terms-of-use"]:
        d = decide(url, DONATE, None)   # donate text -> no red-box signal
        assert not d.scan, url
        assert d.reason == "boilerplate_empty"


def test_boilerplate_WITH_signal_still_scans():
    # If a donate page somehow contained red-box directives, it must NOT be skipped.
    assert decide("https://x.com/donate", SEGMENT_MEDIA, None).scan


def test_unknown_page_scans_by_default():
    # A non-media, non-boilerplate page with no signal still scans (conservative).
    d = decide("https://x.com/about/our-team", "Meet our team and staff.", None)
    assert d.scan and d.reason == "default"


def test_presskit_negative_at_media_path_scans_but_at_odd_path_defaults():
    # A press kit lives at /media -> scans (correctly, it's the high-risk path).
    assert decide("https://x.com/media", PRESSKIT, None).scan
    # The same text at a random path: no signal, not boilerplate -> still scans.
    assert decide("https://x.com/random", PRESSKIT, None).scan
