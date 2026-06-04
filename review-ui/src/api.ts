/**
 * Thin client for the review endpoints the instructor screen drives.
 *
 * Matches api/app.py exactly:
 *   GET  /jobs/{id}          -> JobResponse
 *   GET  /jobs/{id}/edl      -> EditDecisionList (the timeline to review)
 *   GET  /jobs/{id}/preview  -> video/mp4 (we just point <video src> at this URL)
 *   POST /jobs/{id}/approve  -> JobResponse   (deliver)
 *   POST /jobs/{id}/reject   -> JobResponse   ({ reason })
 *   POST /jobs/{id}/tweak    -> JobResponse   ({ edl, note })  (re-render)
 */

import type { EditDecisionList, JobResponse, TweakRequest } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = (await res.json())?.detail;
    } catch {
      detail = await res.text().catch(() => undefined);
    }
    const msg = typeof detail === "string" ? detail : `request failed (${res.status})`;
    throw new ApiError(msg, res.status, detail);
  }
  return (await res.json()) as T;
}

/** Client bound to a single API origin (defaults to same-origin). */
export class ReviewApi {
  constructor(private readonly baseUrl = "") {}

  /** Absolute URL for the streamable preview — feed straight to `<video src>`. */
  previewUrl(jobId: string): string {
    return `${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/preview`;
  }

  getJob(jobId: string, signal?: AbortSignal): Promise<JobResponse> {
    return fetch(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}`, {
      signal,
    }).then(asJson<JobResponse>);
  }

  /** Load the job's persisted EDL — the timeline the instructor reviews. */
  getEdl(jobId: string, signal?: AbortSignal): Promise<EditDecisionList> {
    return fetch(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/edl`, {
      signal,
    }).then(asJson<EditDecisionList>);
  }

  /** Approve & deliver. */
  approve(jobId: string): Promise<JobResponse> {
    return fetch(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/approve`, {
      method: "POST",
    }).then(asJson<JobResponse>);
  }

  /** Reject with a reason; the job is re-queued for a fresh edit. */
  reject(jobId: string, reason: string): Promise<JobResponse> {
    return fetch(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    }).then(asJson<JobResponse>);
  }

  /** Replace the EDL with the instructor's edits and re-render. */
  tweak(jobId: string, edl: EditDecisionList, note?: string | null): Promise<JobResponse> {
    const body: TweakRequest = { edl, note: note ?? null };
    return fetch(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/tweak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(asJson<JobResponse>);
  }
}
