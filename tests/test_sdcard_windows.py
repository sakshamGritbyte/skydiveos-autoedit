"""Windows card discovery: a card mounts AS a drive, not inside a container.

Three things break on Windows and none of them are config:

1. ``SDCARD_MOUNT_ROOTS`` was split on a literal ``':'`` — and a drive letter *contains*
   a colon, so ``E:\\`` became ``("E", "\\")`` and found nothing.
2. The mount walk looked for ``<root>/<vol>/DCIM``. On POSIX the root is a container
   (``/media/<user>/<vol>/DCIM``); on Windows the root IS the volume (``E:\\DCIM``), one
   level shallower, so nothing matched. Searching a *volume* root one level down instead
   would be worse than useless — ``C:/*/DCIM`` would claim any stray DCIM folder on the
   system drive as an inserted camera card.
3. The id fallback used the volume label from the path, and ``Path("E:/").name`` is
   **empty** — so every unlabelled Windows card derived the same id. Two cards sharing an
   id share a staging tree and a retention ledger, which is exactly the collision that
   makes a filename an unsafe delete signal (``AUDIT_MEDIA_MATCH_ISOLATION.md`` §3-F).

The drive letters are *probed* rather than configured, deliberately: a reader gets ``E:``
today and ``F:`` after another device is plugged in, so asking an operator to keep an env
var in step would make "insert the card" a two-step job.

Runs on any platform — the volume-root shape is what is under test, and a tmp_path
directory is a volume root as far as the walk is concerned once we say so.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ingest.sdcard import (
    _default_mount_roots,
    _is_volume_root,
    _mounts_with_dcim,
    card_id_for_mount,
    find_cards,
)


def _card_at(mount: Path, *, serial: str | None = None) -> Path:
    """A GoPro-shaped card: ``DCIM/100GOPRO`` and optionally ``MISC/version.txt``."""
    (mount / "DCIM" / "100GOPRO").mkdir(parents=True, exist_ok=True)
    if serial is not None:
        (mount / "MISC").mkdir(parents=True, exist_ok=True)
        (mount / "MISC" / "version.txt").write_text(
            '{"camera serial number":"' + serial + '"}'
        )
    return mount


# --------------------------------------------------------------------------- #
# 1. The default roots
# --------------------------------------------------------------------------- #


def test_windows_defaults_are_drive_letters(monkeypatch: Any) -> None:
    monkeypatch.setattr(os, "name", "nt")
    roots = _default_mount_roots()
    assert roots[0] == "C:\\"
    assert roots[-1] == "Z:\\"
    # A: and B: are the legacy floppy letters — probing them can stall on hardware that
    # still claims them, and no card reader is ever assigned one.
    assert not any(r.startswith(("A:", "B:")) for r in roots)


def test_posix_defaults_are_unchanged(monkeypatch: Any) -> None:
    monkeypatch.setattr(os, "name", "posix")
    assert _default_mount_roots() == ("/media", "/run/media", "/Volumes")


# --------------------------------------------------------------------------- #
# 2. Volume root vs container root
# --------------------------------------------------------------------------- #


def test_a_filesystem_root_is_a_volume_root() -> None:
    assert _is_volume_root(Path("/")) is True
    assert _is_volume_root(Path("/media")) is False
    assert _is_volume_root(Path("/media/user/CARD")) is False


def _as_volume_root(monkeypatch: Any) -> None:
    """Make the walk treat every root as a volume, i.e. the Windows drive shape.

    ``_mounts_with_dcim`` normalises each root with ``Path(root)``, so a Path subclass
    with an overridden ``parent`` is discarded before the check runs — the honest seam is
    the predicate itself, which has its own tests above.
    """
    import ingest.sdcard as sdcard

    monkeypatch.setattr(sdcard, "_is_volume_root", lambda _p: True)


def test_a_card_directly_under_a_volume_root_is_found(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """``E:\\DCIM`` — the Windows shape, one level shallower than POSIX."""
    _card_at(tmp_path)

    # As a container root, nothing is at this depth: this is the old behaviour, and it is
    # why Windows found no cards at all.
    assert _mounts_with_dcim([tmp_path]) == []

    _as_volume_root(monkeypatch)
    assert _mounts_with_dcim([tmp_path]) == [tmp_path]


def test_a_container_root_still_finds_cards_one_and_two_levels_down(
    tmp_path: Path,
) -> None:
    """The POSIX shapes must be untouched: ``/media/<vol>`` and ``/media/<user>/<vol>``."""
    _card_at(tmp_path / "CARD-A")
    _card_at(tmp_path / "someuser" / "CARD-B")

    found = _mounts_with_dcim([tmp_path])
    assert found == sorted([tmp_path / "CARD-A", tmp_path / "someuser" / "CARD-B"])


def test_a_volume_root_is_not_searched_one_level_down(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The dangerous direction: ``C:/*/DCIM`` claiming a folder on the system drive.

    A stray ``C:\\Photos\\DCIM`` is not an inserted camera card, and treating it as one
    would ingest a stranger's footage into a job.
    """
    _card_at(tmp_path / "Photos")  # a DCIM folder that is NOT a mounted card

    _as_volume_root(monkeypatch)
    assert _mounts_with_dcim([tmp_path]) == []


# --------------------------------------------------------------------------- #
# 3. The id fallback must not collapse to one value
# --------------------------------------------------------------------------- #


def test_the_serial_still_wins_which_is_the_normal_case(tmp_path: Path) -> None:
    """A real GoPro card carries ``MISC/version.txt``, so Windows changes nothing here —
    and the id matches what a wireless pull of the same camera derives, so both share one
    staging tree and one retention ledger."""
    _card_at(tmp_path, serial="C3441325604313")
    assert card_id_for_mount(tmp_path) == "4313"


def test_the_windows_fallback_uses_the_drive_letter_not_an_empty_label() -> None:
    """``Path("E:/").name`` is empty, so without this every unlabelled Windows card
    derives the SAME id — and a shared id means a shared staging tree and ledger."""

    class _Drive(Path):
        """A path shaped like ``E:\\`` on a POSIX test host: empty name, a drive."""

        @property
        def name(self) -> str:
            return ""

        @property
        def drive(self) -> str:
            return "E:"

        def __truediv__(self, other: Any) -> Path:  # version.txt lookup misses
            return Path("/nonexistent") / other

    assert card_id_for_mount(_Drive("/tmp")) == "sd-E"


def test_two_windows_cards_without_serials_get_different_ids() -> None:
    """The collision this closes, stated as the property that matters."""

    def _drive(letter: str) -> Path:
        class _D(Path):
            @property
            def name(self) -> str:
                return ""

            @property
            def drive(self) -> str:
                return f"{letter}:"

            def __truediv__(self, other: Any) -> Path:
                return Path("/nonexistent") / other

        return _D("/tmp")

    assert card_id_for_mount(_drive("E")) != card_id_for_mount(_drive("F"))


def test_a_labelled_posix_card_is_unchanged(tmp_path: Path) -> None:
    card = _card_at(tmp_path / "TESTCARD")
    assert card_id_for_mount(card) == "sd-TESTCARD"


# --------------------------------------------------------------------------- #
# 4. The env var separator
# --------------------------------------------------------------------------- #


def test_mount_roots_split_on_os_pathsep(monkeypatch: Any) -> None:
    """A literal ':' split would turn ``E:\\`` into ("E", "\\") and find nothing."""
    from api.config import get_settings

    monkeypatch.setenv("SDCARD_MOUNT_ROOTS", os.pathsep.join(["/one", "/two"]))
    get_settings.cache_clear()
    try:
        assert get_settings().sdcard_mount_roots == ("/one", "/two")
    finally:
        get_settings.cache_clear()


def test_unset_mount_roots_fall_back_to_the_platform_default(monkeypatch: Any) -> None:
    from api.config import get_settings
    from ingest.sdcard import DEFAULT_MOUNT_ROOTS

    monkeypatch.delenv("SDCARD_MOUNT_ROOTS", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().sdcard_mount_roots == DEFAULT_MOUNT_ROOTS
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# End to end through the public entry point
# --------------------------------------------------------------------------- #


def test_find_cards_reports_a_card_at_a_volume_root(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _card_at(tmp_path, serial="C3441325604313")
    _as_volume_root(monkeypatch)

    cards = find_cards([tmp_path])
    assert [c.camera_id for c in cards] == ["4313"]
    assert cards[0].mount == tmp_path
