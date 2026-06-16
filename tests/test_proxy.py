"""Tests for analysis source selection (analysis.proxy).

Covers the LRV-first / MP4-fallback seam: proxy discovery, the four validation
checks, and the total-fallback contract of :func:`analysis_source` (flag off, no
proxy, failed validation, and unexpected errors all return the MP4).

ffprobe is not invoked — the small probe helpers are monkeypatched so the tests run
without media files and assert pure decision logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from analysis import proxy
from analysis.proxy import ProxyVerdict, analysis_source, find_proxy, validate_proxy

# --------------------------------------------------------------------------- #
# find_proxy
# --------------------------------------------------------------------------- #


def test_find_proxy_same_stem(tmp_path: Path) -> None:
    mp4 = tmp_path / "GH010001.MP4"
    mp4.touch()
    lrv = tmp_path / "GH010001.LRV"
    lrv.touch()
    assert find_proxy(mp4) == lrv


def test_find_proxy_gl_prefix(tmp_path: Path) -> None:
    """GoPro's native naming: GX010084.MP4 -> GL010084.LRV."""
    mp4 = tmp_path / "GX010084.MP4"
    mp4.touch()
    lrv = tmp_path / "GL010084.LRV"
    lrv.touch()
    assert find_proxy(mp4) == lrv


def test_find_proxy_lowercase_suffix(tmp_path: Path) -> None:
    mp4 = tmp_path / "GX010084.MP4"
    mp4.touch()
    lrv = tmp_path / "GL010084.lrv"
    lrv.touch()
    found = find_proxy(mp4)
    # ``samefile`` (not ``==``) so the assertion holds on case-insensitive filesystems,
    # where the upper-case candidate resolves to the same lower-case file on disk.
    assert found is not None and found.samefile(lrv)


def test_find_proxy_absent(tmp_path: Path) -> None:
    mp4 = tmp_path / "GX010084.MP4"
    mp4.touch()
    assert find_proxy(mp4) is None


# --------------------------------------------------------------------------- #
# validate_proxy — the four required checks
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _ok_probes(monkeypatch: MonkeyPatch) -> None:
    """Make every probe pass: equal durations, readable video + GPMF."""
    monkeypatch.setattr(proxy, "_probe_duration", lambda p: 14.798)
    monkeypatch.setattr(proxy, "_has_readable_video", lambda p: True)
    monkeypatch.setattr(proxy, "_has_readable_gpmf", lambda p: True)


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    mp4 = tmp_path / "GX010084.MP4"
    mp4.touch()
    lrv = tmp_path / "GL010084.LRV"
    lrv.touch()
    return mp4, lrv


def test_validate_ok(tmp_path: Path, _ok_probes: None) -> None:
    mp4, lrv = _pair(tmp_path)
    v = validate_proxy(mp4)
    assert v.use_proxy is True
    assert v.proxy_path == lrv


def test_validate_missing_lrv(tmp_path: Path, _ok_probes: None) -> None:
    mp4 = tmp_path / "GX010084.MP4"
    mp4.touch()
    v = validate_proxy(mp4)
    assert v.use_proxy is False
    assert "no matching LRV" in v.reason


def test_validate_duration_mismatch(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    mp4, _ = _pair(tmp_path)
    # 0.5s apart — exceeds the 0.1s tolerance.
    durs = {"MP4": 14.80, "LRV": 14.30}
    monkeypatch.setattr(proxy, "_probe_duration", lambda p: durs[Path(p).suffix.lstrip(".")])
    monkeypatch.setattr(proxy, "_has_readable_video", lambda p: True)
    monkeypatch.setattr(proxy, "_has_readable_gpmf", lambda p: True)
    v = validate_proxy(mp4)
    assert v.use_proxy is False
    assert "duration mismatch" in v.reason


def test_validate_within_tolerance(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """A sub-0.1s difference is accepted."""
    mp4, _ = _pair(tmp_path)
    durs = {"MP4": 14.800, "LRV": 14.750}  # 0.05s
    monkeypatch.setattr(proxy, "_probe_duration", lambda p: durs[Path(p).suffix.lstrip(".")])
    monkeypatch.setattr(proxy, "_has_readable_video", lambda p: True)
    monkeypatch.setattr(proxy, "_has_readable_gpmf", lambda p: True)
    assert validate_proxy(mp4).use_proxy is True


def test_validate_unreadable_video(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    mp4, _ = _pair(tmp_path)
    monkeypatch.setattr(proxy, "_probe_duration", lambda p: 14.798)
    monkeypatch.setattr(proxy, "_has_readable_video", lambda p: False)
    monkeypatch.setattr(proxy, "_has_readable_gpmf", lambda p: True)
    v = validate_proxy(mp4)
    assert v.use_proxy is False
    assert "video" in v.reason


def test_validate_missing_gpmf(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    mp4, _ = _pair(tmp_path)
    monkeypatch.setattr(proxy, "_probe_duration", lambda p: 14.798)
    monkeypatch.setattr(proxy, "_has_readable_video", lambda p: True)
    monkeypatch.setattr(proxy, "_has_readable_gpmf", lambda p: False)
    v = validate_proxy(mp4)
    assert v.use_proxy is False
    assert "GPMF" in v.reason


# --------------------------------------------------------------------------- #
# analysis_source — flag gating + total fallback
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("_ok_probes")
def test_source_flag_off_returns_mp4(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Default (flag off): even with a perfect proxy, analysis reads the MP4."""
    mp4, _ = _pair(tmp_path)
    monkeypatch.setattr(proxy, "_flag_enabled", lambda: False)
    assert analysis_source(mp4) == str(mp4)


@pytest.mark.usefixtures("_ok_probes")
def test_source_flag_on_valid_returns_lrv(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    mp4, lrv = _pair(tmp_path)
    monkeypatch.setattr(proxy, "_flag_enabled", lambda: True)
    assert analysis_source(mp4) == str(lrv)


def test_source_flag_on_no_proxy_returns_mp4(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    mp4 = tmp_path / "GX010084.MP4"
    mp4.touch()
    monkeypatch.setattr(proxy, "_flag_enabled", lambda: True)
    assert analysis_source(mp4) == str(mp4)


def test_source_flag_on_invalid_returns_mp4(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    mp4, _ = _pair(tmp_path)
    monkeypatch.setattr(proxy, "_flag_enabled", lambda: True)
    monkeypatch.setattr(
        proxy, "validate_proxy",
        lambda *a, **k: ProxyVerdict(False, None, "duration mismatch"),
    )
    assert analysis_source(mp4) == str(mp4)


def test_source_swallows_errors(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Any unexpected error in selection falls back to the MP4 (never raises)."""
    mp4, _ = _pair(tmp_path)
    monkeypatch.setattr(proxy, "_flag_enabled", lambda: True)

    def _boom(*a: object, **k: object) -> ProxyVerdict:
        raise RuntimeError("ffprobe exploded")

    monkeypatch.setattr(proxy, "validate_proxy", _boom)
    assert analysis_source(mp4) == str(mp4)


def test_flag_enabled_defaults_off(monkeypatch: MonkeyPatch) -> None:
    """With nothing set, the flag reads as disabled."""
    monkeypatch.delenv("USE_PROXY_ANALYSIS", raising=False)
    # get_settings caches; clear so it re-reads the (cleared) env.
    from api.config import get_settings

    get_settings.cache_clear()
    assert proxy._flag_enabled() is False
