import * as React from "react";

import { type Clip, type EditDecisionList, totalOutputDuration } from "@/types";
import { ClipBlock } from "./ClipBlock";

export interface TimelineProps {
  edl: EditDecisionList;
  /** Pixels per second of source footage (block width scale). */
  pxPerSecond?: number;
  /** Emit the full, reordered/edited clip list whenever anything changes. */
  onChange: (clips: Clip[]) => void;
}

/**
 * The EDL timeline: clips laid left-to-right in play order, each a draggable,
 * trimmable block. Owns only the transient reorder drag state; the clip data
 * itself is lifted to the parent (EdlReviewScreen) so Re-render always POSTs the
 * exact array shown here.
 */
export function Timeline({ edl, pxPerSecond = 14, onChange }: TimelineProps) {
  const { clips } = edl;
  const [dragIndex, setDragIndex] = React.useState<number | null>(null);
  const [overIndex, setOverIndex] = React.useState<number | null>(null);

  const replace = (index: number, patch: Partial<Clip>) => {
    onChange(clips.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  };

  const toggleSlowMo = (index: number) => {
    const c = clips[index];
    // 0.4 is the documented slow-mo rate (CLAUDE.md); toggle back to real-time.
    replace(index, { speed_multiplier: c.speed_multiplier < 1.0 ? 1.0 : 0.4 });
  };

  const remove = (index: number) => {
    // Keep the EDL non-empty — Render rejects a clipless edit (min_length=1).
    if (clips.length <= 1) return;
    onChange(clips.filter((_, i) => i !== index));
  };

  const drop = (target: number) => {
    if (dragIndex === null || dragIndex === target) {
      setDragIndex(null);
      setOverIndex(null);
      return;
    }
    const next = clips.slice();
    const [moved] = next.splice(dragIndex, 1);
    next.splice(target, 0, moved);
    onChange(next);
    setDragIndex(null);
    setOverIndex(null);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {clips.length} clip{clips.length === 1 ? "" : "s"} · footage runtime{" "}
          <span className="font-medium text-foreground">
            {totalOutputDuration(edl).toFixed(1)}s
          </span>
        </span>
        <span className="text-xs">
          drag edges to trim · click a clip for slow-mo · ⠿ to reorder
        </span>
      </div>

      <div
        className="flex items-stretch gap-1.5 overflow-x-auto rounded-lg border bg-muted/30 p-3"
        role="list"
        aria-label="Edit timeline"
      >
        {clips.map((clip, i) => (
          <ClipBlock
            key={i}
            clip={clip}
            index={i}
            pxPerSecond={pxPerSecond}
            isDragging={dragIndex === i}
            isDropTarget={overIndex === i && dragIndex !== null && dragIndex !== i}
            onTrim={(idx, win) => replace(idx, win)}
            onToggleSlowMo={toggleSlowMo}
            onDelete={remove}
            onReorderStart={setDragIndex}
            onReorderOver={setOverIndex}
            onReorderDrop={drop}
            onReorderEnd={() => {
              setDragIndex(null);
              setOverIndex(null);
            }}
          />
        ))}
      </div>
    </div>
  );
}
