# skydiveos-autoedit

Automated video editing pipeline for tandem skydiving footage. Module of
[SkydiveOS](https://skydiveos.com); replaces our dependency on Shred.

Takes raw GoPro footage (5–30 min jumps) and produces a 60–120 sec
customer-ready edit with intro, slow-mo highlights, music, and outro. Output
goes to an instructor review screen, then to the customer.

## Pipeline

`Ingest → Segment → Score → Compose → Render → Review → Deliver`

See [CLAUDE.md](./CLAUDE.md) for the full stage-by-stage description, repo
layout, and project conventions.

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
