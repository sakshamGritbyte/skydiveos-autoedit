# skydiveos-autoedit

Automated video editing pipeline for tandem skydiving footage. Module of
[SkydiveOS](https://skydiveos.com); replaces our dependency on Shred.

Takes raw GoPro footage (5–30 min jumps) and produces a 60–120 sec
customer-ready edit with intro, slow-mo highlights, music, and outro. Output
goes to an instructor review screen, then to the customer.

## Pipeline

`Ingest → Segment → Score → Compose → Render → Review → Deliver`

See [CLAUDE.md](./CLAUDE.md) for the full stage-by-stage description, repo
layout, and project conventions, and
[SKYDIVEOS_INTEGRATION.md](./SKYDIVEOS_INTEGRATION.md) for the REST contract
SkydiveOS drives this module with (create job → attach footage → auto-deliver →
status callbacks).

## Testing and go-live

- **[CLIENT_CALL_RUNBOOK.md](./CLIENT_CALL_RUNBOOK.md)** (Hinglish) — pre-flight both
  machines, demo the whole flow start to end, and what is still pending. Read this first
  before a customer-facing session.
- [QA_NO_GOPRO.md](./QA_NO_GOPRO.md) — test every package and the camera flow with no camera
- [MANUAL_QA.md](./MANUAL_QA.md) — the dropzone checklist, with a real camera
- [GO_LIVE.md](./GO_LIVE.md) — deployment/config checklist

## Quick start

```bash
cp .env.example .env       # then fill in ANTHROPIC_API_KEY etc.
make install               # uv sync
make test                  # pytest
make lint                  # ruff
make typecheck             # mypy
```

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and FFmpeg 6.0+ on
`PATH`.

## Status

Skeleton only — no pipeline logic implemented yet. The Open GoPro SDK is
vendored under [vendor/OpenGoPro/](vendor/OpenGoPro/) as a reference for the
ingest stage.
