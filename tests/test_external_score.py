"""Tests for the source-aware external-cameraman scorer (analysis/external_score.py).

These are dependency-free: the YOLO detector is never loaded. Per-frame detection
is stubbed via ``_person_boxes`` so we exercise the geometry -> schema mapping and
the per-second bucketing on synthetic numpy frames, and the model cache is tested
with a fake ``_load_model``. No ffmpeg, no ultralytics, no sample media.
"""

from __future__ import annotations

import numpy as np
import pytest

from analysis import external_score

SCHEMA = {"ts", "smile", "eye_contact", "face_in_frame", "face_centered"}


def _frames(n: int):
    """``n`` blank 100x100 RGB frames, one per whole second (so each is its own bucket)."""
    return [(float(k), np.zeros((100, 100, 3), dtype=np.uint8)) for k in range(n)]


def test_external_scorer_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows carry exactly the schema fields, all floats in [0, 1]."""
    # One large, centred person box (60% of the 100x100 frame, centred).
    monkeypatch.setattr(
        external_score, "_person_boxes", lambda model, frame: [(20.0, 20.0, 80.0, 80.0)]
    )
    rows = external_score.score_frames_external(_frames(10), model=None)

    assert rows, "expected per-second rows"
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    for row in rows:
        assert set(row) == SCHEMA
        assert isinstance(row["ts"], float)
        for field in external_score.SCORE_FIELDS:
            assert isinstance(row[field], float)
            assert 0.0 <= row[field] <= 1.0
    # A big centred subject reads as fully in-frame and centred; smile is always 0.
    assert rows[0]["face_in_frame"] == 1.0
    assert rows[0]["face_centered"] == 1.0
    assert rows[0]["smile"] == 0.0


def test_external_scorer_empty_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame with no person detected scores 0.0 on every field."""
    monkeypatch.setattr(external_score, "_person_boxes", lambda model, frame: [])
    rows = external_score.score_frames_external(_frames(5), model=None)

    assert len(rows) == 5
    for row in rows:
        for field in external_score.SCORE_FIELDS:
            assert row[field] == 0.0


def test_both_visible_maps_to_eye_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly two well-separated people -> eye_contact 1.0 (the 'engaging shot' proxy)."""
    # Two non-overlapping boxes whose centres (40, 65) sit within 30% of frame width.
    two = [(30.0, 40.0, 50.0, 60.0), (55.0, 40.0, 75.0, 60.0)]
    monkeypatch.setattr(external_score, "_person_boxes", lambda model, frame: two)
    rows = external_score.score_frames_external(_frames(3), model=None)
    assert all(r["eye_contact"] == 1.0 for r in rows)


def test_yolo_model_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model object is loaded once and reused across calls."""
    calls = {"n": 0}
    sentinel = object()

    def fake_load() -> object:
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(external_score, "_load_model", fake_load)
    monkeypatch.setattr(external_score, "_MODEL", None)

    first = external_score.get_model()
    second = external_score.get_model()
    assert first is second is sentinel
    assert calls["n"] == 1, "model must load exactly once, not per call"
