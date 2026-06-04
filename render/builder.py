"""Pure construction of the FFmpeg ``filter_complex`` graph for one edit.

This module turns an :class:`~edl.schema.EditDecisionList` (plus the resolved
intro/outro/music/caption assets) into the exact ``-filter_complex`` string and
the ordered input list FFmpeg needs — and *nothing else*. It runs no subprocess,
touches no disk, and has no FFmpeg dependency, so the graph can be unit-tested by
string assertion the same way /analysis unit-tests its scoring math.

The graph it builds, stage for stage (matching the Render spec):

1. **Trim + speed-ramp + concat the EDL clips.** Each clip becomes a
   ``trim,setpts=(PTS-STARTPTS)/speed`` chain (video) — ``setpts`` divides by the
   playback rate, so ``0.4`` stretches a clip to 2.5x its length (slow-mo), matching
   :attr:`Clip.output_duration`. All clips are normalised to a single
   resolution/fps/SAR so ``concat`` can join them.
2. **Intro / outro cards** are normalised and concatenated on the front and back of
   the footage. The intro additionally gets the customer caption overlaid (see
   ``caption_path``).
3. **Music + ducking.** The backing track is looped/trimmed to the full timeline
   and, *when the source has its own audio*, side-chain compressed so it ducks
   under the original ambience (the exit scream, the instructor's voice) via
   ``sidechaincompress`` keyed by that ambience; the two are then ``amix``-ed. With
   a silent source (the common GoPro case — the proxy/master often carry no audio
   track) there is nothing to duck against, so the music plays straight.

Everything downstream of here — probing durations, generating the caption PNG,
synthesising default cards, running FFmpeg — lives in :mod:`render.render`.

All timestamps are seconds (float) on the source timeline, per project convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from edl.schema import Clip, EditDecisionList, Transition

# Output target (CLAUDE.md Render spec: 1080p, h264, 30fps).
OUT_WIDTH = 1920
OUT_HEIGHT = 1080
OUT_FPS = 30

# Audio working format for every branch, so concat/amix see uniform streams.
AUDIO_RATE = 44100
AUDIO_LAYOUT = "stereo"

# Default backing-track level before ducking, and the side-chain compressor's
# response. Tuned so loud original audio noticeably pulls the music down without
# pumping. Overridable per-call for A/B tests.
DEFAULT_MUSIC_GAIN = 0.7
_DUCK = "threshold=0.03:ratio=8:attack=20:release=300"

# Short fade applied at a clip boundary that carries a transition. Real crossfades
# (overlapping clips) are out of scope for the first render pass; a fade in/out on
# the clip edge reads cleanly and keeps the graph a simple concat. ``flash`` fades
# through white, the others through black.
_FADE_SECONDS = 0.4
_FADE_COLOR = {
    Transition.fade: "black",
    Transition.crossfade: "black",
    Transition.flash: "white",
}


@dataclass(frozen=True)
class InputSpec:
    """One FFmpeg input: its path and any flags that must precede ``-i``.

    ``pre_args`` carries per-input decode flags — e.g. ``-stream_loop -1`` to loop
    the music bed, or ``-loop 1 -t <dur>`` to hold the caption still image for the
    intro's length.
    """

    path: str
    pre_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilterGraph:
    """The built graph: ordered inputs, the ``-filter_complex`` body, and maps.

    ``video_label`` / ``audio_label`` are the pad names to ``-map``. ``audio_label``
    is ``None`` when the edit has no audio at all (silent source and no music), in
    which case the render is video-only.
    """

    inputs: list[InputSpec]
    filter_complex: str
    video_label: str
    audio_label: str | None

    def input_args(self) -> list[str]:
        """Flatten the inputs into FFmpeg argv: ``[*pre, -i, path, ...]``."""
        args: list[str] = []
        for spec in self.inputs:
            args.extend(spec.pre_args)
            args.extend(["-i", spec.path])
        return args


@dataclass
class _GraphState:
    """Accumulator threaded through the builder: chains + the next input index."""

    chains: list[str] = field(default_factory=list)
    inputs: list[InputSpec] = field(default_factory=list)

    def add_input(self, path: str, pre_args: tuple[str, ...] = ()) -> int:
        """Register an input and return its FFmpeg index."""
        idx = len(self.inputs)
        self.inputs.append(InputSpec(path, pre_args))
        return idx


def _num(value: float) -> str:
    """Format a time/seconds value without scientific notation or trailing zeros."""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _norm_chain(width: int, height: int, fps: int) -> str:
    """Filters that force any source into the uniform output geometry.

    Letterbox/pillarbox into ``width x height`` (never distort), pin SAR to 1, and
    resample to ``fps`` in ``yuv420p`` — the prerequisites for ``concat`` to accept
    clips, cards, and ramps as one homogeneous stream.
    """
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={fps},format=yuv420p"
    )


def atempo_chain(speed: float) -> list[float]:
    """Decompose a playback-rate change into ``atempo`` factors within [0.5, 2.0].

    ``atempo`` only accepts a single factor in [0.5, 2.0]; a 0.4x slow-mo or a 3x
    speed-up must be expressed as a product of in-range factors (0.4 -> 0.5 * 0.8).
    Returns the factor list (empty for a no-op 1.0, so the caller emits no filter).
    """
    if speed <= 0:
        raise ValueError(f"speed must be positive, got {speed}")
    factors: list[float] = []
    remaining = speed
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    # Drop any ~1.0 factors — atempo=1 is a pointless identity filter.
    return [f for f in factors if abs(f - 1.0) > 1e-6]


def _clip_video_chain(src: int, clip: Clip, norm: str, out_label: str) -> str:
    """``trim -> speed ramp -> normalise -> (optional fades)`` for one clip's video."""
    parts = [
        f"[{src}:v]trim={_num(clip.src_start)}:{_num(clip.src_end)}",
        f"setpts=(PTS-STARTPTS)/{clip.speed_multiplier:.4f}",
        norm,
    ]
    out_dur = clip.output_duration
    fade = min(_FADE_SECONDS, out_dur / 2)
    if clip.transition_in is not None and clip.transition_in is not Transition.cut:
        color = _FADE_COLOR.get(clip.transition_in, "black")
        parts.append(f"fade=t=in:st=0:d={_num(fade)}:color={color}")
    if clip.transition_out is not None and clip.transition_out is not Transition.cut:
        color = _FADE_COLOR.get(clip.transition_out, "black")
        parts.append(f"fade=t=out:st={_num(out_dur - fade)}:d={_num(fade)}:color={color}")
    return ",".join(parts) + f"[{out_label}]"


def _clip_audio_chain(src: int, clip: Clip, out_label: str) -> str:
    """``atrim -> reset PTS -> tempo ramp -> resample`` for one clip's audio.

    The ambient audio is speed-ramped in lock-step with the video so it stays in
    sync; ``atempo`` is pitch-preserving, so slow-mo lowers tempo without a chipmunk
    pitch shift.
    """
    parts = [
        f"[{src}:a]atrim={_num(clip.src_start)}:{_num(clip.src_end)}",
        "asetpts=PTS-STARTPTS",
    ]
    for factor in atempo_chain(clip.speed_multiplier):
        parts.append(f"atempo={factor:.4f}")
    parts.append(f"aresample={AUDIO_RATE}")
    return ",".join(parts) + f"[{out_label}]"


def build_filtergraph(
    edl: EditDecisionList,
    source_path: str,
    *,
    has_audio: bool,
    intro_path: str | None = None,
    intro_duration: float = 0.0,
    outro_path: str | None = None,
    outro_duration: float = 0.0,
    music_path: str | None = None,
    caption_path: str | None = None,
    width: int = OUT_WIDTH,
    height: int = OUT_HEIGHT,
    fps: int = OUT_FPS,
    music_gain: float = DEFAULT_MUSIC_GAIN,
) -> FilterGraph:
    """Build the ``filter_complex`` graph and input list for an edit.

    Args:
        edl: The validated edit; ``edl.clips`` are placed in order.
        source_path: Full-res master the clips are cut from (input 0).
        has_audio: Whether the source carries an audio track. When False there is
            no ambience to duck under, so music (if any) plays unducked and the
            output's only audio is the music.
        intro_path / outro_path: Resolved card clips bracketing the footage; pass
            ``None`` to omit a card.
        intro_duration / outro_duration: Card lengths (seconds), from probing — used
            to hold the caption over the intro and to size the music bed.
        music_path: Backing track. Looped (``-stream_loop -1``) and trimmed to the
            full timeline. ``None`` omits music.
        caption_path: A still PNG (customer name + date) overlaid on the intro.
            Requires ``intro_path``; ignored otherwise.
        width / height / fps: Output geometry (default 1080p30).
        music_gain: Base music level before ducking.

    Returns:
        A :class:`FilterGraph` ready to splice into an FFmpeg command.
    """
    norm = _norm_chain(width, height, fps)
    state = _GraphState()

    # Input 0 is always the source the EDL cuts from.
    src = state.add_input(source_path)

    # --- video: [intro?] + clips + [outro?] -> concat ------------------------- #
    video_segments: list[str] = []

    if intro_path is not None:
        intro_idx = state.add_input(intro_path)
        if caption_path is not None:
            cap_idx = state.add_input(
                caption_path,
                ("-loop", "1", "-t", _num(intro_duration), "-framerate", str(fps)),
            )
            state.chains.append(f"[{intro_idx}:v]{norm}[vintro_b]")
            state.chains.append(f"[vintro_b][{cap_idx}:v]overlay=0:0[vintro]")
        else:
            state.chains.append(f"[{intro_idx}:v]{norm}[vintro]")
        video_segments.append("vintro")

    for k, clip in enumerate(edl.clips):
        label = f"v{k}"
        state.chains.append(_clip_video_chain(src, clip, norm, label))
        video_segments.append(label)

    if outro_path is not None:
        outro_idx = state.add_input(outro_path)
        state.chains.append(f"[{outro_idx}:v]{norm}[voutro]")
        video_segments.append("voutro")

    video_label = "vout"
    if len(video_segments) == 1:
        # A lone segment: alias it to the output pad (concat n=1 is degenerate).
        state.chains.append(f"[{video_segments[0]}]null[{video_label}]")
    else:
        joined = "".join(f"[{s}]" for s in video_segments)
        state.chains.append(f"{joined}concat=n={len(video_segments)}:v=1:a=0[{video_label}]")

    # --- audio: ambient (delayed under the body) + ducked music --------------- #
    total_duration = intro_duration + edl.output_duration + outro_duration
    audio_label = _build_audio(
        state,
        edl=edl,
        src=src,
        has_audio=has_audio,
        music_path=music_path,
        intro_duration=intro_duration,
        total_duration=total_duration,
        music_gain=music_gain,
    )

    if audio_label is not None:
        # Pad the audio to span the *whole* video (intro + footage + outro). The
        # ambient ends with the footage and the cards are silent, so without this
        # the audio track is shorter than the video — which makes browsers drop or
        # ignore audio after a seek (the media element's duration is the longer
        # video stream). Trailing silence keeps audio_duration == video_duration.
        state.chains.append(
            f"[{audio_label}]apad=whole_dur={_num(total_duration)}[afull]"
        )
        audio_label = "afull"

    return FilterGraph(
        inputs=state.inputs,
        filter_complex=";".join(state.chains),
        video_label=video_label,
        audio_label=audio_label,
    )


def _build_ambient(
    state: _GraphState, edl: EditDecisionList, src: int, intro_duration: float
) -> str:
    """Concatenate the clips' original audio and delay it to sit under the body.

    The ambient is offset by the intro length so it lines up with the footage (the
    intro/outro cards contribute no ambience). Returns the resulting pad label.
    """
    clip_labels: list[str] = []
    for k, clip in enumerate(edl.clips):
        label = f"a{k}"
        state.chains.append(_clip_audio_chain(src, clip, label))
        clip_labels.append(label)

    joined = "".join(f"[{c}]" for c in clip_labels)
    if len(clip_labels) == 1:
        state.chains.append(f"{joined}anull[amb_cat]")
    else:
        state.chains.append(f"{joined}concat=n={len(clip_labels)}:v=0:a=1[amb_cat]")

    delay_ms = int(round(intro_duration * 1000))
    state.chains.append(
        f"[amb_cat]adelay={delay_ms}:all=1,aformat=channel_layouts={AUDIO_LAYOUT}[amb]"
    )
    return "amb"


def _build_audio(
    state: _GraphState,
    *,
    edl: EditDecisionList,
    src: int,
    has_audio: bool,
    music_path: str | None,
    intro_duration: float,
    total_duration: float,
    music_gain: float,
) -> str | None:
    """Assemble the final audio pad from ambient + music, or return ``None``.

    Four cases: ambient + music (duck + mix), music only (straight), ambient only,
    or neither (a silent, video-only render).
    """
    ambient = _build_ambient(state, edl, src, intro_duration) if has_audio else None

    if music_path is None:
        return ambient  # ambient-only, or None when the source was silent too

    music_idx = state.add_input(music_path, ("-stream_loop", "-1"))
    state.chains.append(
        f"[{music_idx}:a]atrim=0:{_num(total_duration)},asetpts=PTS-STARTPTS,"
        f"aresample={AUDIO_RATE},aformat=channel_layouts={AUDIO_LAYOUT},"
        f"volume={music_gain:.4f}[mus]"
    )

    if ambient is None:
        return "mus"  # nothing to duck against -> music plays straight

    # Split the ambient: one copy keys the compressor, one is mixed back in so the
    # original sound is still heard *over* the ducked music. The key is padded to
    # the full timeline first: sidechaincompress ends its output when the *key*
    # ends, so an un-padded key (ambient stops with the footage) would truncate the
    # ducked music — and thus all audio — at the end of the footage. Padding the key
    # with trailing silence lets the music run the whole length, ducking under the
    # footage and playing at full level under the (silent-key) outro.
    state.chains.append(f"[{ambient}]asplit=2[amb_mix][amb_key_raw]")
    state.chains.append(
        f"[amb_key_raw]apad=whole_dur={_num(total_duration)}[amb_key]"
    )
    state.chains.append(f"[mus][amb_key]sidechaincompress={_DUCK}[mus_duck]")
    state.chains.append(
        "[amb_mix][mus_duck]amix=inputs=2:duration=longest:normalize=0[aout]"
    )
    return "aout"
