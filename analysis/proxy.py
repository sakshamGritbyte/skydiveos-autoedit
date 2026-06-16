"""Analysis source selection: prefer a validated GoPro LRV proxy, else the MP4.

The pipeline's *analysis* stages (GPMF parsing, scene segmentation, exit/freefall/
canopy detection, and face/smile/eye/subject scoring) can run on the low-res ``.LRV``
proxy GoPro records alongside every clip — far cheaper to decode than the 4K master,
and (on validated footage) frame- and telemetry-identical to it. Rendering, EDL
application, photo extraction, thumbnails, and every customer-facing deliverable
ALWAYS use the original MP4; this module never touches that path.

:func:`analysis_source` is the single seam: given a master MP4 it returns the matching
LRV when ``USE_PROXY_ANALYSIS`` is enabled AND that LRV passes :func:`validate_proxy`,
otherwise the MP4 unchanged. It never raises — a disabled flag, missing proxy, failed
validation, or *any* unexpected error all fall back to the MP4 transparently. So no job
can fail for a missing/bad proxy, and with the flag off the pipeline behaves exactly as
production does today.

Validation confirms the four required guarantees: a matching LRV exists, its duration
is within :data:`PROXY_DURATION_TOL_S` of the master, its video stream is readable, and
it carries a readable GPMF (``gpmd``) telemetry stream.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Max allowed |MP4 − LRV| container-duration difference (seconds) for a proxy to be
#: trusted. Measured real GoPro pairs match to <1 ms; 0.1 s is a conservative guard.
PROXY_DURATION_TOL_S = 0.1

#: Suffixes a GoPro low-res proxy may carry on disk (camera-native and case variants).
_PROXY_SUFFIXES = (".LRV", ".lrv")

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ProxyVerdict:
    """Outcome of validating a master/proxy pair.

    ``use_proxy`` is True only when every required check passed; ``proxy_path`` is the
    LRV to read in that case (else ``None``); ``reason`` explains the decision (logged
    and persistable for the compatibility matrix / diagnostics).
    """

    use_proxy: bool
    proxy_path: Path | None
    reason: str


def _flag_enabled() -> bool:
    """Whether ``USE_PROXY_ANALYSIS`` is on. Defaults to off; never raises.

    Reads the resolved service settings when available (the canonical source, which
    also loads ``.env``); falls back to the raw env var if the config module can't be
    imported (e.g. analysis used standalone). Either way an error means "disabled".
    """
    try:
        from api.config import get_settings

        return get_settings().use_proxy_analysis
    except Exception:  # noqa: BLE001 - config unavailable -> behave as if disabled
        return os.environ.get("USE_PROXY_ANALYSIS", "").strip().lower() in _TRUTHY


def find_proxy(mp4_path: str | Path) -> Path | None:
    """The LRV proxy matching ``mp4_path`` on disk, or ``None``.

    Looks beside the master for both the same-stem proxy (``GH010001.LRV``) and GoPro's
    native ``GL``-prefixed name (``GX010001.MP4`` -> ``GL010001.LRV``), each in upper-
    and lower-case suffix. Returns the first that exists; never the master itself.
    """
    p = Path(mp4_path)
    d = p.parent
    stem = p.stem
    candidates: list[Path] = [d / f"{stem}{suf}" for suf in _PROXY_SUFFIXES]
    if len(stem) >= 2 and stem[:2].upper() in {"GX", "GH"}:
        gl = "GL" + stem[2:]
        candidates += [d / f"{gl}{suf}" for suf in _PROXY_SUFFIXES]
    for c in candidates:
        if c.exists():
            return c
    return None


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _has_readable_video(path: Path) -> bool:
    """True if the file exposes a video stream with a codec and non-zero dimensions."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    parts = out.split(",")
    return (
        len(parts) >= 3
        and parts[0] not in ("", "N/A")
        and parts[1] not in ("", "0", "N/A")
        and parts[2] not in ("", "0", "N/A")
    )


def _has_readable_gpmf(path: Path) -> bool:
    """True if the file carries a GPMF (``gpmd``-tagged) data stream."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_tag_string",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=False,
    ).stdout
    return "gpmd" in out


def validate_proxy(mp4_path: str | Path, lrv_path: str | Path | None = None) -> ProxyVerdict:
    """Validate a master/proxy pair against the four required checks; never raises.

    Confirms (1) a matching LRV exists, (2) its container duration is within
    :data:`PROXY_DURATION_TOL_S` of the master, (3) its video stream is readable, and
    (4) it carries a readable ``gpmd`` GPMF stream. Any failure yields
    ``use_proxy=False`` with a reason, so the caller falls back to the MP4.
    """
    mp4 = Path(mp4_path)
    lrv = Path(lrv_path) if lrv_path is not None else find_proxy(mp4)
    if lrv is None or not lrv.exists():
        return ProxyVerdict(False, None, "no matching LRV proxy")
    dm = _probe_duration(mp4)
    dl = _probe_duration(lrv)
    if dm <= 0 or dl <= 0:
        return ProxyVerdict(False, None, f"unreadable duration (mp4={dm}, lrv={dl})")
    if abs(dm - dl) > PROXY_DURATION_TOL_S:
        return ProxyVerdict(
            False, None, f"duration mismatch {abs(dm - dl):.3f}s > {PROXY_DURATION_TOL_S}s"
        )
    if not _has_readable_video(lrv):
        return ProxyVerdict(False, None, "LRV video stream unreadable")
    if not _has_readable_gpmf(lrv):
        return ProxyVerdict(False, None, "LRV GPMF (gpmd) stream missing/unreadable")
    return ProxyVerdict(True, lrv, "validated")


def analysis_source(mp4_path: str | Path) -> str:
    """Return the path analysis should read for ``mp4_path``.

    A validated LRV proxy when ``USE_PROXY_ANALYSIS`` is enabled and the proxy passes
    validation; otherwise the MP4 unchanged. This is the single decision point for
    LRV-first analysis and is total: a disabled flag, missing proxy, failed validation,
    or any unexpected error all return the original MP4, so analysis never fails and —
    with the flag off — behaves exactly as production does today.
    """
    mp4 = Path(mp4_path)
    try:
        if not _flag_enabled():
            return str(mp4)
        verdict = validate_proxy(mp4)
        if verdict.use_proxy and verdict.proxy_path is not None:
            logger.info(
                "analysis using LRV proxy for %s: %s", mp4.name, verdict.proxy_path.name
            )
            return str(verdict.proxy_path)
        logger.debug("analysis using MP4 for %s (%s)", mp4.name, verdict.reason)
        return str(mp4)
    except Exception as e:  # noqa: BLE001 - fallback must be total; never fail a job
        logger.warning("proxy selection failed for %s (%r); using MP4", mp4, e)
        return str(mp4)
