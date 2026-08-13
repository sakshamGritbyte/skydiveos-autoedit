"""Ownership regression suite: which customer does a clip belong to?

Every case here is a **confirmed finding** from the 2026-08-11 audit
(``AUDIT_MEDIA_MATCH_ISOLATION.md``), pinned as executable fact before the matcher is
redesigned. Each one states, for one clip:

    clip (capture instant)  →  load  →  jumper slot  →  customer  →  role

and asserts the exact tuple ``ingest.match.select_match`` produces **today**, plus the
answer the Phase 3 design says it *should* produce. The two are recorded side by side in
:data:`CASES`, so:

* the current behaviour cannot drift unnoticed while Phase 3 is designed and reviewed;
* :func:`test_impact_of_the_phase_3_rule` prints (and pins) how many cases would change —
  the "expected increase in flagged jobs" number the operator signs off on;
* when the rule lands, the diff on this file IS the impact statement: every case whose
  ``current`` changes must be one whose ``desired`` it now matches.

``REFUSE`` means "no confident owner — flag for review". The audit's governing principle:
*"Unmatched — needs review" is always preferable to "probably this customer"*, because a
video delivered to the wrong customer cannot be taken back.

The decision logic is pure, so none of this needs Mongo, S3 or a worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from ingest.match import (
    WINDOW_POST,
    WINDOW_PRE,
    AmbiguousMatch,
    Candidate,
    FootageMatchError,
    NoBookingMatch,
    _in_window,
    select_match,
)

#: Sentinel for "the matcher must refuse and flag rather than name an owner".
REFUSE = "REFUSE"


def candidate(
    load: str,
    departure: datetime | None,
    business_day: str | None,
    jumper_index: int,
    customer: str,
    role: str = "instructor",
    *,
    flew: tuple[datetime, datetime] | None = None,
) -> Candidate:
    """One (load, jumper, role) slot the camera's staff member holds.

    ``customer`` rides inside the jumper document, which is what makes an assertion here
    a statement about a *person* rather than an array index.

    ``flew=(takeoff, landing)`` is the load's **recorded** flight. Supplying it is the
    difference between the scheduled ±2.5 h window (which overlaps 5–6 loads) and the
    ~25 min the plane was really up — see the two ``…_with_recorded_flight_times`` cases:
    the same clip resolves differently, and that is a *data* dependency on ops filling in
    `actualTakeoffTime`, not something code can decide.
    """
    return Candidate(
        load_id=load,
        load_number=None,
        departure_local=departure,
        business_day=business_day,
        jumper_index=jumper_index,
        jumper={
            "customer": customer,
            "booking": f"bk-{customer.lower()}",
            "mediaPackage": "video",
            "videoType": "inside",
        },
        role=role,
        actual_takeoff_local=flew[0] if flew else None,
        landing_local=flew[1] if flew else None,
    )


def resolve(candidates: list[Candidate], captured: datetime) -> str:
    """The customer ``select_match`` picks, or :data:`REFUSE` when it declines.

    Collapses the whole decision to the one fact that matters operationally: whose
    gallery this clip would end up in.
    """
    try:
        return select_match(
            candidates, captured, captured_day=captured.date().isoformat()
        ).jumper["customer"]
    except FootageMatchError:
        return REFUSE


@dataclass(frozen=True)
class Case:
    """One clip-ownership scenario: what it did before Phase 3, and what it does now."""

    ref: str  #: audit section (``AUDIT_MEDIA_MATCH_ISOLATION.md`` §3-x)
    name: str
    candidates: list[Candidate]
    captured: datetime
    before: str  #: what the matcher returned BEFORE Phase 3 (history, not asserted)
    after: str  #: what it returns NOW (asserted)
    desired: str  #: the correct owner, or REFUSE when no owner can be established
    why: str

    @property
    def was_wrong(self) -> bool:
        """Pre-Phase-3, this named a customer the clip did not belong to."""
        return self.before not in (self.desired, REFUSE)

    @property
    def is_wrong_now(self) -> bool:
        """Still names the wrong customer — the honest count of what is NOT fixed."""
        return self.after not in (self.desired, REFUSE)

    @property
    def bucket(self) -> str:
        """Which line of the before/after report this case belongs on."""
        if self.was_wrong and self.after == REFUSE:
            return "wrong->refused"
        if self.was_wrong and self.after == self.desired:
            return "wrong->correct"
        if self.was_wrong:
            return "still-wrong"
        if self.after != self.before:
            return "regressed"
        return "correct->correct"


D = datetime

# --------------------------------------------------------------------------- #
# The confirmed findings, one Case each.
# --------------------------------------------------------------------------- #

CASES: list[Case] = [
    # ── 1. Multiple customers + a delayed interview (the audit's §8 worst case) ──
    Case(
        ref="§3-C",
        name="busy_instructor_delayed_intro",
        candidates=[
            candidate("load-0930", D(2026, 8, 11, 9, 30), "2026-08-11", 0, "Xavier"),
            candidate("load-1400", D(2026, 8, 11, 14, 0), "2026-08-11", 3, "Priya"),
        ],
        captured=D(2026, 8, 11, 10, 15),
        before="Xavier",
        after="Xavier",
        desired=REFUSE,
        why=(
            "STILL WRONG, and honestly so: neither load records its actual flight, so the "
            "scheduled ±2.5 h window applies and 10:15 is legitimately inside the 09:30 "
            "load's. Nothing in the timestamp distinguishes Priya's interview from a real "
            "Xavier clip at 10:15, so no rule over departureTime alone can fix this — see "
            "the recorded-flight variant below, and Phase 4's per-jump marker."
        ),
    ),
    Case(
        ref="§3-C",
        name="busy_instructor_delayed_intro_with_recorded_flight_times",
        candidates=[
            candidate(
                "load-0930", D(2026, 8, 11, 9, 30), "2026-08-11", 0, "Xavier",
                flew=(D(2026, 8, 11, 9, 35), D(2026, 8, 11, 9, 58)),
            ),
            candidate(
                "load-1400", D(2026, 8, 11, 14, 0), "2026-08-11", 3, "Priya",
                flew=(D(2026, 8, 11, 14, 5), D(2026, 8, 11, 14, 30)),
            ),
        ],
        captured=D(2026, 8, 11, 10, 15),
        before="Xavier",
        after=REFUSE,
        desired=REFUSE,
        why=(
            "The SAME scenario once ops recorded the flights: Xavier's load was airborne "
            "09:35–09:58, so its window ends 10:08 and the 10:15 clip is outside it — as "
            "it is outside Priya's. Two same-day candidates and none in-window → refuse. "
            "This is the §8 worst case closed, and it is closed by DATA (actualTakeoffTime "
            "being filled in), not by a narrower guess."
        ),
    ),
    # ── 2. Two customers with close departure times ──
    Case(
        ref="§3-D",
        name="departures_five_minutes_apart",
        candidates=[
            candidate("load-a", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "CustomerA"),
            candidate("load-b", D(2026, 8, 11, 10, 5), "2026-08-11", 0, "CustomerB"),
        ],
        captured=D(2026, 8, 11, 10, 10),
        before="CustomerB",
        after=REFUSE,
        desired=REFUSE,
        why=(
            "Both windows contain 10:10, so the causal tie-break takes the LATEST "
            "departure at or before the clip — a guess. A's clip goes to B."
        ),
    ),
    Case(
        ref="§3-D",
        name="same_departure_instant_is_already_refused",
        candidates=[
            candidate("load-a", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "CustomerA"),
            candidate("load-a", D(2026, 8, 11, 10, 0), "2026-08-11", 1, "CustomerB"),
        ],
        captured=D(2026, 8, 11, 10, 10),
        before=REFUSE,
        after=REFUSE,
        desired=REFUSE,
        why="A genuine tie raises AmbiguousMatch today — the behaviour to preserve.",
    ),
    # ── 3. Yesterday's clip after yesterday's load is pruned ──
    Case(
        ref="§3-F",
        name="yesterdays_clip_yesterdays_load_pruned",
        candidates=[
            candidate("load-today", D(2026, 8, 11, 10, 0), "2026-08-11", 2, "TodaysCustomer"),
        ],
        captured=D(2026, 8, 10, 10, 15),
        before="TodaysCustomer",
        after=REFUSE,
        desired=REFUSE,
        why=(
            "The capture day matches no candidate, so `same_day or pool` re-admits every "
            "other day and the lone-candidate path accepts it with no window check: "
            "yesterday's leftover footage becomes TODAY's customer's job."
        ),
    ),
    Case(
        ref="§3-F",
        name="yesterdays_clip_when_both_days_are_manifested",
        candidates=[
            candidate("load-yday", D(2026, 8, 10, 10, 0), "2026-08-10", 2, "YesterdayCust"),
            candidate("load-today", D(2026, 8, 11, 10, 0), "2026-08-11", 2, "TodaysCustomer"),
        ],
        captured=D(2026, 8, 10, 10, 15),
        before="YesterdayCust",
        after="YesterdayCust",
        desired="YesterdayCust",
        why="Day narrowing works when the day's load still exists — keep it working.",
    ),
    # ── 4. Camera clock offset ──
    Case(
        ref="§3-N",
        name="camera_clock_one_hour_fast",
        candidates=[
            candidate("load-1000", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "OurCustomer"),
            candidate("load-1100", D(2026, 8, 11, 11, 0), "2026-08-11", 0, "CustomerX"),
        ],
        captured=D(2026, 8, 11, 11, 15),  # really 10:15; the clock is +1 h
        before="CustomerX",
        after="CustomerX",
        desired=REFUSE,
        why=(
            "STILL WRONG, and not fixable by any window rule: the shifted timestamp lands "
            "squarely inside a real load's real window, so the answer is internally "
            "consistent and simply about the wrong flight. Narrowing the window (recorded "
            "flight times, below) does not help — it moves the clip into the 11:00 load's "
            "ACTUAL flight instead. This needs the GPMF GPS cross-check (audit ⚠️-8): "
            "satellite time is absolute and already parsed by metadata/gpmf.py."
        ),
    ),
    Case(
        ref="§3-N",
        name="camera_clock_one_hour_fast_with_recorded_flight_times",
        candidates=[
            candidate(
                "load-1000", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "OurCustomer",
                flew=(D(2026, 8, 11, 10, 5), D(2026, 8, 11, 10, 28)),
            ),
            candidate(
                "load-1100", D(2026, 8, 11, 11, 0), "2026-08-11", 0, "CustomerX",
                flew=(D(2026, 8, 11, 11, 5), D(2026, 8, 11, 11, 28)),
            ),
        ],
        captured=D(2026, 8, 11, 11, 15),  # really 10:15
        before="CustomerX",
        after="CustomerX",
        desired=REFUSE,
        why=(
            "Proof that tighter windows are NOT the answer to clock skew: 11:15 is inside "
            "the 11:00 load's recorded flight, so the wrong customer is chosen with full "
            "'window' evidence. Clock discipline + the GPS cross-check are the only fixes."
        ),
    ),
    # ── 5. Midnight / day boundary ──
    Case(
        ref="§3-O",
        name="post_midnight_clip_with_a_next_day_load",
        candidates=[
            candidate("load-night", D(2026, 8, 11, 23, 50), "2026-08-11", 0, "NightJumper"),
            candidate("load-morning", D(2026, 8, 12, 11, 0), "2026-08-12", 0, "MorningCust"),
        ],
        captured=D(2026, 8, 12, 0, 5),
        before="MorningCust",
        after="NightJumper",
        desired="NightJumper",
        why=(
            "Day narrowing runs FIRST and discards the night load, leaving one candidate "
            "that the window check is then skipped for — so a 00:05 clip is handed to a "
            "customer whose load departs 11 hours later. `select_load` already handles "
            "this correctly for spec flights by querying on the departure window."
        ),
    ),
    Case(
        ref="§3-O",
        name="post_midnight_clip_with_no_next_day_load",
        candidates=[
            candidate("load-night", D(2026, 8, 11, 23, 50), "2026-08-11", 0, "NightJumper"),
        ],
        captured=D(2026, 8, 12, 0, 5),
        before="NightJumper",
        after="NightJumper",
        desired="NightJumper",
        why="The right answer today, but reached by the lone-candidate shortcut, not proof.",
    ),
    # ── 6. Stale planned load (never cancelled after the jump moved) ──
    Case(
        ref="§3-B",
        name="stale_planned_load_takes_the_interview",
        candidates=[
            candidate("load-1000", D(2026, 8, 11, 10, 0), "2026-08-11", 1, "Ours@stale"),
            candidate("load-1400", D(2026, 8, 11, 14, 0), "2026-08-11", 3, "Ours@real"),
        ],
        captured=D(2026, 8, 11, 10, 15),
        before="Ours@stale",
        after="Ours@stale",
        desired="Ours@stale",
        why=(
            "SAME customer on both slots, so this is not a cross-customer leak — but the "
            "two slots become two jobs, two renders and two emails, one of them "
            "interview-only. Phase 1's jump-evidence guard stops that one being "
            "DELIVERED; only a per-jump identity (⚠️-4) stops the second job existing."
        ),
    ),
    Case(
        ref="§3-B",
        name="stale_planned_load_leaves_the_jump_correct",
        candidates=[
            candidate("load-1000", D(2026, 8, 11, 10, 0), "2026-08-11", 1, "Ours@stale"),
            candidate("load-1400", D(2026, 8, 11, 14, 0), "2026-08-11", 3, "Ours@real"),
        ],
        captured=D(2026, 8, 11, 14, 5),
        before="Ours@real",
        after="Ours@real",
        desired="Ours@real",
        why="The jump clips themselves resolve correctly — the split is only the orphans.",
    ),
    # ── 7. Customer moved between loads (old slot properly cancelled) ──
    Case(
        ref="§3-A",
        name="rescheduled_customer_old_load_cancelled",
        candidates=[
            candidate("load-1400", D(2026, 8, 11, 14, 0), "2026-08-11", 3, "Priya"),
        ],
        captured=D(2026, 8, 11, 10, 15),
        before="Priya",
        after="Priya",
        desired="Priya",
        why=(
            "The scenario that MUST keep working: the cancelled 10:00 load leaves one "
            "candidate, and her 4-hours-early interview still joins her jump. Note this "
            "is the same lone-candidate acceptance that breaks §3-F — which is why the "
            "Phase 3 rule keeps it only when the capture DAY matches."
        ),
    ),
    Case(
        ref="§3-A",
        name="rescheduled_customer_jump_clip",
        candidates=[
            candidate("load-1400", D(2026, 8, 11, 14, 0), "2026-08-11", 3, "Priya"),
        ],
        captured=D(2026, 8, 11, 14, 12),
        before="Priya",
        after="Priya",
        desired="Priya",
        why="In-window, one candidate, right day — correct under every proposed rule.",
    ),
    # ── 8. Multiple loads for the same staff member (moved 3×, flew on the 4th) ──
    Case(
        ref="§3-H",
        name="four_slots_jump_resolves_to_the_flown_one",
        candidates=[
            candidate("load-a", D(2026, 8, 11, 10, 0), "2026-08-11", 1, "Ours@A"),
            candidate("load-b", D(2026, 8, 11, 12, 0), "2026-08-11", 1, "Ours@B"),
            candidate("load-c", D(2026, 8, 11, 14, 0), "2026-08-11", 1, "Ours@C"),
            candidate("load-d", D(2026, 8, 11, 16, 0), "2026-08-11", 1, "Ours@D"),
        ],
        captured=D(2026, 8, 11, 16, 5),
        before="Ours@D",
        after="Ours@D",
        desired="Ours@D",
        why="The causal rule is right here: the 16:00 load is the one that had departed.",
    ),
    Case(
        ref="§3-H",
        name="four_slots_early_clip_lands_on_a_stale_one",
        candidates=[
            candidate("load-a", D(2026, 8, 11, 10, 0), "2026-08-11", 1, "Ours@A"),
            candidate("load-b", D(2026, 8, 11, 12, 0), "2026-08-11", 1, "Ours@B"),
            candidate("load-c", D(2026, 8, 11, 14, 0), "2026-08-11", 1, "Ours@C"),
            candidate("load-d", D(2026, 8, 11, 16, 0), "2026-08-11", 1, "Ours@D"),
        ],
        captured=D(2026, 8, 11, 10, 15),
        before="Ours@A",
        after="Ours@A",
        desired="Ours@A",
        why="Same customer, so no leak — but up to four jobs for one jump (see §3-B).",
    ),
    # ── A cameraman working two loads back to back: the role must follow the slot ──
    Case(
        ref="§3-E",
        name="cameraman_role_follows_the_matched_slot",
        candidates=[
            candidate("load-1000", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "TandemA"),
            candidate(
                "load-1100", D(2026, 8, 11, 11, 0), "2026-08-11", 2, "TandemB",
                role="external",
            ),
        ],
        captured=D(2026, 8, 11, 11, 10),
        before="TandemB",
        after="TandemB",
        desired="TandemB",
        why=(
            "One staff member, instructor on one load and cameraman on the next, same "
            "GoPro: the role is a property of the matched slot, never of the camera."
        ),
    ),
    # ── Zero candidates: already safe, and must stay that way ──
    Case(
        ref="§3-G",
        name="no_candidate_at_all_is_refused",
        candidates=[],
        captured=D(2026, 8, 11, 10, 15),
        before=REFUSE,
        after=REFUSE,
        desired=REFUSE,
        why=(
            "The instructor-swap case ends here when the filming staff has no other "
            "customer that day: NoBookingMatch, which the bridge flags. Safe, and the "
            "footage is preserved on the card and in S3 for a human to attach."
        ),
    ),
]


def _ids() -> list[str]:
    return [f"{c.ref}:{c.name}" for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=_ids())
def test_ownership_behaviour_is_pinned(case: Case) -> None:
    """What the matcher answers NOW, for every audited scenario.

    A failure here means behaviour drifted: either deliberately (update ``after``, and
    check the case's bucket in :func:`test_before_after_report`) or by accident.
    """
    assert resolve(case.candidates, case.captured) == case.after, case.why


_CORRECT_BEFORE = [c for c in CASES if not c.was_wrong]


@pytest.mark.parametrize(
    "case", _CORRECT_BEFORE, ids=[f"{c.ref}:{c.name}" for c in _CORRECT_BEFORE]
)
def test_scenarios_that_were_already_correct_stay_correct(case: Case) -> None:
    """The regression fence: Phase 3 must not have disturbed anything that worked.

    Separate from the pinned baseline on purpose — this is the list that must never
    change, whatever else moves. Includes §3-A (a rescheduled customer's early interview),
    which survives only because of the ``out_of_window_same_day`` compatibility path.
    """
    assert resolve(case.candidates, case.captured) == case.before, case.why


def test_the_full_match_result_names_the_load_slot_and_role_not_just_a_customer() -> None:
    """clip → load → jumper_index → customer → role, asserted as one tuple.

    A customer name alone would not catch an index drift (`jumper_index` is a manifest
    position, not an identity — audit §0.2), so the whole ownership tuple is pinned.
    """
    candidates = [
        candidate("load-0930", D(2026, 8, 11, 9, 30), "2026-08-11", 0, "Xavier"),
        candidate(
            "load-1400", D(2026, 8, 11, 14, 0), "2026-08-11", 3, "Priya", role="external"
        ),
    ]
    match = select_match(candidates, D(2026, 8, 11, 14, 12), captured_day="2026-08-11")
    assert (match.load_id, match.jumper_index, match.jumper["customer"], match.role) == (
        "load-1400", 3, "Priya", "external",
    )


def test_cross_customer_leaks_that_remain_are_exactly_the_known_two() -> None:
    """Which scenarios STILL name the wrong customer — the go-live number.

    Each entry is a live path by which customer A's footage can reach customer B's
    gallery. The set must only ever shrink, and every member must have a named reason it
    is not closable inside the matcher:

    * §3-C **without recorded flight times** — a data dependency. The same scenario with
      ``actualTakeoffTime`` filled in refuses (see the recorded-flight case), so the fix is
      ops recording flights; the durable fix is Phase 4's per-jump marker.
    * §3-N clock skew — needs the GPMF GPS cross-check (audit ⚠️-8). Tighter windows make
      it no better, which the recorded-flight variant proves.
    """
    assert {c.name for c in CASES if c.is_wrong_now} == {
        "busy_instructor_delayed_intro",
        "camera_clock_one_hour_fast",
        "camera_clock_one_hour_fast_with_recorded_flight_times",
    }


def test_before_after_report(capsys: pytest.CaptureFixture[str]) -> None:
    """The Phase 3 outcome, in the four buckets that matter. Run with ``-s`` to read it.

    Pinned, so a later change to the matcher has to restate its effect here rather than
    quietly moving a case between buckets.
    """
    buckets: dict[str, list[Case]] = {}
    for case in CASES:
        buckets.setdefault(case.bucket, []).append(case)

    print(f"\nPhase 3 — ownership outcome over {len(CASES)} audited scenarios:")
    for name in ("wrong->refused", "wrong->correct", "correct->correct", "still-wrong",
                 "regressed"):
        rows = buckets.get(name, [])
        print(f"\n  {name}  ({len(rows)})")
        for c in rows:
            print(f"     {c.ref:7} {c.name:52} {c.before:16} → {c.after}")

    assert {c.name for c in buckets.get("wrong->refused", [])} == {
        "departures_five_minutes_apart",                      # §3-D
        "yesterdays_clip_yesterdays_load_pruned",             # §3-F
        "busy_instructor_delayed_intro_with_recorded_flight_times",  # §3-C, with data
    }
    assert [c.name for c in buckets.get("wrong->correct", [])] == [
        "post_midnight_clip_with_a_next_day_load"             # §3-O
    ]
    # The acceptance criterion: nothing that worked before may have broken.
    assert buckets.get("regressed", []) == []
    assert len(buckets.get("correct->correct", [])) == 11
    # And the honest remainder, each with a named reason it is not a matcher problem.
    assert {c.name for c in buckets.get("still-wrong", [])} == {
        "busy_instructor_delayed_intro",                        # needs recorded flights
        "camera_clock_one_hour_fast",                           # needs the GPS cross-check
        "camera_clock_one_hour_fast_with_recorded_flight_times",
    }


# --------------------------------------------------------------------------- #
# The window itself — the primitive every rule above rests on.
# --------------------------------------------------------------------------- #


def test_window_is_asymmetric_and_wide_enough_to_swallow_a_gap_between_loads() -> None:
    """Why a between-loads clip is claimed at all: 2.5 h of post-departure window.

    Pins the two constants a Phase 3 review has to reason about — the audit's point is
    not that the window is wrong, but that it is never *required* to hold.
    """
    departure = D(2026, 8, 11, 9, 30)
    assert WINDOW_PRE.total_seconds() == 30 * 60
    assert WINDOW_POST.total_seconds() == 150 * 60
    assert _in_window(D(2026, 8, 11, 9, 5), departure) is True      # early boarding
    assert _in_window(D(2026, 8, 11, 10, 15), departure) is True    # 45 min after: still in
    assert _in_window(D(2026, 8, 11, 12, 1), departure) is False    # past the tail
    assert _in_window(D(2026, 8, 11, 8, 59), departure) is False    # before the pre-roll
    assert _in_window(D(2026, 8, 11, 10, 15), None) is False        # no departure recorded


def test_a_lone_candidate_is_no_longer_proof_of_ownership() -> None:
    """The root cause, closed (audit §2 R1 — "unopposed is not established").

    Before Phase 3, ``select_match`` returned a single candidate before any day or window
    logic ran, so a clip from any date and a load with no departure time at all were both
    accepted. Every 🔴 in the audit descended from that line plus ``same_day or pool``.
    """
    only = [candidate("load-x", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "Someone")]
    # A clip from a different month is refused: wrong day (T1).
    assert resolve(only, D(2026, 3, 2, 4, 30)) == REFUSE
    # A load with no timing evidence at all can never establish ownership (T2).
    assert resolve([candidate("load-y", None, None, 0, "Nobody")], D(2026, 8, 11, 10, 0)) == REFUSE
    # A slot with no booking/customer reference cannot be joined downstream (T3).
    orphan = candidate("load-z", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "Ghost")
    orphan.jumper.pop("booking")
    orphan.jumper.pop("customer")
    with pytest.raises(NoBookingMatch, match="no booking"):
        select_match([orphan], D(2026, 8, 11, 10, 5), captured_day="2026-08-11")
    # But the same-day lone candidate IS still accepted out-of-window — the temporary
    # compatibility path that keeps a rescheduled customer's early interview attached.
    assert resolve(only, D(2026, 8, 11, 6, 0)) == "Someone"


def test_refusals_carry_the_candidates_a_human_needs() -> None:
    """A flag is only actionable if it says what it could not choose between."""
    tie = [
        candidate("load-a", D(2026, 8, 11, 10, 0), "2026-08-11", 0, "CustomerA"),
        candidate("load-a", D(2026, 8, 11, 10, 0), "2026-08-11", 1, "CustomerB"),
    ]
    with pytest.raises(AmbiguousMatch) as caught:
        select_match(tie, D(2026, 8, 11, 10, 10), captured_day="2026-08-11")
    assert {c.jumper["customer"] for c in caught.value.candidates} == {
        "CustomerA", "CustomerB",
    }
    with pytest.raises(NoBookingMatch):
        select_match([], D(2026, 8, 11, 10, 10), captured_day="2026-08-11")
