"""Locating the brand assets the Render stage layers onto every edit.

The intro/outro cards and the music library live under ``/templates`` (CLAUDE.md
Repo Structure). This module is the single place that knows that layout, mirroring
how :mod:`edl.storage` owns the jobs layout and :mod:`ingest.storage` owns the raw
layout.

Resolution is forgiving: a missing intro/outro is reported as ``None`` so the
Render stage can synthesise a plain default card rather than fail (a brand-new
deployment may not have dropped its PSD-exported cards in yet), and the music
track can be named or just defaulted to the first track on disk.

The templates root is the repo ``templates/`` dir, overridable with
``$TEMPLATES_ROOT`` or an explicit argument.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"
_ENV_ROOT = "TEMPLATES_ROOT"

INTRO_NAME = "intro.mp4"
OUTRO_NAME = "outro.mp4"
MUSIC_DIRNAME = "music"
_MUSIC_SUFFIXES = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg")


def templates_root(override: str | Path | None = None) -> Path:
    """Resolve the templates root: explicit arg > ``$TEMPLATES_ROOT`` > repo default."""
    if override is not None:
        return Path(override)
    env = os.environ.get(_ENV_ROOT)
    return Path(env) if env else DEFAULT_TEMPLATES_ROOT


def resolve_intro(root: str | Path | None = None) -> Path | None:
    """Path to ``templates/intro.mp4`` if present, else ``None``."""
    path = templates_root(root) / INTRO_NAME
    return path if path.exists() else None


def resolve_outro(root: str | Path | None = None) -> Path | None:
    """Path to ``templates/outro.mp4`` if present, else ``None``."""
    path = templates_root(root) / OUTRO_NAME
    return path if path.exists() else None


def resolve_music(name: str | None = None, root: str | Path | None = None) -> Path | None:
    """Resolve a backing track from ``templates/music/``.

    With ``name`` given, match it by stem or full filename (so the EDL's ``music``
    field — e.g. ``"upbeat_indie"`` — maps to ``upbeat_indie.mp3``). With no name,
    return the first track found (sorted, for determinism). ``None`` if the music
    directory is absent/empty or the named track is missing.
    """
    music_dir = templates_root(root) / MUSIC_DIRNAME
    if not music_dir.is_dir():
        return None
    tracks = sorted(p for p in music_dir.iterdir() if p.suffix.lower() in _MUSIC_SUFFIXES)
    if not tracks:
        return None
    if name is None:
        return tracks[0]
    for track in tracks:
        if name in (track.name, track.stem):
            return track
    return None
