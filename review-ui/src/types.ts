/**
 * Wire types for the instructor review screen.
 *
 * These mirror the Python contracts exactly so the EDL we POST back to
 * `/jobs/{id}/tweak` round-trips through the FastAPI/Pydantic validation
 * boundary unchanged:
 *
 *   - EDL shape:  edl/schema.py  (EditDecisionList, Clip, Transition)
 *   - Job shape:  api/schemas.py (JobResponse) + api/jobs.py (JobStatus)
 *   - Tweak body: api/schemas.py (TweakRequest)
 *
 * Conventions inherited from the pipeline (CLAUDE.md):
 *   - All timestamps are SECONDS (float), never frames.
 *   - `speed_multiplier` is the playback rate: 1.0 real-time, 0.4 = the slow-mo
 *     we use for exits / peak-smile highlights, >1.0 sped up. A clip's output
 *     (on-screen) length is `(src_end - src_start) / speed_multiplier`.
 */

/** The cut/transition at a clip boundary — must match edl.schema.Transition. */
export type Transition = "cut" | "fade" | "crossfade" | "flash";

/** One source window placed on the output timeline — mirrors edl.schema.Clip. */
export interface Clip {
  /** Source in-point (seconds), >= 0. */
  src_start: number;
  /** Source out-point (seconds), exclusive, > src_start. */
  src_end: number;
  /** Playback rate: 1.0 normal, 0.4 slow-mo, >1.0 sped up. */
  speed_multiplier: number;
  transition_in: Transition | null;
  transition_out: Transition | null;
}

/** A complete, ordered edit — mirrors edl.schema.EditDecisionList. */
export interface EditDecisionList {
  /** EDL schema version; sent back untouched so Render records what produced it. */
  version: string;
  /** Played in list order; must be non-empty. */
  clips: Clip[];
  /** Music track id/name for Render. */
  music: string | null;
  /** Editor rationale (logged, not rendered). */
  notes: string | null;
}

/** Job lifecycle — mirrors api.jobs.JobStatus. */
export type JobStatus =
  | "queued"
  | "processing"
  | "ready_for_review"
  | "approved"
  | "delivered"
  | "rejected"
  | "failed";

/** Public job view — mirrors api.schemas.JobResponse. */
export interface JobResponse {
  job_id: string;
  status: JobStatus;
  customer_name: string;
  jump_date: string | null;
  camera_id: string | null;
  music: string | null;
  target_duration: number;
  reject_reason: string | null;
  error: string | null;
  created_at: number;
  updated_at: number;
}

/** Body for POST /jobs/{id}/tweak — mirrors api.schemas.TweakRequest. */
export interface TweakRequest {
  edl: EditDecisionList;
  note?: string | null;
}

/** The documented slow-mo rate used for exits and peak-smile highlights. */
export const SLOWMO_SPEED = 0.4;
/** Real-time playback. */
export const NORMAL_SPEED = 1.0;

/** Source-window length (real seconds of footage consumed). */
export function sourceDuration(clip: Clip): number {
  return clip.src_end - clip.src_start;
}

/** On-screen length after the speed ramp (seconds). */
export function outputDuration(clip: Clip): number {
  return sourceDuration(clip) / clip.speed_multiplier;
}

/** Total runtime of the footage edit (excludes intro/outro), in seconds. */
export function totalOutputDuration(edl: EditDecisionList): number {
  return edl.clips.reduce((sum, c) => sum + outputDuration(c), 0);
}

/** True when a clip is running at (or below) the slow-mo rate. */
export function isSlowMo(clip: Clip): boolean {
  return clip.speed_multiplier < NORMAL_SPEED;
}
