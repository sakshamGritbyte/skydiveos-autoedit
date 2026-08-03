# Was it exploited? — access review for the open `/jobs` + `/deliverables` window

Purpose: decide **vulnerability vs breach**. The auto-edit API was internet-facing with
no authentication (verified 2026-08-03), so anonymous `GET /jobs` returned every
customer's name, email and delivery links and `GET /jobs/{id}/deliverables/{name}`
streamed finished videos. That is a confirmed *exposure*. Whether anyone other than us
used it is what these queries answer.

Run these **before** the proxy lockdown restarts anything — a `docker compose up
--build` does not delete logs, but don't take the chance.

---

## Read this first: which log can attribute a request

| Log | Tells you | Does NOT tell you |
|---|---|---|
| **Fronting proxy** (nginx/Caddy on the box that terminates TLS for `ai.ultimatedzm.com`) | The **real client IP**, path, status, bytes, user-agent | — |
| **API container** (`docker compose logs api`) | Path, status, timing — a complete request inventory | **Who.** uvicorn runs *without* `--proxy-headers`, so its access line shows the *proxy's* address (a docker-gateway/loopback IP), not the caller's |

So: the container log establishes **what was requested and how much**; only the proxy
log establishes **by whom**. Get both. If the proxy log is gone or was never enabled,
say so explicitly in the finding — absence of the attributing log is not absence of a
breach.

> Fix for next time (one line, during the lockdown window): run the API as
> `uvicorn api.app:app --host 0.0.0.0 --port 8000 --proxy-headers
> --forwarded-allow-ips='<proxy-ip>'` so `X-Forwarded-For` becomes the logged client.

## Known-good callers (anything else on a staff route is a third party)

| Who | Address | Legitimately hits |
|---|---|---|
| SkydiveOS backend | `3.99.127.109` | everything (server-to-server) |
| Dropzone Mac (ingest) | *fill in* | `/jobs`, `/jobs/*/upload`, `/cameras*` |
| Dropzone Windows PC (ingest) | *fill in* | same |
| Staff browsers | any office/home IP | **`/jobs*` until the proxy change ships** — this is the noisy set, because the frontend called the pipeline directly |
| Customers | any | `/j/*` only — expected and fine |

The staff-browser row is why "unknown IP" alone isn't proof of a breach: until now, every
staff member's browser talked straight to this API. Distinguish by **behaviour**, not
just address (see the triage rules below).

---

## 1. API container — the request inventory

```bash
cd /…/skydiveos-autoedit

# How far back does the log actually go? (No `logging:` block in docker-compose.yml →
# default json-file driver; check the file size/first line before trusting a "clean" result.)
docker inspect --format='{{.LogPath}}' $(docker compose ps -q api) | xargs sudo ls -lh
docker compose logs api --no-color | head -1

# Every staff/PII-bearing request that SUCCEEDED, newest last.
docker compose logs api --no-color --timestamps \
  | grep -E '"(GET|POST|DELETE) /(jobs|cameras)' \
  | grep -E '" (200|206|201)' \
  > /tmp/ae-staff-requests.txt
wc -l /tmp/ae-staff-requests.txt

# The two highest-signal shapes:
#  a) job enumeration — one call returns EVERY customer's name/email/links
grep -E '"GET /jobs(\?|")' /tmp/ae-staff-requests.txt | wc -l
#  b) customer video streamed
grep -E '"GET /jobs/[^/]+/deliverables/' /tmp/ae-staff-requests.txt | wc -l
#  c) photo reads
grep -E '"GET /jobs/[^/]+/photos/' /tmp/ae-staff-requests.txt | wc -l

# Requests per day, to spot a burst that doesn't look like staff working a shift:
awk '{print substr($1,1,10)}' /tmp/ae-staff-requests.txt | sort | uniq -c

# Mutations are the loudest possible signal — nobody should see unexplained ones:
docker compose logs api --no-color --timestamps \
  | grep -E '"(POST|DELETE) /(jobs/[^/]+/(approve|reject|tweak|unlock)|cameras)' 
```

Note: `/docs` and `/openapi.json` hits from an unknown IP are the classic
reconnaissance tell — cheap to check, worth reporting:

```bash
docker compose logs api --no-color --timestamps | grep -E '"GET /(docs|openapi.json)'
```

## 2. Proxy — attribution

Adjust paths to whatever fronts `ai.ultimatedzm.com` (not in either repo — check
`/etc/nginx/`, a `caddy` container, or an ALB).

**nginx:**
```bash
sudo zgrep -hE ' "(GET|POST|DELETE) /(jobs|cameras|docs|openapi)' /var/log/nginx/access.log* \
  | awk '{print $1}' | sort | uniq -c | sort -rn | head -40
```

**Caddy (JSON lines):**
```bash
sudo jq -r 'select(.request.uri | test("^/(jobs|cameras|docs|openapi)"))
            | [.request.remote_ip, .request.uri, .status] | @tsv' \
  /var/log/caddy/access.log | sort | uniq -c | sort -rn | head -40
```

**AWS ALB (if that's the front):**
```sql
-- Athena over the ALB access logs
SELECT client_ip, count(*) AS hits, min(time) AS first_seen, max(time) AS last_seen
FROM alb_logs
WHERE request_url LIKE '%/jobs%' OR request_url LIKE '%/cameras%'
GROUP BY client_ip ORDER BY hits DESC;
```

Then subtract the known-good table above. **Every remaining IP on a `/jobs*` or
`/cameras*` path is a third party that could read customer PII and video.**

## 3. Triage: vulnerability or breach?

| Signal | Reading |
|---|---|
| Unknown IPs only on `/j/*` | Normal — that's customers. **Vulnerability only.** |
| No unknown IPs on `/jobs*`, and the proxy log covers the whole exposure window | **Vulnerability, not breach.** Say the window explicitly ("proxy log covers 2026-06-22 → today"). |
| Unknown IP hitting `/docs` then `/jobs` | Reconnaissance → enumeration. **Treat as breach**; the `/jobs` response contains every customer's email. |
| Sequential `/jobs/<id>/deliverables/<name>` across many job ids from one IP | Bulk video exfiltration. **Breach.** |
| Unexplained `POST …/approve|reject|tweak` or `DELETE /cameras/*` | Tampering. **Breach**, and check whether any job's state or camera registry was altered. |
| Proxy log missing or shorter than the exposure window | **Undetermined** — report as "cannot exclude", never as clean. |

## 4. If it reads as a breach

1. Scope it: which job ids appear → which customers (names/emails/videos) were readable.
   `jobs/<id>/job.json` maps id → customer.
2. Rotate anything that travelled: the presigned delivery links in `Job.delivery_links`
   are 7-day URLs — re-deliver affected jobs to invalidate the exposure window's usefulness.
3. Note that **gallery short codes** may have been enumerable via `/jobs` responses
   (`gallery_token` is deliberately not in the API response, but delivery links were);
   a rotated token means a new customer link, so weigh that against customer confusion.
4. Québec's Law 25 / PIPEDA breach-notification duties attach to a confidentiality
   incident involving customer names + emails — get that in front of whoever owns
   compliance for the dropzone the same day. This file is not legal advice; the trigger
   is a real-risk-of-serious-injury assessment, so it needs a human decision with the
   scope from step 1 in hand.
5. Preserve evidence first: copy the container log file and the proxy logs off the box
   (`docker inspect --format='{{.LogPath}}'` → `sudo cp`) before any rebuild.
