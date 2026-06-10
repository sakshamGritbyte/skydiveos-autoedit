"""A throwaway mock of the SkydiveOS media module — for testing auto-discovery.

Stands in for the SkydiveOS web layer so you can test the auto-discovery hand-off
without the real SkydiveOS running. Auto-discovery uploads the pulled file to S3 and
then POSTs a small JSON ``{s3_key, camera_id, instructor_id}`` here; this mock just
records that notification and exposes a ``GET /media`` "media module" view so you can
watch entries appear (the file itself lives in S3, referenced by ``s3_key``).

Run it::

    uvicorn scripts.mock_skydiveos:app --port 4100

Point the AI backend at it (so discovery notifies here instead of real SkydiveOS)::

    SKYDIVEOS_API_BASE=http://localhost:4100   # in .env, then start uvicorn api.app:app

Watch entries land::

    curl localhost:4100/media        # the "media module" — lists what was registered
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock SkydiveOS media receiver")

#: In-memory "media module" — what GET /media returns.
_MEDIA: list[dict[str, object]] = []


class RawUploadNotice(BaseModel):
    """The JSON auto-discovery POSTs after uploading the file to S3."""

    s3_key: str
    camera_id: str
    instructor_id: str | None = None


@app.post("/api/media/raw-upload")
def raw_upload(notice: RawUploadNotice) -> dict[str, object]:
    """Receive an S3-key notification from auto-discovery and add it to the media module."""
    item: dict[str, object] = {
        "id": len(_MEDIA) + 1,
        "s3_key": notice.s3_key,
        "camera_id": notice.camera_id,
        "instructor_id": notice.instructor_id,
    }
    _MEDIA.append(item)
    print(
        f"[mock-skydiveos] registered {notice.s3_key} "
        f"from {notice.camera_id} -> instructor {notice.instructor_id}"
    )
    return {"ok": True, "media": item}


@app.get("/media")
def list_media(instructor_id: str | None = None) -> dict[str, object]:
    """The 'media module': every registered file, optionally scoped to one instructor."""
    items = [m for m in _MEDIA if instructor_id is None or m["instructor_id"] == instructor_id]
    return {"count": len(items), "media": items}
