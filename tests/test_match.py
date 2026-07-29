"""Pure-function tests for the footage→jump resolver (:mod:`ingest.match`).

The DB-touching :class:`~ingest.match.FootageMatcher.resolve` is exercised live
against the shared DB by hand; here we lock down the two *pure* decision points —
:func:`package_for` (add-on → package) and :func:`select_match` (which jumper a clip
belongs to, and when to refuse) — because that is where a mistake mis-delivers a
customer's video.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ingest.match import (
    AmbiguousMatch,
    Candidate,
    NoBookingMatch,
    package_for,
    select_match,
)


def _cand(
    *,
    load_id: str = "L1",
    role: str = "external",
    day: str | None = "2026-07-21",
    departure: datetime | None = None,
    idx: int = 0,
) -> Candidate:
    return Candidate(
        load_id=load_id,
        load_number=1,
        departure_local=departure,
        business_day=day,
        jumper_index=idx,
        jumper={"booking": "b", "customer": "c"},
        role=role,
    )


class TestPackageFor:
    @pytest.mark.parametrize(
        "media,video,expected",
        [
            ("video-photos", "both", "ultimum"),
            ("video-photos", "outside", "external"),
            ("video-photos", "inside", "selfie"),
            ("video", "both", "ultimum"),
            ("video", "outside", "video_only"),
            ("video", "inside", "video_only"),
            ("photos", None, "photo_only"),
            ("none", "both", None),
            ("", None, None),
            (None, None, None),
            ("video-photos", None, None),  # video+photos but no side named → flag
            ("video-photos", "", None),
        ],
    )
    def test_mapping(self, media, video, expected):
        assert package_for(media, video) == expected

    def test_case_insensitive(self):
        assert package_for("Video-Photos", "BOTH") == "ultimum"


class TestSelectMatch:
    _day = "2026-07-21"
    _cap = datetime(2026, 7, 21, 13, 15)  # 15 min after a 13:00 departure

    def test_none_raises(self):
        with pytest.raises(NoBookingMatch):
            select_match([], self._cap, captured_day=self._day)

    def test_single_returns_it(self):
        c = _cand()
        assert select_match([c], self._cap, captured_day=self._day) is c

    def test_two_same_day_narrows_by_window(self):
        dep = datetime(2026, 7, 21, 13, 0)
        hit = _cand(load_id="L1", departure=dep, idx=0)
        # a load the same day but hours away from the capture instant
        miss = _cand(load_id="L2", departure=datetime(2026, 7, 21, 8, 0), idx=1)
        got = select_match([hit, miss], self._cap, captured_day=self._day)
        assert got.load_id == "L1"

    def test_two_in_window_refuses(self):
        dep = datetime(2026, 7, 21, 13, 0)
        a = _cand(load_id="L1", departure=dep, idx=0)
        b = _cand(load_id="L2", departure=dep, idx=1)
        with pytest.raises(AmbiguousMatch) as ei:
            select_match([a, b], self._cap, captured_day=self._day)
        assert len(ei.value.candidates) == 2

    def test_different_day_dropped_leaving_one(self):
        dep = datetime(2026, 7, 21, 13, 0)
        today = _cand(load_id="L1", day="2026-07-21", departure=dep)
        other = _cand(load_id="L2", day="2026-07-20", departure=dep)
        got = select_match([today, other], self._cap, captured_day=self._day)
        assert got.load_id == "L1"

    def test_capture_outside_window_refuses(self):
        # two same-day loads, neither within the flight window of the capture instant
        far = datetime(2026, 7, 21, 3, 0)
        a = _cand(load_id="L1", departure=far, idx=0)
        b = _cand(load_id="L2", departure=far, idx=1)
        with pytest.raises(AmbiguousMatch):
            select_match([a, b], self._cap, captured_day=self._day)

    def test_busy_day_each_jump_matches_its_own_load(self):
        """A staff member flying 5 loads a day: every clip must find ITS jump.

        ``WINDOW_POST`` (2.5 h) is far wider than the gap between loads, so a clip is
        inside several loads' windows at once. Resolving that by departure order is
        what makes the unattended BLE flow usable — refusing would automate only the
        first jump of the day and strand the other four.
        """
        cands = [
            _cand(load_id=f"L{i}", departure=datetime(2026, 7, 21, hour, 0), idx=i)
            for i, hour in enumerate((10, 11, 12, 13, 14))
        ]
        for i, hour in enumerate((10, 11, 12, 13, 14)):
            captured = datetime(2026, 7, 21, hour, 5)  # 5 min after that load left
            got = select_match(cands, captured, captured_day=self._day)
            assert got.load_id == f"L{i}", f"clip at {hour}:05 matched {got.load_id}"

    def test_clip_belongs_to_the_flight_that_already_departed(self):
        """Never attribute footage to a load that had not taken off yet."""
        earlier = _cand(load_id="L1", departure=datetime(2026, 7, 21, 12, 0), idx=0)
        later = _cand(load_id="L2", departure=datetime(2026, 7, 21, 13, 0), idx=1)
        # 12:20 — after L1 left, still inside L2's pre-window. It is L1's footage.
        got = select_match([earlier, later], datetime(2026, 7, 21, 12, 20),
                           captured_day=self._day)
        assert got.load_id == "L1"

    def test_same_departure_still_refuses(self):
        """Two jumpers on the SAME flight is real ambiguity — never guess."""
        dep = datetime(2026, 7, 21, 13, 0)
        a = _cand(load_id="L1", departure=dep, idx=0)
        b = _cand(load_id="L2", departure=dep, idx=1)
        other = _cand(load_id="L3", departure=datetime(2026, 7, 21, 11, 0), idx=2)
        with pytest.raises(AmbiguousMatch) as ei:
            select_match([a, b, other], self._cap, captured_day=self._day)
        assert len(ei.value.candidates) == 2  # only the tied pair, not the 11:00 load

    def test_window_boundaries(self):
        from ingest.match import WINDOW_POST, WINDOW_PRE, _in_window

        dep = datetime(2026, 7, 21, 13, 0)
        assert _in_window(dep + WINDOW_POST, dep)
        assert _in_window(dep - WINDOW_PRE, dep)
        assert not _in_window(dep + WINDOW_POST + timedelta(seconds=1), dep)
        assert not _in_window(dep, None)
