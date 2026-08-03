# Hand-off: lock the auto-edit API down to `/j/*` (audit Phase 0.5)

**Owner: human (infra).** Nothing in this file was applied by the audit — it is the
exact change to make on the box/proxy, plus how to verify it.

## Why (verified against production, 2026-08-03)

`ai.ultimatedzm.com` → `15.223.191.11` serves the auto-edit FastAPI service to the
open internet, and it has **no authentication**:

| Probe (no headers at all) | Result |
|---|---|
| `GET /docs` | **200** — the whole API surface, documented |
| `GET /jobs` | **200** — every job's `customer_name`, `customer_email`, `delivery_links` |
| `GET /jobs/<id>/deliverables` | **200** |
| `GET /jobs/<id>/deliverables/full_video` (Range: 0-0) | **206** — a customer's finished video |

Cause: the service reads identity from self-asserted `X-Instructor-Id` / `X-Role`
headers, and `ENFORCE_INSTRUCTOR_AUTH` is unset, so *every* caller resolves to an
admin. It is public because the production React build points browsers straight at it
(`REACT_APP_AI_BACKEND_URL=https://ai.ultimatedzm.com`, `deploy-prod.yml`).

That browser dependency is now **removed**: the frontend calls
`/api/media/ai-jobs/...` on the SkydiveOS backend, which proxies to the pipeline
server-to-server. So the pipeline only needs to accept traffic from:

1. **the public**, for the customer gallery — `/j/*` only;
2. **the SkydiveOS backend** (`3.99.127.109`), for everything else;
3. **the dropzone ingest hosts** (Mac + Windows PC), which POST camera footage.

## 1. Deploy the frontend + backend change first

Both live in `skydiving-os`. Until the new build is live, cutting the pipeline off
from browsers breaks the staff Media UI. Order: **deploy SkydiveOS → then lock the
pipeline down.**

## 2. Set the service token on both sides (belt to the network's braces)

The gate is off until the token is set, and both sides read it from env:

```bash
# EC2 autoedit box: /…/skydiveos-autoedit/.env
AUTO_EDIT_API_KEY=<same-long-random-secret>

# SkydiveOS backend container env
AI_BACKEND_API_KEY=<same-long-random-secret>   # already sent as `Authorization: Bearer`
```

Then `docker compose up -d api worker` (autoedit) and restart the SkydiveOS backend.
This alone closes the anonymous-read hole even if the proxy rules slip, because it is
enforced in the app, not the network.

## 3. Proxy rules — only `/j/*` is public

`ai.ultimatedzm.com` terminates TLS somewhere outside both repos (the repo's
`Caddyfile` is an unused template). Apply the equivalent of this to whatever fronts
it:

### If it's nginx

```nginx
server {
    listen 443 ssl;
    server_name ai.ultimatedzm.com;

    # 1. The customer gallery — the ONLY public surface. Short-code authed,
    #    must stay open (customers have no account): the page, its media and
    #    its photos.
    location /j/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;          # stream video, don't buffer it
        proxy_read_timeout 300s;
    }

    # 2. Everything else — staff/admin + the ingest hooks. Callers by IP.
    #    NOTE: this covers the MUTATING routes too (POST /jobs, /upload,
    #    /approve, /reject, /tweak, /unlock, /cameras/*, DELETE /cameras/*),
    #    which are as reachable as the reads today.
    location / {
        allow 3.99.127.109;           # SkydiveOS backend (api.ultimatedzm.com)
        allow <dropzone-mac-public-ip>;      # ingest: camera pulls → S3 → job
        allow <dropzone-windows-public-ip>;  # second location (USB ingest)
        deny  all;

        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 0;       # multi-GB master uploads
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

### If it's Caddy

```caddyfile
ai.ultimatedzm.com {
    @public path /j/*
    handle @public {
        reverse_proxy 127.0.0.1:8000
    }

    @allowed remote_ip 3.99.127.109 <dropzone-mac-ip> <dropzone-windows-ip>
    handle @allowed {
        reverse_proxy 127.0.0.1:8000
    }

    handle {
        respond "Not found" 404
    }
}
```

### Also: close the direct port

The API is published on the host as `8000:8000` (`docker-compose.yml`) and
`DEPLOY.md §5.2` prescribes a security-group rule `8000 → 0.0.0.0/0`, so the proxy
can be bypassed entirely today.

* **Security group:** delete the `0.0.0.0/0` rule for **8000**. Keep 443 (and 80 for
  ACME) open; add 8000 only for the proxy host itself if it isn't on the same box.
* If the proxy runs on the same box, bind the container to loopback instead:
  `ports: ["127.0.0.1:8000:8000"]`.

## 4. Verify (from a machine that is NOT the SkydiveOS box)

```bash
# Public gallery still works (use a real code from a delivered job):
curl -s -o /dev/null -w '%{http_code}\n' https://ai.ultimatedzm.com/j/<code>        # want 200

# The admin surface is gone:
for p in /docs /openapi.json /jobs /cameras; do
  printf '%s -> ' "$p"
  curl -s -o /dev/null -w '%{http_code}\n' "https://ai.ultimatedzm.com$p"           # want 403/404
done

# The direct port is gone:
curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://15.223.191.11:8000/docs        # want no response

# With the token, from the SkydiveOS box, staff routes still work:
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $AI_BACKEND_API_KEY" \
  https://ai.ultimatedzm.com/jobs                                                   # want 200
```

Then smoke-test the staff Media UI (upload → poll → photos → download) — it now
goes through `/api/media/ai-jobs/...`, so a 401/404 there means the frontend build or
the backend env didn't ship.

## 5. Do NOT deploy the REV03 paywall branch until Phase 2 has landed here

The entitlement/paywall work (preview rendering, `/j/{code}` gallery, `/unlock`) is
**not** in production today — the deployed build has 17 routes, no `/unlock`, and no
`entitlement` field, and there are zero `preview_only` jobs. Deploying it onto an open
API would arm both paywall holes at once (a free-unlock endpoint, and presigned
clean-master links). Sequence: SkydiveOS proxy change → token → lockdown → *then* the
REV03 branch.
