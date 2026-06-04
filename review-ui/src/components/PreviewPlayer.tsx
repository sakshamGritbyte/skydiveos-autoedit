import * as React from "react";

import { cn } from "@/lib/utils";

export interface PreviewPlayerProps {
  /** Stream URL for the rendered preview (range-served by the API). */
  src: string;
  className?: string;
}

/** Coarse load state, driven by the media element's events. */
type Status = "loading" | "ready" | "error";

/**
 * Isolated, seek-robust preview player with a loading/error overlay.
 *
 * Behaviour:
 * - Shows a **spinner** ("Loading video…") until the stream is playable, and
 *   again while buffering after a seek — so the user never stares at a black box
 *   wondering if it's broken.
 * - Shows a friendly **"still processing"** message (with Retry) if the preview
 *   can't be fetched yet (e.g. the render isn't finished / the file 404s).
 * - Resumes playback+audio after a scrub (some browsers pause/drop audio on seek).
 *
 * Wrapped in `React.memo` with a stable `src`, so parent re-renders (timeline
 * edits, note typing, button state) never reload the media node mid-playback.
 */
function PreviewPlayerImpl({ src, className }: PreviewPlayerProps) {
  const videoRef = React.useRef<HTMLVideoElement>(null);
  // Whether the user wants playback — survives the browser's transient scrub-pause
  // so `seeked` can resume rather than leaving a silent gap.
  const playIntentRef = React.useRef(false);
  const [status, setStatus] = React.useState<Status>("loading");

  // New source (switched jobs) → back to loading until it's playable again.
  React.useEffect(() => {
    setStatus("loading");
  }, [src]);

  const markReady = () => setStatus("ready");
  const markBuffering = () => setStatus((s) => (s === "error" ? s : "loading"));

  const handlePlay = () => {
    playIntentRef.current = true;
    markReady();
  };
  const handlePause = () => {
    const v = videoRef.current;
    // A pause emitted *while seeking* is the browser's transient scrub-pause —
    // keep the intent so we resume. A genuine user pause clears it.
    if (v && !v.seeking && !v.ended) {
      playIntentRef.current = false;
    }
  };
  const handleEnded = () => {
    playIntentRef.current = false;
  };
  const handleSeeked = () => {
    const v = videoRef.current;
    if (!v) return;
    // Were we playing before the scrub but the element came back paused? Resume
    // from the new position so audio + video continue with no drop or silence.
    if (playIntentRef.current && v.paused && !v.ended) {
      void v.play().catch(() => undefined);
    }
  };

  const retry = () => {
    setStatus("loading");
    videoRef.current?.load();
  };

  return (
    <div className={cn("relative overflow-hidden rounded-lg bg-black", className)}>
      <video
        ref={videoRef}
        src={src}
        className="h-full w-full object-contain"
        controls
        playsInline
        preload="metadata"
        // Load lifecycle → spinner on/off.
        onLoadStart={markBuffering}
        onLoadedMetadata={markReady}
        onLoadedData={markReady}
        onCanPlay={markReady}
        onPlaying={markReady}
        onWaiting={markBuffering}
        onError={() => setStatus("error")}
        // Playback intent + seek-resume.
        onPlay={handlePlay}
        onPause={handlePause}
        onEnded={handleEnded}
        onSeeked={handleSeeked}
      />

      {status === "loading" && (
        <div
          role="status"
          aria-live="polite"
          className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/50 text-white"
        >
          <span
            aria-hidden
            className="h-8 w-8 animate-spin rounded-full border-2 border-white/30 border-t-white"
          />
          <span className="text-sm font-medium">Loading video…</span>
        </div>
      )}

      {status === "error" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/70 px-6 text-center text-white">
          <span className="text-sm">
            Preview isn’t ready yet — the edit may still be processing.
          </span>
          <button
            type="button"
            onClick={retry}
            className="rounded-md border border-white/40 px-3 py-1 text-xs font-medium hover:bg-white/10"
          >
            Retry
          </button>
        </div>
      )}
    </div>
  );
}

/** Memoised: only a change to `src` (switching jobs) can re-render/reload it. */
export const PreviewPlayer = React.memo(PreviewPlayerImpl);
