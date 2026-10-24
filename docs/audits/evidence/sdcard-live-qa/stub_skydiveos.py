"""Tiny SkydiveOS stand-in for the SD-card live QA: records raw-upload notifies and card
status pushes; rejects (404) any filename the driver registers via POST /qa/reject."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException

app = FastAPI()
STATE: dict[str, Any] = {"notices": [], "snapshots": [], "reject": set()}


@app.post("/api/media/raw-upload")
def raw_upload(body: dict) -> dict:
    name = PurePosixPath(str(body.get("s3_key", ""))).name
    if name in STATE["reject"]:
        raise HTTPException(status_code=404, detail=f"no staff for {name} (QA reject)")
    STATE["notices"].append(body)
    return {"ok": True, "n": len(STATE["notices"])}


@app.post("/api/media/ingest-cards/status")
def card_status(body: dict) -> dict:
    STATE["snapshots"].append(body.get("cards", []))
    return {"ok": True}


@app.post("/qa/reject")
def set_reject(body: dict) -> dict:
    STATE["reject"] = set(body.get("files") or [])
    return {"reject": sorted(STATE["reject"])}


@app.get("/qa/state")
def state() -> dict:
    return {"notices": STATE["notices"], "last_snapshot": (STATE["snapshots"] or [[]])[-1],
            "snapshots": len(STATE["snapshots"])}
