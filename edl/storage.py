"""Per-job persistence for the EDL artifact.

Every Compose run writes its EDL to ``<jobs_root>/{job_id}/edl.json`` (CLAUDE.md:
"persisted with every job (lets us replay/A-B test)"). Keeping this in one place
means the Compose stage, the instructor-review UI, and ``scripts/replay_edl.py``
all agree on where a job's edit lives.

* ``<jobs_root>`` defaults to ``./jobs`` and is overridable with the ``$JOBS_ROOT``
  env var (or an explicit argument) — mirroring how /ingest resolves its storage
  root.

The on-disk format is the JSON serialization of :class:`~edl.schema.EditDecisionList`
(version-tagged), written with ``indent=2`` + trailing newline to match the rest
of the pipeline's artifacts.
"""

from __future__ import annotations

import os
from pathlib import Path

from .schema import EditDecisionList

DEFAULT_JOBS_ROOT = Path("jobs")
_ENV_ROOT = "JOBS_ROOT"
EDL_FILENAME = "edl.json"


def jobs_root(override: str | Path | None = None) -> Path:
    """Resolve the jobs root: explicit arg > ``$JOBS_ROOT`` > default ``./jobs``."""
    if override is not None:
        return Path(override)
    env = os.environ.get(_ENV_ROOT)
    return Path(env) if env else DEFAULT_JOBS_ROOT


def job_dir(job_id: str, root: str | Path | None = None) -> Path:
    """Directory holding one job's artifacts: ``<jobs_root>/{job_id}``."""
    return jobs_root(root) / job_id


def edl_path(job_id: str, root: str | Path | None = None) -> Path:
    """Full path to a job's persisted EDL: ``<jobs_root>/{job_id}/edl.json``."""
    return job_dir(job_id, root) / EDL_FILENAME


def persist_edl(edl: EditDecisionList, job_id: str, root: str | Path | None = None) -> Path:
    """Write ``edl`` to ``<jobs_root>/{job_id}/edl.json`` and return its path."""
    path = edl_path(job_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(edl.model_dump_json(indent=2) + "\n")
    return path


def load_edl(job_id: str, root: str | Path | None = None) -> EditDecisionList:
    """Load a previously persisted EDL (used by ``scripts/replay_edl.py``)."""
    return EditDecisionList.model_validate_json(edl_path(job_id, root).read_text())
