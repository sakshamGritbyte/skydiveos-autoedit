"""Tests for the random default-music assignment (api.selfie._ensure_default_music).

Verify the "pick once, persist, reuse on replay" contract without running the heavy
pipeline: a real :class:`JobStore` on tmp_path, a temp ``templates/music`` pointed at
via ``$TEMPLATES_ROOT``, and an injected chooser so the pick is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.jobs import Job, JobStore
from api.selfie import _ensure_default_music
from render.templates import list_music


def _music_lib(root: Path, *names: str) -> Path:
    """Create templates/music/<name> tracks under root; return the templates root."""
    mdir = root / "music"
    mdir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (mdir / n).write_bytes(b"ID3")  # content irrelevant; only the path is used
    return root


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path)


def _job_with_booking(store: JobStore, tmp_path: Path, music: str | None) -> str:
    job = store.create(Job(job_id="j1", music=music))
    store.write_booking("j1", {"customer_name": "Jane", "music": music})
    return job.job_id


def test_list_music_lists_tracks_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _music_lib(tmp_path / "tpl", "b.mp3", "a.mp3", "notes.txt")
    monkeypatch.setenv("TEMPLATES_ROOT", str(root))
    assert [p.name for p in list_music()] == ["a.mp3", "b.mp3"]  # sorted, audio only


def test_assigns_random_default_and_persists(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _music_lib(tmp_path / "tpl", "sunrise.mp3", "chill.mp3", "epic.mp3")
    monkeypatch.setenv("TEMPLATES_ROOT", str(root))
    _job_with_booking(store, tmp_path, music=None)

    booking = {"customer_name": "Jane", "music": None}
    # Tracks sort alphabetically: [chill, epic, sunrise]; pick index 1 = "epic".
    out = _ensure_default_music(
        booking, "j1", store, tmp_path, choose=lambda tracks: tracks[1]
    )

    assert out["music"] == "epic"  # stem of the chosen track
    # Persisted to BOTH the job field and booking.json so replay reuses it.
    assert store.load("j1").music == "epic"
    import json

    saved = json.loads(store.booking_path("j1").read_text())
    assert saved["music"] == "epic"


def test_respects_an_existing_booking_choice(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _music_lib(tmp_path / "tpl", "sunrise.mp3", "chill.mp3")
    monkeypatch.setenv("TEMPLATES_ROOT", str(root))
    _job_with_booking(store, tmp_path, music="customer_pick")

    def _must_not_choose(tracks: object) -> object:
        raise AssertionError("should not pick a default when music is already set")

    booking = {"customer_name": "Jane", "music": "customer_pick"}
    out = _ensure_default_music(booking, "j1", store, tmp_path, choose=_must_not_choose)
    assert out["music"] == "customer_pick"
    assert store.load("j1").music == "customer_pick"


def test_reuse_on_second_call_is_stable(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulates first-process (picks) then replay (must reuse, not re-pick).
    root = _music_lib(tmp_path / "tpl", "a.mp3", "b.mp3", "c.mp3")
    monkeypatch.setenv("TEMPLATES_ROOT", str(root))
    _job_with_booking(store, tmp_path, music=None)

    first = _ensure_default_music(
        {"music": None}, "j1", store, tmp_path, choose=lambda t: t[0]
    )
    assert first["music"] == "a"

    # A replay re-reads the persisted booking.json; a second call with a *different*
    # chooser must NOT change the track (it's already set).
    import json

    reread = json.loads(store.booking_path("j1").read_text())
    second = _ensure_default_music(
        reread, "j1", store, tmp_path, choose=lambda t: t[2]  # would pick 'c' if it ran
    )
    assert second["music"] == "a"  # unchanged


def test_empty_library_leaves_music_unset(
    store: JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _music_lib(tmp_path / "tpl")  # no tracks
    monkeypatch.setenv("TEMPLATES_ROOT", str(root))
    _job_with_booking(store, tmp_path, music=None)

    out = _ensure_default_music({"music": None}, "j1", store, tmp_path)
    assert out.get("music") in (None, "")
    assert store.load("j1").music is None  # render just runs music-less, as before
