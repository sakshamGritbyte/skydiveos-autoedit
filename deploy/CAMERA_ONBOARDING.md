# Adding a GoPro Camera — Staff Guide

How to set up a **new GoPro camera** so its footage uploads automatically after every
jump. Two parts: a **one-time setup** per camera, then **daily use** (nothing to think
about).

There are two kinds of computer at the dropzone:

- **Mac** — connects to cameras over **Bluetooth + WiFi** (wireless).
- **Windows** — connects to cameras over a **USB-C cable** (this PC has no Bluetooth).

Follow the section for the computer at your location.

---

## Before you start — find the camera's number

On the GoPro: **Preferences → Connections → Camera Info** (or **About → Camera Info**).
Note the **camera name / last digits**, e.g. `GoPro 4523` → the number is **4523**.
This "serial number" is what you type when pairing.

> If unsure which digits to use, ask the admin — the first camera is confirmed together.

---

## 🍎 MAC — one-time setup (per new camera)

Do these **once** for each new camera. After that, it's automatic forever.

### Step 1 — Register the camera in SkydiveOS
In the SkydiveOS web app:
> **Staff → open the instructor → GoPro Camera → "Pair GoPro" → type the serial number → Pair**

This links the camera to that instructor (so footage lands in the right account).

### Step 2 — Bluetooth-bond the camera to the Mac
1. Put the GoPro in pairing mode:
   **Preferences → Connections → Connect Device → GoPro Quik App**
2. Open **Terminal** on the Mac (⌘+Space → type `terminal` → Enter) and run:
   ```bash
   cd ~/skydiveos-autoedit
   uv run python -m ingest.pull --camera <SERIAL> --pair
   ```
   Replace `<SERIAL>` with the camera number from above (e.g. `4523`).
   Wait for it to say pairing succeeded.

> If Terminal says `uv: command not found`, run this first, then repeat:
> `export PATH="$HOME/.local/bin:$PATH"`

✅ Done. This camera is now set up.

### Daily use (Mac)
After every jump: **turn the camera ON and bring it within a few metres of the Mac.**
Within ~30 seconds the new clips upload automatically and appear in SkydiveOS.
Nothing else to do.

---

## 🪟 WINDOWS — one-time setup (per new camera)

Do this **once** for each new camera.

### Step 1 — Register the camera in SkydiveOS
In the SkydiveOS web app:
> **Staff → open the instructor → GoPro Camera → "Pair GoPro" → type the serial number → Pair**

That's the only setup step on Windows — **no Bluetooth command needed** (it uses the USB cable).

### Daily use (Windows)
After every jump: **plug the camera into the PC with the USB-C cable** (camera ON).
Within ~30 seconds the new clips upload automatically and appear in SkydiveOS.
Unplug when done.

---

## Quick reference

| | One-time per camera | Every jump |
|---|---|---|
| **Mac** | SkydiveOS "Pair GoPro" + `--pair` command | Camera ON, bring near the Mac |
| **Windows** | SkydiveOS "Pair GoPro" only | Plug in the USB-C cable |

**Key rules**
- The camera must be paired in SkydiveOS **before** footage can upload — an unregistered
  camera is ignored.
- The Mac `--pair` command is **once per camera, ever** (only re-run if the camera is
  factory-reset or was paired to another phone/computer).
- **Old clips are never re-uploaded** — only new footage each time.
- Windows never needs the `--pair` command.

---

## If footage doesn't show up

1. Is the camera **paired in SkydiveOS** for that instructor? (most common cause)
2. Is the camera **ON**? Mac: within a few metres. Windows: cable firmly plugged in.
3. Wait a full minute — it scans every 30 seconds.
4. Still nothing → contact the admin (there are logs to check on the computer).

*Admin note: logs live at `~/skydiveos-autoedit/logs/ingest.err.log` (Mac) or
`...\skydiveos-autoedit\logs\ingest.out.log` (Windows).*
