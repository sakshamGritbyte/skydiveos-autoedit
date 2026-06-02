"""Compose stage (/edl): turn the pipeline's signals into a renderable edit.

Public surface:

* :func:`compose_edl` — the stage entry point: timeline + scores + customer
  metadata -> a single Claude call -> a validated, persisted
  :class:`EditDecisionList`.
* :class:`EditDecisionList` / :class:`Clip` / :class:`Transition` — the EDL schema,
  the contract handed to /render.
* :func:`load_edl` / :func:`edl_path` — read back a job's persisted EDL (replay).
* :class:`EdlError` — raised when a valid EDL cannot be produced.

See :mod:`edl.compose` for the prompt/stylistic rules and :mod:`edl.schema` for the
data model. All timestamps are seconds (float) on the source timeline.
"""

from __future__ import annotations

from .compose import (
    DEFAULT_TARGET_DURATION,
    MODEL,
    EdlError,
    compose_edl,
)
from .schema import EDL_VERSION, Clip, EditDecisionList, Transition
from .storage import edl_path, job_dir, load_edl, persist_edl

__all__ = [
    "compose_edl",
    "EditDecisionList",
    "Clip",
    "Transition",
    "EdlError",
    "EDL_VERSION",
    "MODEL",
    "DEFAULT_TARGET_DURATION",
    "persist_edl",
    "load_edl",
    "edl_path",
    "job_dir",
]
