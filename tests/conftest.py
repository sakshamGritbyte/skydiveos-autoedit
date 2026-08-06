"""Shared test fixtures: pin the environment the suite runs against.

:mod:`api.config` loads the repo's ``.env`` on import, so without this the suite
inherits whatever the developer's *deployment* is configured for — and a dev box that
has the camera simulation and hands-off delivery switched on makes the tests slow,
non-deterministic, and messy:

* ``ENABLE_AUTO_DISCOVERY=1`` + ``CAMERA_SCANNER=static`` means every ``TestClient``
  lifespan starts a discovery loop that stages ``DISCOVERY_SAMPLE_COUNT`` copies of the
  sample MP4 — dozens of times over a full run.
* ``AUTO_DELIVER=1`` makes a pipeline task try a real S3 upload + SMTP send.
* The two storage roots (``$RAW_STORAGE_ROOT`` for the camera card mirror, ``$ARCHIVE_ROOT``
  for the browsable jump archive — see :mod:`api.archive`) are resolved from settings deep
  inside the upload/task code, so they'd write into the developer's real ``./raw-storage``.

The autouse fixture pins all of that per test: both storage roots go into the test's own
``tmp_path``, the deployment-only switches are forced off, and the cached
:class:`~api.config.Settings` snapshot is dropped around every test so a test can freely
``monkeypatch.setenv`` a knob without leaking it into the next one. Tests that need a
particular setting build their own ``Settings`` and monkeypatch ``get_settings`` (see
``test_delivery.py``), which keeps working.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from api.config import get_settings


@pytest.fixture(autouse=True)
def pinned_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate each test's storage roots and switch off the deployment-only behaviours."""
    monkeypatch.setenv("RAW_STORAGE_ROOT", str(tmp_path / "raw-storage"))
    monkeypatch.setenv("ARCHIVE_ROOT", str(tmp_path / "raw-storage"))
    # Deployment opt-ins that must never fire implicitly in a test run.
    monkeypatch.setenv("ENABLE_AUTO_DISCOVERY", "0")
    monkeypatch.setenv("AUTO_DELIVER", "0")
    # Never let a test publish Celery tasks into the dev box's REAL Redis: a task
    # queued here outlives pytest and a later live worker will consume it (observed:
    # test-job settle checks crashing the dropzone worker). Dead port → a test that
    # reaches an unmocked .apply_async fails loudly instead of polluting the broker.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    # A dev box with the service-token gate configured would 401 every TestClient
    # request; the tests that assert the gate itself set a token explicitly.
    for var in ("AUTO_EDIT_API_KEY", "AI_BACKEND_API_KEY", "AUTO_EDIT_SERVICE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    # Path B's go-live prerequisite: a locked (preview_only) job can only be delivered
    # as the served /j/{code} gallery, so ``POST /jobs`` refuses to create one without
    # an origin to serve it from. Pin it here so the suite exercises the *deliverable*
    # configuration; the tests that assert the gate itself unset it explicitly.
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://gallery.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
