"""Bug 439 — the served gallery's bulk download: ONE streamed ZIP per click.

``GET /j/{code}/zip/photos`` (the photo set) and ``GET /j/{code}/zip/all`` (every
deliverable the customer OWNS plus the photo set) answer with a ZIP built on the
fly and streamed to the response chunk by chunk — the archive never exists in
memory or on disk, so a 50-photo set and a multi-video "everything" cost the same
few MB of RAM. Raw camera masters are deliberately NOT in either archive: they are
several GB each, and one huge archive is the download most likely to die on a
phone; the Raw Footage cards keep their own per-clip downloads.

Rules (the route enforces the first two; this module is the mechanics):

* **The entitlement, never the URL, decides what goes in** — a locked deliverable
  and a locked photo set are never zipped (their only address is the watermarked
  preview at ``/j/{code}/…``). A ZIP with nothing the customer owns is a 404, not
  an empty archive.
* **Same gallery-code credential as every ``/j/`` route**; the code is never logged.
* **Never a silent gap**: a file that cannot be opened (pruned and not in S3, a
  read error) is skipped and NAMED in ``MISSING-FILES.txt`` inside the archive.
* **Deflate, never STORE**: a streamed entry always carries a data descriptor
  (its size is unknown up front), and STORED + data-descriptor entries are the
  combination some unzippers (historically macOS/iOS Archive Utility) refuse.
  Level 1, because JPEGs and MP4s barely compress.

Pure apart from the openers the caller supplies, so it is tested without the API.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import quote

#: The two archives the gallery offers.
ZIP_KINDS: frozenset[str] = frozenset({"photos", "all"})
#: Note appended when a file could not be included.
MISSING_NOTE = "MISSING-FILES.txt"
#: Read size per chunk — also roughly the most the stream buffers at once.
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ZipEntry:
    """One file to put in the archive: its name inside the ZIP and how to open it.

    ``open`` returns a binary file-like object (local file, S3 body); it may raise,
    which lists the entry in ``MISSING-FILES.txt`` instead of failing the archive.
    """

    arcname: str
    open: Callable[[], BinaryIO]


class _Sink(io.RawIOBase):
    """A write-only, NON-seekable byte sink the ZIP writer fills and the stream drains.

    No ``seek``/``tell``: :class:`zipfile.ZipFile` then switches to streaming mode
    (data descriptors, its own offset count) instead of seeking back to patch
    headers — which is what lets the bytes leave as soon as they are written.
    """

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, b: bytes) -> int:  # type: ignore[override]
        self._chunks.append(bytes(b))
        return len(b)

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def zip_file_names(customer_name: str | None, kind: str) -> tuple[str, str]:
    """``(ascii, utf8)`` download names from the customer's name: ``Ana-Silva-photos.zip``.

    The UTF-8 spelling keeps accents (``Éloïse-Roy-photos.zip``) for ``filename*``;
    the ASCII one is the header-safe fallback. Path separators, quotes and control
    characters never reach either.
    """
    suffix = "gallery" if kind == "all" else "photos"
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", (customer_name or "").strip())
    base = re.sub(r"\s+", "-", base).strip("-")[:80]
    ascii_base = re.sub(r"[^A-Za-z0-9-]+", "-", _fold(base))
    ascii_base = re.sub(r"-+", "-", ascii_base).strip("-")
    return f"{ascii_base or 'jump'}-{suffix}.zip", f"{base or 'jump'}-{suffix}.zip"


def content_disposition(customer_name: str | None, kind: str) -> str:
    """RFC 6266 attachment header carrying both spellings of the name."""
    ascii_name, utf8_name = zip_file_names(customer_name, kind)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(utf8_name)}"


def unique_arcnames(names: Iterable[str]) -> list[str]:
    """Make archive names unique (``a.jpg``, ``a-2.jpg``) and path-safe."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in names:
        parts = [p for p in str(raw).replace("\\", "/").split("/") if p not in ("", ".", "..")]
        name = "/".join(parts) or "file"
        n = seen.get(name, 0) + 1
        seen[name] = n
        if n > 1:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem}-{n}.{ext}" if dot and stem else f"{name}-{n}"
        out.append(name)
    return out


def stream_zip(entries: Sequence[ZipEntry]) -> Iterator[bytes]:
    """Yield the ZIP for ``entries`` as it is written, one file at a time.

    Each source is opened only when its turn comes and closed before the next opens,
    so at most one file (local or S3) is held open. A source that fails to OPEN is
    listed in ``MISSING-FILES.txt``; a source that fails MID-READ ends the stream
    with the error (a truncated download the browser marks as failed — never an
    archive that looks complete and isn't).
    """
    sink = _Sink()
    missing: list[str] = []
    names = unique_arcnames(e.arcname for e in entries)
    with zipfile.ZipFile(
        sink, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
    ) as zf:
        for entry, name in zip(entries, names, strict=True):
            try:
                src = entry.open()
            except Exception:  # noqa: BLE001 - any open failure is a missing file
                missing.append(name)
                continue
            try:
                with zf.open(name, "w", force_zip64=True) as dest:
                    while True:
                        chunk = src.read(_CHUNK)
                        if not chunk:
                            break
                        dest.write(chunk)
                        out = sink.drain()
                        if out:
                            yield out
            finally:
                close = getattr(src, "close", None)
                if callable(close):
                    close()
            out = sink.drain()
            if out:
                yield out
        if missing:
            zf.writestr(
                MISSING_NOTE,
                "Some files could not be included in this download:\n\n"
                + "".join(f"  - {n}\n" for n in missing)
                + "\nThey are still available individually from your gallery page,\n"
                "or ask the dropzone to send them to you.\n",
            )
    tail = sink.drain()  # the central directory, written on close
    if tail:
        yield tail
