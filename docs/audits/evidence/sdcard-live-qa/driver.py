"""SD-card live QA driver: real API in sdcard mode + fake cards + stub SkydiveOS + real S3.
Scenarios: normal card → safe; second card same camera same day with reused filename
(the P0) → staged beside, uploaded under its own key, safe; refused clip → needs_attention;
restart → reconciliation re-hands off exactly the unconfirmed clip. Cleans up its S3 keys."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path("/home/msi/Desktop/Development/skydiving-autoedit/skydiveos-autoedit")
SCR = Path(__file__).resolve().parent
READER, RAW, JOBS, LOGS = SCR / "reader", SCR / "raw", SCR / "jobs", SCR / "logs"
API, STUB = "http://127.0.0.1:8010", "http://127.0.0.1:4100"
SRC = REPO / "raw-storage/_camera-staging/sd-disk/2026-10-24"
PY = str(REPO / ".venv/bin/python")
os.chdir(REPO)
sys.path.insert(0, str(REPO))
from api.auth import service_auth_headers  # noqa: E402
from api.config import get_settings  # noqa: E402

HDR = service_auth_headers()
results: list[tuple[str, bool, str]] = []
procs: list[subprocess.Popen] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def env_for_api() -> dict[str, str]:
    e = dict(os.environ)
    e.update({
        "ENABLE_AUTO_DISCOVERY": "1", "CAMERA_SCANNER": "sdcard",
        "SDCARD_MOUNT_ROOTS": str(READER), "RAW_STORAGE_ROOT": str(RAW), "JOBS_ROOT": str(JOBS),
        "ARCHIVE_ROOT": str(RAW), "DELETE_AFTER_TRANSFER": "0", "DISCOVERY_INTERVAL_SECONDS": "3",
        "SKYDIVEOS_API_BASE": STUB, "AUTO_DELIVER": "0", "CELERY_TASK_ALWAYS_EAGER": "1",
        "MONGO_URL": "", "PUBLIC_BASE_URL": API, "HANDOFF_RECONCILE_ON_START": "1",
        "CARD_SAFE_REQUIRES_UPLOAD": "1", "SDCARD_QR_MAX_CLIP_SECONDS": "0",
        "PATH": os.path.expanduser("~/.local/bin/ffbin") + ":" + os.environ.get("PATH", ""),
    })
    return e


def start_stub() -> None:
    p = subprocess.Popen([PY, "-m", "uvicorn", "stub_skydiveos:app", "--port", "4100"],
                         cwd=SCR, stdout=open(LOGS / "stub.log", "a"), stderr=subprocess.STDOUT)
    procs.append(p)
    wait_http(f"{STUB}/qa/state")


def start_api() -> subprocess.Popen:
    p = subprocess.Popen([PY, "-m", "uvicorn", "api.app:app", "--port", "8010"], cwd=REPO,
                         env=env_for_api(), stdout=open(LOGS / "api.log", "a"), stderr=subprocess.STDOUT)
    procs.append(p)
    wait_http(f"{API}/ingest/cards", headers=HDR)
    return p


def stop(p: subprocess.Popen) -> None:
    p.terminate()
    try:
        p.wait(20)
    except subprocess.TimeoutExpired:
        p.kill()


def wait_http(url: str, headers: dict | None = None, timeout: float = 60) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, headers=headers, timeout=3).status_code < 500:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} not up")


def cards() -> list[dict]:
    return httpx.get(f"{API}/ingest/cards", headers=HDR, timeout=10).json()


def stub_state() -> dict:
    return httpx.get(f"{STUB}/qa/state", timeout=10).json()


def wait_state(camera_id: str, state: str, timeout: float = 240) -> dict | None:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        for c in cards():
            if c["camera_id"] == camera_id:
                last = c
                if c["state"] == state:
                    return c
        time.sleep(1)
    print("  last row:", json.dumps(last), flush=True)
    return None


def wait_gone(camera_id: str, timeout: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if all(c["camera_id"] != camera_id for c in cards()):
            return True
        time.sleep(1)
    return False


def make_card(name: str, serial: str, files: dict[str, bytes]) -> Path:
    mount = READER / name
    (mount / "DCIM" / "100GOPRO").mkdir(parents=True)
    (mount / "MISC").mkdir()
    (mount / "MISC" / "version.txt").write_text(json.dumps({"camera serial number": serial}))
    for fname, body in files.items():
        (mount / "DCIM" / "100GOPRO" / fname).write_bytes(body)
    return mount


def remove_card(mount: Path) -> None:
    shutil.move(str(mount), str(SCR / f"removed-{mount.name}-{int(time.time())}"))


def ledger(camera_id: str) -> dict:
    p = RAW / "_camera-staging" / camera_id / ".transferred.json"
    return json.loads(p.read_text()) if p.exists() else {}


def main() -> int:
    for d in (READER, RAW, JOBS, LOGS):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    clip1 = (SRC / "GX010001.MP4").read_bytes()
    clip2 = (SRC / "GX010002.MP4").read_bytes()
    clip3 = (SRC / "GX010003.MP4").read_bytes()
    day = time.strftime("%Y-%m-%d", time.gmtime())
    start_stub()
    api = start_api()
    try:
        # --- 1. a normal card ---
        a = make_card("CARD-A", "C3504224549999", {"GX010001.MP4": clip1, "GX010002.MP4": clip2})
        row = wait_state("9999", "safe_to_remove")
        check("card A reaches safe_to_remove", row is not None)
        n = stub_state()["notices"]
        keys_a = sorted(x["s3_key"] for x in n)
        check("card A: 2 notifies with day-scoped keys", keys_a == [f"raw/9999/{day}/GX010001.MP4", f"raw/9999/{day}/GX010002.MP4"], str(keys_a))
        check("card A: ledger confirms both (size recorded)", all(v.get("size") for v in ledger("9999").values()) and len(ledger("9999")) == 2, json.dumps(ledger("9999")))
        check("card A: uploaded_files counted on the row", row is not None and row.get("uploaded_files") == 2, str(row and row.get("uploaded_files")))
        remove_card(a)
        check("card A removed → row gone", wait_gone("9999"))

        # --- 2. the P0: second card, same camera, same day, reused filename ---
        clip1b = clip1 + b"\x00" * 1234  # a different recording, different size
        b = make_card("CARD-B", "C3504224549999", {"GX010001.MP4": clip1b, "GX010003.MP4": clip3})
        row = wait_state("9999", "safe_to_remove")
        check("card B reaches safe_to_remove", row is not None)
        staged = sorted(str(p.relative_to(RAW)) for p in (RAW / "_camera-staging" / "9999").rglob("GX010001.MP4"))
        check("card B: GX010001 staged BESIDE card A's (collision sibling)", len(staged) == 2 and any("/alt-" in s for s in staged), str(staged))
        n = stub_state()["notices"]
        b_keys = [x["s3_key"] for x in n if x["s3_key"].endswith("GX010001.MP4")]
        check("card B: GX010001 handed off under its OWN key (no overwrite)", len(b_keys) == 2 and len(set(b_keys)) == 2 and f"raw/9999/{day}/GX010001.MP4" in b_keys, str(b_keys))
        led = ledger("9999")
        recs = [k for k in led if led[k].get("filename", k.split("#")[0]) == "GX010001.MP4"]
        check("card B: ledger holds one record per recording for GX010001", len(recs) == 2 and {led[k]["size"] for k in recs} == {len(clip1), len(clip1b)}, str(recs))
        check("card B: 4 notifies in total (nothing dropped)", len(n) == 4, str(len(n)))
        remove_card(b)
        wait_gone("9999")

        # --- 3. a refused clip must not read green ---
        httpx.post(f"{STUB}/qa/reject", json={"files": ["GX010001.MP4"]}, timeout=10)
        c = make_card("CARD-C", "C3504224548888", {"GX010001.MP4": clip2, "GX010002.MP4": clip3})
        row = wait_state("8888", "needs_attention", timeout=180)
        check("card C: refused clip → needs_attention (never safe_to_remove)", row is not None, "")
        if row:
            check("card C: row names the refused file", row.get("rejected_files") == ["GX010001.MP4"] and "GX010001.MP4" in (row.get("error") or "") and row.get("pending_files") == 1, json.dumps({k: row.get(k) for k in ("rejected_files", "pending_files", "error", "issues")}))
        rej = RAW / "_camera-staging" / "8888" / ".rejected.json"
        check("card C: rejection recorded durably", rej.exists() and "GX010001.MP4" in rej.read_text(), "")
        check("card C: the other clip WAS confirmed", any(v.get("filename", k.split('#')[0]) == "GX010002.MP4" for k, v in ledger("8888").items()), json.dumps(ledger("8888")))
        httpx.post(f"{STUB}/qa/reject", json={"files": []}, timeout=10)
        remove_card(c)
        wait_gone("8888", timeout=30)

        # --- 4. restart recovery: forget card B's GX010001 confirmation, restart ---
        before = len(stub_state()["notices"])
        stop(api)
        led = ledger("9999")
        victim = next(k for k in led if led[k].get("filename", k.split("#")[0]) == "GX010001.MP4" and led[k]["size"] == len(clip1b))
        del led[victim]
        (RAW / "_camera-staging" / "9999" / ".transferred.json").write_text(json.dumps(led, indent=2))
        api = start_api()
        # Two clips are unconfirmed on disk: card B's GX010001 (record deleted above) and
        # card C's GX010001 (refused earlier; a restart is an operator retry, and the
        # stub now accepts it). Exactly those two must be re-handed off — nothing else.
        t0 = time.time()
        while time.time() - t0 < 90 and len(stub_state()["notices"]) < before + 2:
            time.sleep(1)
        time.sleep(8)  # settle: nothing else must be re-handed off
        after = stub_state()["notices"]
        new_keys = sorted(x["s3_key"] for x in after[before:])
        check("restart: exactly the TWO unconfirmed clips re-handed off", len(after) == before + 2, f"{before} -> {len(after)}: {new_keys}")
        want = sorted([next(k for k in b_keys if k != f"raw/9999/{day}/GX010001.MP4"), f"raw/8888/{day}/GX010001.MP4"])
        check("restart: card B's GX010001 (same fingerprint key) + card C's refused clip", new_keys == want, f"{new_keys} vs {want}")
        check("restart: ledger record restored", any(v.get("size") == len(clip1b) for v in ledger("9999").values()), "")
        rej_after = json.loads((RAW / "_camera-staging" / "8888" / ".rejected.json").read_text())
        check("restart: the refused clip's rejection is cleared once it is accepted", rej_after == {} and any(v.get("filename", k.split('#')[0]) == "GX010001.MP4" for k, v in ledger("8888").items()), json.dumps(rej_after))
        return 0 if all(ok for _, ok, _ in results) else 1
    finally:
        stub_state_cache()  # read the notices while the stub is still up
        for p in procs:
            stop(p)
        # --- S3 cleanup: only the objects this run created ---
        try:
            import boto3
            s = get_settings()
            client = boto3.client("s3", endpoint_url=s.s3_endpoint_url, region_name=s.s3_region)
            keys = sorted({x["s3_key"] for x in stub_state_cache()})
            for k in keys:
                client.delete_object(Bucket=s.s3_bucket, Key=k)
            print(f"S3 cleanup: deleted {len(keys)} QA object(s) from {s.s3_bucket}: {keys}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"S3 cleanup FAILED: {e!r}", flush=True)
        (SCR / "results.json").write_text(json.dumps(results, indent=2))
        print("\nSUMMARY:", sum(ok for _, ok, _ in results), "passed /", sum(not ok for _, ok, _ in results), "failed", flush=True)


_last_notices: list = []


def stub_state_cache() -> list:
    global _last_notices
    try:
        _last_notices = stub_state()["notices"]
    except Exception:
        pass
    return _last_notices


if __name__ == "__main__":
    # keep a copy of notices before the stub is stopped
    import atexit
    atexit.register(lambda: None)
    sys.exit(main())
