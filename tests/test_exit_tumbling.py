"""The tumbling-tandem exit (bug reopen 2026-10-23, job ba5fade8dc1b419b899fa654a8f6953d).

The freefall video opened on 19 s of aircraft because ``detect_exit_offset`` found no
exit: a tandem rolling out of the door keeps a ~1 g payload mean, and its near-0 g
samples are vetoed by the door-shake guard. The camera (no GPS) recorded only ACCL/GYRO/
GRAV. The fixture below is that file's real per-second accelerometer record (mean / min
/ max g, from ``scripts/diagnose_freefall_sync.py --telemetry``), expanded to samples.
Its quarter-second detail at the exit is reproduced too: 19.00–19.25 s averaged 0.62 g
with a 0.07 g sample — the drop off the aircraft — while every quarter-second in the
cabin, door phase included, stayed above 0.9 g.
"""

from __future__ import annotations

from typing import Any

import pytest

from api import selfie

# (second, mean g, min g, max g) — GX011465.MP4, verbatim from the box.
REAL = [
    (0, 1.18, 0.90, 1.56), (1, 1.14, 0.92, 1.32), (2, 1.16, 0.89, 1.54),
    (3, 1.14, 0.80, 1.46), (4, 1.06, 0.89, 1.19), (5, 1.02, 0.94, 1.41),
    (6, 1.01, 0.91, 1.07), (7, 1.01, 0.79, 1.28), (8, 1.08, 0.79, 1.66),
    (9, 1.01, 0.20, 1.46), (10, 1.06, 0.42, 2.06), (11, 1.04, 0.64, 1.32),
    (12, 1.02, 0.44, 2.13), (13, 1.22, 0.41, 4.13), (14, 1.23, 0.43, 2.24),
    (15, 1.01, 0.37, 1.85), (16, 1.07, 0.49, 1.62), (17, 1.16, 0.48, 2.12),
    (18, 1.20, 0.45, 2.18), (19, 0.91, 0.07, 1.75), (20, 0.99, 0.45, 1.70),
    (21, 1.29, 0.09, 8.06), (22, 0.96, 0.22, 2.05), (23, 1.18, 0.25, 2.41),
    (24, 1.97, 0.49, 5.51), (25, 1.93, 0.19, 10.75), (26, 1.11, 0.17, 2.43),
    (27, 1.04, 0.15, 1.98), (28, 1.69, 0.13, 6.08), (29, 1.17, 0.21, 1.71),
    (30, 1.48, 0.52, 4.21), (31, 1.87, 0.40, 3.65), (32, 1.14, 0.18, 2.10),
    (33, 1.33, 0.18, 3.03), (34, 1.45, 0.30, 2.97), (35, 1.17, 0.34, 2.01),
    (36, 1.20, 0.25, 2.12), (37, 1.10, 0.19, 2.10), (38, 1.28, 0.45, 2.41),
    (39, 1.27, 0.50, 2.22), (40, 1.06, 0.44, 1.62), (41, 1.07, 0.37, 1.67),
    (42, 1.23, 0.37, 2.13), (43, 1.14, 0.44, 2.02), (44, 1.23, 0.37, 2.08),
    (45, 1.26, 0.37, 2.99), (46, 1.12, 0.22, 2.52), (47, 1.11, 0.36, 2.32),
    (48, 1.25, 0.25, 2.24), (49, 1.08, 0.35, 2.21), (50, 1.15, 0.25, 2.22),
    (51, 1.36, 0.23, 8.48), (52, 1.19, 0.09, 2.27), (53, 1.04, 0.45, 1.58),
    (54, 1.13, 0.44, 1.67), (55, 1.05, 0.52, 1.58), (56, 1.09, 0.24, 1.74),
    (57, 1.14, 0.35, 1.74), (58, 1.07, 0.50, 1.65), (59, 1.12, 0.31, 1.83),
    (60, 1.19, 0.38, 1.83), (61, 1.11, 0.40, 1.71), (62, 1.15, 0.30, 1.77),
    (63, 1.10, 0.57, 1.60), (64, 1.07, 0.17, 1.73), (65, 1.14, 0.21, 1.72),
    (66, 1.09, 0.33, 2.25), (67, 1.31, 0.23, 2.36), (68, 2.74, 0.44, 5.81),
    (69, 2.05, 0.60, 4.12), (70, 1.83, 0.45, 2.83), (71, 1.82, 0.47, 3.50),
    (72, 1.38, 0.60, 2.62), (73, 1.50, 0.53, 2.22), (74, 1.44, 0.75, 1.99),
    (75, 1.62, 1.10, 2.48), (76, 1.60, 0.97, 2.47), (77, 1.52, 1.15, 2.04),
    (78, 1.38, 0.96, 1.90), (79, 1.11, 0.91, 1.44), (80, 1.00, 0.80, 1.24),
    (81, 0.97, 0.75, 1.21), (82, 0.90, 0.74, 1.10), (83, 1.06, 0.93, 1.19),
    (84, 1.12, 0.90, 1.31), (85, 1.33, 1.12, 1.69), (86, 1.42, 1.14, 1.63),
    (87, 1.39, 0.73, 2.95), (88, 1.35, 1.02, 1.65),
]
_N = 20  # samples per payload (5 per quarter-second)
_G = 9.80665


def _second(mean: float, mn: float, mx: float, weightless_head: bool) -> list[float]:
    """20 magnitudes with exactly this mean/min/max. The min sits mid-payload among
    ~mean neighbours (so no quarter-second reads weightless) unless ``weightless_head``,
    which reproduces the measured exit quarter: the first 5 samples average ~0.6 g."""
    if weightless_head:
        head = [mn, 0.55, 0.65, 0.75, 0.95]                      # mean 0.594, min = mn
        fill = (_N * mean - sum(head) - mx) / (_N - len(head) - 1)
        vals = [*head, *[fill] * (_N - len(head) - 1), mx]
    else:
        fill = (_N * mean - mn - mx) / (_N - 2)
        vals = [fill] * _N
        vals[10], vals[15] = mn, mx
    assert min(vals) == pytest.approx(mn) and max(vals) == pytest.approx(mx)
    return vals


def _gpmf(rows: list[tuple[int, float, float, float]], weightless: set[int]) -> Any:
    from metadata.gpmf import GpmfData, StreamSamples

    payloads = [
        [(g * _G, 0.0, 0.0) for g in _second(m, lo, hi, sec in weightless)]
        for sec, m, lo, hi in rows
    ]
    times = [float(i) for i in range(len(rows))]
    accl = StreamSamples(fourcc="ACCL", payloads=payloads, times=times)
    return GpmfData(streams={"ACCL": accl}, duration_s=float(len(rows)))


def _detect(monkeypatch: pytest.MonkeyPatch, data: Any) -> float:
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: data)
    return selfie.detect_exit_offset("/x/GX011465.MP4")


def test_the_payload_rule_alone_misses_this_exit() -> None:
    # Pin WHY the fallback exists: no payload of this file dips its mean below the
    # plane level, and the near-0 g mins come with a ~1 g mean (the door-shake veto).
    assert all(m >= 0.85 for _, m, _, _ in REAL[:68])
    assert [s for s, m, lo, _ in REAL if lo < 0.25 and m < 0.85] == []


def test_tumbling_exit_is_found_on_the_real_record(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _detect(monkeypatch, _gpmf(REAL, weightless={19})) == pytest.approx(19.0)


def test_and_deployment_is_still_the_canopy_opening(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the exit now at 19 s, the deploy prior [exit+40, exit+80] still lands on the
    # 68 s opening shock (2.74 g), not the drogue at 24-25 s.
    data = _gpmf(REAL, weightless={19})
    monkeypatch.setattr("metadata.gpmf.parse_gpmf", lambda p: data)
    assert selfie.detect_deploy_offset(["/x/GX011465.MP4"], exit_offset=19.0) == pytest.approx(
        68.0, abs=1.0
    )


def test_a_weightless_moment_at_the_door_is_not_the_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stumble at the door (second 9, which really did read a 0.20 g sample) that also
    # went weightless for a quarter-second: the quiet second 11 after it (0.64-1.32 g)
    # proves the pair was still on board, so the real exit at 19 s still wins.
    assert _detect(monkeypatch, _gpmf(REAL, weightless={9, 19})) == pytest.approx(19.0)


def test_a_clip_that_starts_in_freefall_gets_no_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same jump from second 26 on (mid-freefall, e.g. a GoPro chapter): even with a
    # weightless quarter in it, there was no calm on-board second before, so 0.0.
    tail = [(i, m, lo, hi) for i, (_, m, lo, hi) in enumerate(REAL[26:])]
    assert _detect(monkeypatch, _gpmf(tail, weightless={2})) == 0.0


def test_a_weightless_moment_without_a_freefall_after_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cabin only (seconds 0-18): a weightless quarter at 9 is never followed by 30 s of
    # unbroken turbulence, so it is not an exit.
    assert _detect(monkeypatch, _gpmf(REAL[:19], weightless={9})) == 0.0


def test_the_payload_rule_still_wins_when_it_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stable exit the original rule catches is never re-decided by the fallback.
    from metadata.gpmf import GpmfData, StreamSamples

    payloads = [[(g * _G, 0.0, 0.0)] * 5 for g in [1.0] * 10 + [0.4, 0.4, 0.5, 0.6]]
    accl = StreamSamples(fourcc="ACCL", payloads=payloads, times=[float(t) for t in range(14)])
    assert _detect(monkeypatch, GpmfData(streams={"ACCL": accl}, duration_s=14.0)) == 10.0
