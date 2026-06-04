import * as React from "react";

import { Button } from "@/components/ui/button";
import { Timeline } from "@/components/Timeline";
import { PreviewPlayer } from "@/components/PreviewPlayer";
import { ApiError, ReviewApi } from "@/api";
import {
  type Clip,
  type EditDecisionList,
  type JobResponse,
} from "@/types";

export interface EdlReviewScreenProps {
  jobId: string;
  /** The job's saved EDL (from `edl.json` / the compose stage). */
  edl: EditDecisionList;
  /** Booking/job metadata for the header (optional). */
  job?: Pick<JobResponse, "customer_name" | "jump_date" | "music">;
  /** API client; defaults to a same-origin client. Inject a base URL or a mock. */
  api?: ReviewApi;
  /** Notified after any successful action so the host can refresh/navigate. */
  onActed?: (action: "approve" | "reject" | "tweak", job: JobResponse) => void;
}

type Pending = null | "approve" | "reject" | "tweak";

/**
 * Instructor review screen (pipeline stage 6).
 *
 *   1. Native HTML5 preview of the rendered final.mp4.
 *   2. An editable EDL timeline (trim / slow-mo / delete / reorder).
 *   3. Approve & Send · Re-render with my edits · Reject.
 *
 * Approve and Reject act on the original render; Re-render POSTs the *edited*
 * EDL to /jobs/{id}/tweak. Edits stay local until the instructor re-renders, so
 * the gate (CLAUDE.md: "Don't render until the instructor has approved") holds.
 */
export function EdlReviewScreen({
  jobId,
  edl: initialEdl,
  job,
  api: injectedApi,
  onActed,
}: EdlReviewScreenProps) {
  const api = React.useMemo(() => injectedApi ?? new ReviewApi(), [injectedApi]);

  // Stable preview URL: only changes when the job does, so the (memoised) player
  // never reloads while the instructor edits the timeline below it.
  const previewSrc = React.useMemo(() => api.previewUrl(jobId), [api, jobId]);

  // Working copy of the clips the instructor edits; reset if the job/EDL changes.
  const [clips, setClips] = React.useState<Clip[]>(initialEdl.clips);
  React.useEffect(() => {
    setClips(initialEdl.clips);
  }, [initialEdl]);

  const [note, setNote] = React.useState("");
  const [pending, setPending] = React.useState<Pending>(null);
  const [error, setError] = React.useState<string | null>(null);

  const dirty = React.useMemo(
    () => JSON.stringify(clips) !== JSON.stringify(initialEdl.clips),
    [clips, initialEdl.clips],
  );

  const run = async (action: Pending, fn: () => Promise<JobResponse>) => {
    setPending(action);
    setError(null);
    try {
      const updated = await fn();
      if (action) onActed?.(action, updated);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : "request failed",
      );
    } finally {
      setPending(null);
    }
  };

  const handleApprove = () =>
    run("approve", () => api.approve(jobId));

  const handleReject = () => {
    const reason = window.prompt("Reason for rejection (logged as a training signal):");
    if (!reason) return; // cancelled — reject requires a reason (min_length=1)
    return run("reject", () => api.reject(jobId, reason));
  };

  const handleRerender = () => {
    const edl: EditDecisionList = { ...initialEdl, clips };
    return run("tweak", () => api.tweak(jobId, edl, note.trim() || null));
  };

  const busy = pending !== null;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4">
      <header className="flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold">Review edit</h1>
          {job && (
            <p className="text-sm text-muted-foreground">
              {job.customer_name}
              {job.jump_date ? ` · ${job.jump_date}` : ""}
              {job.music ? ` · ♫ ${job.music}` : ""}
            </p>
          )}
        </div>
        <code className="text-xs text-muted-foreground">{jobId}</code>
      </header>

      {/* 1. Rendered preview — isolated, seek-robust native player. Keyed by src
          so switching jobs gives a fresh element; otherwise never remounts. */}
      <PreviewPlayer
        key={previewSrc}
        src={previewSrc}
        className="aspect-video w-full rounded-lg bg-black"
      />

      {/* 2. Editable EDL timeline. */}
      <Timeline edl={{ ...initialEdl, clips }} onChange={setClips} />

      {/* Optional note persisted alongside the tweaked EDL (training signal). */}
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">Note for this edit (optional)</span>
        <input
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. Trimmed the canopy beat, slowed the exit"
          className="rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </label>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {/* 3. The three review actions. */}
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={handleApprove} disabled={busy || dirty} title={dirty ? "Re-render your edits before approving" : undefined}>
          {pending === "approve" ? "Sending…" : "Approve & Send"}
        </Button>
        <Button variant="secondary" onClick={handleRerender} disabled={busy || !dirty}>
          {pending === "tweak" ? "Re-rendering…" : "Re-render with my edits"}
        </Button>
        <Button variant="destructive" onClick={handleReject} disabled={busy}>
          {pending === "reject" ? "Rejecting…" : "Reject"}
        </Button>
        {dirty && (
          <span className="text-xs text-muted-foreground">
            Unsaved edits — re-render to apply them.
          </span>
        )}
      </div>
    </div>
  );
}

export default EdlReviewScreen;
