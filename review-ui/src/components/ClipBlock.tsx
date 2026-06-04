import * as React from "react";

import { cn } from "@/lib/utils";
import {
  type Clip,
  isSlowMo,
  outputDuration,
  sourceDuration,
} from "@/types";

/** Smallest source window we let an instructor trim a clip down to (seconds). */
const MIN_SOURCE_DURATION = 0.5;

function fmt(seconds: number): string {
  return `${seconds.toFixed(1)}s`;
}

export interface ClipBlockProps {
  clip: Clip;
  index: number;
  /** Horizontal scale: how many pixels represent one second of source footage. */
  pxPerSecond: number;
  /** True while this block is the active drag source in a reorder. */
  isDragging: boolean;
  /** True while another block is hovering to drop in front of this one. */
  isDropTarget: boolean;

  /** Trim: report a new source window (already clamped) for this clip. */
  onTrim: (index: number, next: { src_start: number; src_end: number }) => void;
  /** Click body: toggle the slow-mo speed ramp on this clip. */
  onToggleSlowMo: (index: number) => void;
  /** Remove this clip from the edit. */
  onDelete: (index: number) => void;

  // Native drag-and-drop reorder (driven from the grip handle only).
  onReorderStart: (index: number) => void;
  onReorderOver: (index: number) => void;
  onReorderDrop: (index: number) => void;
  onReorderEnd: () => void;
}

type TrimEdge = "start" | "end";

/**
 * One clip on the timeline. Width is proportional to the *source* window so
 * edge-trimming maps 1:1 to pixels; the speed ramp is shown as a badge and the
 * resulting on-screen (output) duration is labelled separately.
 *
 * Three gestures, kept on separate hit targets so they never fight:
 *   - left/right edge handles  -> pointer-drag to trim src_start / src_end
 *   - the ⠿ grip               -> native drag to reorder
 *   - the body                 -> click to toggle slow-mo
 */
export function ClipBlock(props: ClipBlockProps) {
  const {
    clip,
    index,
    pxPerSecond,
    isDragging,
    isDropTarget,
    onTrim,
    onToggleSlowMo,
    onDelete,
    onReorderStart,
    onReorderOver,
    onReorderDrop,
    onReorderEnd,
  } = props;

  // Trim gesture state lives in a ref so pointermove doesn't re-render per pixel.
  const trim = React.useRef<{
    edge: TrimEdge;
    startX: number;
    origStart: number;
    origEnd: number;
  } | null>(null);

  const slow = isSlowMo(clip);
  const widthPx = Math.max(sourceDuration(clip) * pxPerSecond, 56);

  const beginTrim = (edge: TrimEdge) => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    (e.target as Element).setPointerCapture(e.pointerId);
    trim.current = {
      edge,
      startX: e.clientX,
      origStart: clip.src_start,
      origEnd: clip.src_end,
    };
  };

  const moveTrim = (e: React.PointerEvent) => {
    const t = trim.current;
    if (!t) return;
    const deltaSec = (e.clientX - t.startX) / pxPerSecond;
    if (t.edge === "start") {
      // Pull the in-point; never past 0 or within MIN of the out-point.
      const next = Math.min(
        Math.max(t.origStart + deltaSec, 0),
        t.origEnd - MIN_SOURCE_DURATION,
      );
      onTrim(index, { src_start: next, src_end: t.origEnd });
    } else {
      // Push the out-point; never within MIN of the in-point.
      const next = Math.max(t.origEnd + deltaSec, t.origStart + MIN_SOURCE_DURATION);
      onTrim(index, { src_start: t.origStart, src_end: next });
    }
  };

  const endTrim = (e: React.PointerEvent) => {
    if (!trim.current) return;
    trim.current = null;
    try {
      (e.target as Element).releasePointerCapture(e.pointerId);
    } catch {
      /* capture may already be gone */
    }
  };

  return (
    <div
      className={cn(
        "relative flex h-24 shrink-0 select-none flex-col justify-between rounded-md border text-xs shadow-sm transition-colors",
        slow
          ? "border-amber-400/60 bg-amber-50 dark:bg-amber-950/40"
          : "border-border bg-card",
        isDropTarget && "ring-2 ring-primary ring-offset-1",
        isDragging && "opacity-40",
      )}
      style={{ width: widthPx }}
      // Drop targeting: any block can be hovered/dropped onto during reorder.
      onDragOver={(e) => {
        e.preventDefault();
        onReorderOver(index);
      }}
      onDrop={(e) => {
        e.preventDefault();
        onReorderDrop(index);
      }}
      data-testid={`clip-${index}`}
    >
      {/* Left trim handle */}
      <div
        role="slider"
        aria-label="Trim clip start"
        aria-valuenow={Math.round(clip.src_start * 10) / 10}
        onPointerDown={beginTrim("start")}
        onPointerMove={moveTrim}
        onPointerUp={endTrim}
        onPointerCancel={endTrim}
        className="absolute inset-y-0 left-0 z-10 w-2 cursor-ew-resize rounded-l-md bg-border/70 hover:bg-primary"
        data-testid={`clip-${index}-trim-start`}
      />
      {/* Right trim handle */}
      <div
        role="slider"
        aria-label="Trim clip end"
        aria-valuenow={Math.round(clip.src_end * 10) / 10}
        onPointerDown={beginTrim("end")}
        onPointerMove={moveTrim}
        onPointerUp={endTrim}
        onPointerCancel={endTrim}
        className="absolute inset-y-0 right-0 z-10 w-2 cursor-ew-resize rounded-r-md bg-border/70 hover:bg-primary"
        data-testid={`clip-${index}-trim-end`}
      />

      {/* Header: reorder grip + delete */}
      <div className="flex items-center justify-between px-3 pt-1.5">
        <span
          draggable
          onDragStart={() => onReorderStart(index)}
          onDragEnd={onReorderEnd}
          className="cursor-grab text-muted-foreground active:cursor-grabbing"
          aria-label="Drag to reorder clip"
          title="Drag to reorder"
          data-testid={`clip-${index}-grip`}
        >
          ⠿
        </span>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDelete(index);
          }}
          className="rounded px-1 text-muted-foreground hover:text-destructive"
          aria-label="Delete clip"
          title="Delete clip"
          data-testid={`clip-${index}-delete`}
        >
          ✕
        </button>
      </div>

      {/* Body: click toggles slow-mo. Shows src in-point, durations, speed. */}
      <button
        type="button"
        onClick={() => onToggleSlowMo(index)}
        className="flex flex-1 cursor-pointer flex-col items-start justify-center gap-0.5 px-3 pb-2 text-left"
        title={slow ? "Click for real-time" : "Click for slow-mo"}
        data-testid={`clip-${index}-body`}
      >
        <span className="font-mono text-[11px] text-muted-foreground">
          @ {fmt(clip.src_start)}
        </span>
        <span className="font-medium">{fmt(sourceDuration(clip))} src</span>
        <span className="flex items-center gap-1">
          <span
            className={cn(
              "rounded px-1 py-0.5 font-mono text-[10px]",
              slow
                ? "bg-amber-200 text-amber-900 dark:bg-amber-800 dark:text-amber-50"
                : "bg-muted text-muted-foreground",
            )}
          >
            {clip.speed_multiplier}×{slow ? " slo-mo" : ""}
          </span>
          <span className="text-[10px] text-muted-foreground">
            → {fmt(outputDuration(clip))}
          </span>
        </span>
      </button>
    </div>
  );
}
