"""Seed (or reset) a job into ``ready_for_review`` for review-ui manual QA.

The full pipeline needs the Claude API (Compose) and FFmpeg/GPU (Render), so for
UI testing this drops the two artifacts the review screen reads — a real
``edl.json`` and a stub ``final.mp4`` — and marks the job ``ready_for_review``.
Run it once to create a QA job, or again with ``--job-id`` to reset that same job
after destructive tests (delete / tweak).

    python scripts/seed_review_job.py                 # create a fresh QA job
    python scripts/seed_review_job.py --job-id <id>   # reset an existing one

Prints the job id; open the UI at ``http://localhost:5173/?job=<id>``.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

# Allow running as a plain script (`python scripts/seed_review_job.py`), not just
# as a module: put the repo root on sys.path so the pipeline packages import.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.jobs import Job, JobStatus, JobStore  # noqa: E402
from edl.schema import Clip, EditDecisionList, Transition  # noqa: E402

#: A representative house cut: slow-mo exit, real-time beats, a slowed highlight,
#: then a quick canopy tag — enough variety to exercise every timeline gesture.
QA_EDL = EditDecisionList(
    music="sunrise",
    notes="QA fixture house cut",
    clips=[
        Clip(src_start=132.0, src_end=138.0, speed_multiplier=0.4, transition_in=Transition.fade),
        Clip(src_start=141.5, src_end=149.0),
        Clip(src_start=150.0, src_end=156.0),
        Clip(src_start=158.0, src_end=162.5, speed_multiplier=0.4, transition_out=Transition.flash),
        Clip(src_start=305.0, src_end=309.0, transition_in=Transition.crossfade),
    ],
)

# Minimal valid-ish MP4 header bytes; the player shows controls but no real frames.
_STUB_MP4 = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 64


def seed(job_id: str | None) -> str:
    store = JobStore()  # default ./jobs root, same as `uvicorn api.app:app`
    if job_id is None:
        job_id = uuid.uuid4().hex
    if not store.exists(job_id):
        store.create(Job(job_id=job_id, customer_name="Jane Doe", jump_date="2026-06-02", music="sunrise"))

    store.save_edl(job_id, QA_EDL)
    final = store.final_path(job_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(_STUB_MP4)
    store.update(job_id, status=JobStatus.ready_for_review, reject_reason=None, error=None)
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default=None, help="reset this job instead of creating one")
    args = parser.parse_args()
    job_id = seed(args.job_id)
    print(f"job_id={job_id}")
    print(f"open:   http://localhost:5173/?job={job_id}")


if __name__ == "__main__":
    main()
