# AUDIT: Scene labels vs. the load-spec `scene_filter: "shared_only"` assumption

**Phase 1 read-only audit — 2026-08-10.** No code was changed. Every claim carries a
`file:line` reference against the current working tree.

## Verdict up front

`scene_filter: "shared_only"` is **not a parameter** and it is **not a small feature.
It is a labelling project.** The composition machinery could accept a label
restriction with modest work (there is an exact precedent: the freefall-only
deliverables). But the labels it would filter on do not mean what the build spec
assumes:

1. **"door/aerial" does not exist as a label at all** — door footage is a
   time-window convention inside other scenes, and the aerial exit lives *inside*
   the `freefall` scene, which is the customer's personal scene.
2. **`takeoff` and `plane` are only ever emitted when the clip has GPS altitude.**
   On GPS-less footage (common: no satellite lock) they are never produced; that
   footage collapses into `boarding`.
3. **Personal ground scenes (interviews) are separated from shared ground scenes
   (boarding) by clip position in the recording order, not by content.** On
   telemetry the two are identical (~1 g, ground level). A mid-sequence interview
   is labelled `boarding`; early boarding is labelled `intro_interview`. The
   feature's promise — a paying customer's personal scenes never appear in anyone
   else's video — **cannot be guaranteed by these labels.**

Two of the three stop conditions are met (details in the Stop Conditions section).

---

## A. Label inventory

There are **two different label systems** in this repo. They are not the same thing
and prior docs blur them.

### A.1 The scene-pipeline labels (what production jobs use)

Every package (`selfie`, `external`, `video_only`, `photo_only`, `ultimum`) runs
through `api/selfie.py` ([run_selfie_pipeline](api/selfie.py#L2643),
[run_ultimum_pipeline](api/selfie.py#L3049)). Each raw clip (whole file) is
classified into exactly one scene label. The canonical set is `SCENE_ORDER` at
[api/selfie.py:91-100](api/selfie.py#L91-L100):

```
intro_interview, boarding, takeoff, plane, freefall, canopy, landing, outro_interview
```

- **Closed-ish enum, loosely enforced.** `VALID_SCENES = frozenset(SCENE_ORDER)`
  ([api/selfie.py:112](api/selfie.py#L112)) validates only *manual overrides*
  (`scene_labels.json`, [load_scene_labels, api/selfie.py:759-785](api/selfie.py#L759-L785)).
  The EDL clip's `scene` field itself is a **free-form string**
  ([api/selfie.py:155](api/selfie.py#L155)) — nothing validates that a composed
  clip names a real scene.
- **Two labels are never emitted by the classifier proper:**
  - `landing` is produced only by a post-hoc rename of a post-freefall `canopy`
    scene whose mean vertical acceleration exceeds 1.5 g
    ([build_scenes, api/selfie.py:966-980](api/selfie.py#L966-L980);
    threshold [api/selfie.py:628](api/selfie.py#L628)). `classify_scene` itself
    never returns `"landing"`.
  - `unknown` is a ninth, undocumented outcome: a GPS clip that falls through all
    rules ([api/selfie.py:665](api/selfie.py#L665)). Up to two unknowns become a
    scene literally named `unknown`, appended *after* the known scenes and flagged
    `needs_review` ([api/selfie.py:956-958](api/selfie.py#L956-L958),
    [1004](api/selfie.py#L1004), [1043-1044](api/selfie.py#L1043-L1044)); more than
    two fails the job with `LowConfidenceError`
    ([api/selfie.py:818-822](api/selfie.py#L818-L822)).
- Granularity is **one label per raw file**. The classifier never splits a clip;
  a file spanning two phases takes one label, whole
  ([classify_files, api/selfie.py:788-823](api/selfie.py#L788-L823)).

### A.2 The single-master phase timeline (legacy path, not what packages run)

`metadata.PHASES` ([metadata/__init__.py:30-39](metadata/__init__.py#L30-L39)):
`plane_boarding, exit, freefall_start, freefall_end, deployment, canopy_start,
landing, landing_end`. These are **timestamps** on one continuous MP4
([Segmentation, metadata/segment.py:48-71](metadata/segment.py#L48-L71)), not clip
labels, and its EDL ([edl/schema.py Clip:50-88](edl/schema.py#L50-L88)) has **no
scene field at all** — only raw source times. This path's Claude composer
([edl/compose.py:175](edl/compose.py#L175)) is not wired into the API pipeline
([api/tasks.py:12](api/tasks.py#L12) says so explicitly); it is reachable only via
`scripts/process_jump.py`.

Any `scene_filter` discussion therefore concerns the scene pipeline (A.1) only.

## B. Which phases are scored

**The doc claim is wrong for production.** [CLAUDE.md:57](CLAUDE.md#L57) says
MediaPipe runs "*only during freefall* (saves 95% compute)". That describes the
legacy `analysis.score_freefall` entry point
([analysis/__init__.py:38](analysis/__init__.py#L38)), which takes an explicit
freefall window and is called by **no production code path** — only the
`python -m analysis` CLI ([analysis/__main__.py:34](analysis/__main__.py#L34)).

The scene pipeline scores **every scene in full**, freefall or not:
[score_scenes](api/selfie.py#L1077-L1098) loops over all manifest scenes and
[score_scene](api/selfie.py#L1058-L1074) scores each one's whole duration (called
from [run_selfie_pipeline:2688](api/selfie.py#L2688) and per-camera in the ultimum
path). The compute saving that survives is proxy-based (LRV when available,
[api/selfie.py:1092-1094](api/selfie.py#L1092-L1094)) plus a freefall-window
*filter on the rows handed to Claude*
([_build_compose_prompt, api/selfie.py:1221-1230](api/selfie.py#L1221-L1230)).

Per non-freefall phase, detection status (scene pipeline):

| Phase | With GPS altitude | Without GPS |
|---|---|---|
| boarding | (a) own label — but also the **default bucket** for any ground clip mid-sequence ([api/selfie.py:662-664](api/selfie.py#L662-L664)) | (a)/(b) — the catch-all for everything pre-freefall that isn't position-guessed as intro ([api/selfie.py:724-726](api/selfie.py#L724-L726)) |
| takeoff | (a) own label: `altitude_delta > 100` ([api/selfie.py:654-655](api/selfie.py#L654-L655)) | **(c) never emitted** — `_classify_no_gps` can only return freefall/canopy/intro/outro/boarding ([api/selfie.py:668-687](api/selfie.py#L668-L687)) |
| plane | (a) own label: height > 2000 m and calm ([api/selfie.py:652-653](api/selfie.py#L652-L653)) | **(c) never emitted** — "plane vs boarding is indistinguishable without GPS" ([api/selfie.py:674](api/selfie.py#L674)) |
| intro/outro interview | (a) label exists, but assigned **by position**, not content ([api/selfie.py:658-661](api/selfie.py#L658-L661)) | same, position only ([api/selfie.py:683-687](api/selfie.py#L683-L687), [726-728](api/selfie.py#L726-L728)) |
| canopy | (a) own label ([api/selfie.py:656-657](api/selfie.py#L656-L657)) | (a) via magnitude stats ([api/selfie.py:681-682](api/selfie.py#L681-L682)) |
| landing | (b)→(a): emitted only as the canopy rename ([api/selfie.py:966-980](api/selfie.py#L966-L980)) | same rename applies if the accl signature fires |
| door / exit-prep | **(c) not a label** — a time convention (see E) | (c) |

## C. Boundary reliability

**Scene boundaries are raw-file boundaries.** The classifier assigns whole clips;
nothing re-segments inside a file. So "where does boarding end and takeoff begin"
is answered by *where the instructor stopped recording*, and the entire accuracy
model rests on staff filming discrete clips per shot —
[PACKAGES.md:45](PACKAGES.md#L45) states this dependency outright ("Staff film to
this shot list").

Signals per label, and the failure mode:

- **freefall** — accelerometer magnitude: near-0 g exit dip / high buffeting
  variance ([api/selfie.py:650-651](api/selfie.py#L650-L651),
  [679-680](api/selfie.py#L679-L680), thresholds
  [615-621](api/selfie.py#L615-L621)). Most reliable label in the system.
  Its *internal* boundaries (`exit_offset`, `deploy_offset`) come from dedicated
  accelerometer detectors ([detect_exit_offset,
  api/selfie.py:304-348](api/selfie.py#L304-L348); [detect_deploy_offset,
  437-525](api/selfie.py#L437-L525)). Both silently return `0.0` when telemetry is
  unreadable — the aerial-window clamp then degrades rather than failing.
- **takeoff / plane / canopy (GPS)** — altitude mean/delta relative to the jump's
  lowest GPS reading ([api/selfie.py:648](api/selfie.py#L648),
  [652-657](api/selfie.py#L652-L657)). Fails when GPS never locks (the clip then
  routes to the accelerometer fallback and these labels vanish, folding into
  `boarding`), or when one file spans phases (first matching rule takes the whole
  clip — e.g. a ground-to-altitude file with `altitude_delta > 100` is all
  `takeoff` including its boarding head).
- **intro_interview / outro_interview / boarding** — ground test (`height < 50 m`)
  plus **clip index**: first 20 % of the sorted clip list → intro, last 20 % →
  outro, everything else → boarding
  ([api/selfie.py:646-647](api/selfie.py#L646-L647),
  [658-664](api/selfie.py#L658-L664)); GPS-less clips are placed relative to the
  freefall anchor with the same 20 % position rule
  ([api/selfie.py:704-728](api/selfie.py#L704-L728)). Failure: any deviation from
  the expected shot count/order silently relabels — the clip is not dropped, not
  flagged, it just gets the wrong (valid-looking) label.
- **landing** — only the accl-signature rename of a post-freefall `canopy` scene
  (>1.5 g signed mean, [api/selfie.py:966-980](api/selfie.py#L966-L980)); renames
  are at least surfaced in the manifest's `flagged` list. In the legacy segmenter,
  landing needs a GPS stream that captured ≥100 m of descent
  ([metadata/segment.py:157](metadata/segment.py#L157)); and `plane_boarding` is
  **never detected** there — deliberately left `None`
  ([metadata/segment.py:194-196](metadata/segment.py#L194-L196)) with fallback to
  *human-labeled* `sample-data/labels.json`
  ([metadata/__init__.py:96-101](metadata/__init__.py#L96-L101)).

What happens on failure: a misclassified clip keeps a confident, valid label and
flows into scene concat unchallenged. Only two conditions surface at all: `unknown`
(GPS clips only, [api/selfie.py:751-752](api/selfie.py#L751-L752)) and the
canopy→landing rename. There is no confidence score on any label.

## D. Personal vs shared ground scenes — the critical one

**Nominally yes, practically no.** `intro_interview`/`outro_interview` (personal)
and `boarding` (shared) are distinct label strings — this is *not* a single
"ground" bucket. But the audit question is whether the separation is trustworthy,
and it is not, for four independent reasons:

1. **The separation is positional, not detected.** On telemetry an interview and
   boarding are the same signal (ground, ~1 g). The classifier's literal rule:
   ground clip in the first 20 % of the clip list → `intro_interview`, ground clip
   mid-list → `boarding` ([api/selfie.py:658-664](api/selfie.py#L658-L664)). An
   interview filmed fourth-of-ten is labelled `boarding` — and a `shared_only`
   filter would then **put the customer's personal interview into the load
   master**. The reverse also holds: real boarding footage early in the card is
   labelled `intro_interview` and would be wrongly excluded.
2. **Shared footage deliberately lives inside the personal intro scene.** The
   pipeline's own compose rules and backstops treat the aircraft-entry (boarding)
   beat as the *second source file of the `intro_interview` scene* when the
   interview and walk-to-plane were one continuous take
   ([_COMPOSE_RULES, api/selfie.py:1143-1149](api/selfie.py#L1143-L1149);
   [_intro_entry_clip, 1261-1279](api/selfie.py#L1261-L1279);
   [edl/validate.py:540-568](edl/validate.py#L540-L568)). Label-level filtering is
   the wrong resolution by the code's own admission — the same scene name contains
   both personal and shared material, addressed by file offsets.
3. **GPS-less collapse.** Without GPS, `takeoff` and `plane` are never emitted;
   the whole pre-jump block becomes `boarding` or `intro_interview` by position
   ([api/selfie.py:668-687](api/selfie.py#L668-L687)). A `shared_only =
   {boarding, takeoff, plane}` filter on such footage filters mostly on the
   position heuristic.
4. **A camera-flyer's ground clips of *other* customers have no identity
   dimension.** Labels say *what phase*, never *who is in frame*. Nothing in the
   pipeline (MediaPipe scoring included — it scores smiles, not identities;
   [analysis/score.py](analysis/score.py)) can tell "boarding shot featuring the
   assigned customer" from "boarding shot featuring someone else". The load-spec's
   exclusion promise needs a who-axis the data model simply lacks.

## E. Exits

**Not a label.** There is no `exit` or `door` scene in `SCENE_ORDER`. The exit is
a **timestamp inside the `freefall` scene**: `exit_offset`, detected from the
accelerometer and stored on the freefall manifest entry
([build_scenes, api/selfie.py:1033-1041](api/selfie.py#L1033-L1041);
[detect_exit_offset, 304-348](api/selfie.py#L304-L348)). The freefall scene
*starts inside the aircraft* — the compose prompt hammers this
([api/selfie.py:1115-1121](api/selfie.py#L1115-L1121)) — so in-plane and door
footage is part of the `freefall`-labelled scene, before `exit_offset`.

Door/exit-prep is likewise a time convention, in one of two places: the seconds
before `exit_offset` within the freefall scene, or the last `_DOOR_PREP_S` (8 s)
of whichever aircraft scene (`plane`/`takeoff`/`boarding`) precedes freefall
([api/selfie.py:1782-1783](api/selfie.py#L1782-L1783),
[_aircraft_before_freefall 1844-1850](api/selfie.py#L1844-L1850),
[_curated_freefall 1889-1902](api/selfie.py#L1889-L1902)). Consequence for the
feature: the build spec's shared "door/aerial" phase is partly inside the
customer's personal `freefall` scene (excluded by `shared_only`, so that shared
material is unreachable) and partly an unlabelled tail convention on aircraft
scenes. In the legacy segmenter, `exit` is its own timestamp
([metadata/segment.py:56](metadata/segment.py#L56)) — still not a clip label.

## F. Is label a filter or only a hint?

Labels are **the composition currency, but there is no filter parameter.**

- Scene-pipeline EDL clips are scene-name-relative: `{scene, src_start, src_end,
  speed_multiplier, camera}` ([api/selfie.py:140-161](api/selfie.py#L140-L161)),
  and the renderer resolves each clip through its scene's file. So a restriction
  "build a cut from only these scene types" is *expressible* in the data model.
- **Precedent exists**: the freefall deliverables are exactly a label-restricted
  cut, enforced twice — at compose by a dedicated curator
  ([_curated_freefall, api/selfie.py:1861-1927](api/selfie.py#L1861-L1927)) and at
  validation by `_drop_non_freefall`
  ([edl/validate.py:226-236](edl/validate.py#L226-L236)), keyed off the hardcoded
  `FREEFALL_DELIVERABLES` frozenset
  ([edl/validate.py:34-36](edl/validate.py#L34-L36)). Note the mechanism: a
  **per-deliverable-name rule set**, not a `scene_filter` argument.
- **No general parameter exists.** `compose_edls`
  ([api/selfie.py:1539-1548](api/selfie.py#L1539-L1548)) takes no scene
  restriction; the house cut builds `full_video` by iterating **all** manifest
  scenes ([api/selfie.py:2021-2025](api/selfie.py#L2021-L2025)); `EDLResponse` is
  a closed three-field model ([api/selfie.py:170-177](api/selfie.py#L170-L177)).
  The only subtractive control is `exclude.json` — per-scene **time ranges** cut
  at render ([api/selfie.py:117-119](api/selfie.py#L117-L119),
  [load_exclusions:3346](api/selfie.py#L3346),
  [apply_exclusions:3384](api/selfie.py#L3384)) — a manual per-job tool, not a
  policy.

**The exact function where a `scene_filter` would have to apply:** a new curator in
`api/selfie.py` alongside `_curated_freefall` (or a scene-subset pass in front of
[`_house_edls`, api/selfie.py:1985](api/selfie.py#L1985)), persisted through the
extra-deliverable path `run_ultimum_pipeline` already uses — curate →
`_validated(clips, deliverable, manifest)` → `_persist_clips` →
`render_selfie_video` ([api/selfie.py:3149-3156](api/selfie.py#L3149-L3156)).

That is a **new-code feature, not a parameter change** — but it is not a redesign
either, with two sharp caveats:

1. The new cut must use a **new deliverable name**. Reusing `full_video`/
   `highlights` triggers the milestone backstops, which would *re-inject the
   personal scenes the filter removed*: `_ensure_story`/`_ensure_milestones` force
   exit, deployment and landing beats into those deliverables
   ([api/selfie.py:1476-1489](api/selfie.py#L1476-L1489),
   [1507-1536](api/selfie.py#L1507-L1536)), and `validate_and_repair` injects
   deploy/boarding/intro beats for the story deliverable names
   ([edl/validate.py:755-778](edl/validate.py#L755-L778)). An unknown deliverable
   name gets only dedupe + chronological sort
   ([edl/validate.py:765-766](edl/validate.py#L765-L766)) — safe, but it also
   means **zero validation rules currently exist for a shared-only cut**; the
   "never a personal scene" guarantee would need its own validator rule.
2. In the legacy single-master path the restriction is **not expressible at all**
   (no scene field in the EDL, [edl/schema.py:50-67](edl/schema.py#L50-L67)) —
   irrelevant for production packages, but it rules out that path entirely.

One scope note outside the composition step: the pipeline is strictly one job =
one jump = one customer (CLAUDE.md "One job per jump"; the matcher refuses
ambiguous clips, [ingest/match.py](ingest/match.py)). A "load master" spans a
load. Where that job comes from and which clips it owns is a second unknown the
build spec's "small change" framing does not cover; this audit only establishes
that its *label* assumption fails.

## Stop conditions — two of three are met

1. **"Classifier does not emit distinct labels for the shared phases"** — **MET.**
   `boarding`/`takeoff`/`plane` exist as distinct labels *only when GPS altitude is
   present*; GPS-less footage never yields `takeoff` or `plane`
   ([api/selfie.py:668-687](api/selfie.py#L668-L687)). `door/aerial` is not a
   label under any conditions (section E).
2. **"Personal ground scenes cannot be distinguished from shared ground scenes by
   label"** — **MET in substance.** The label strings differ, but assignment is a
   clip-position heuristic over identical telemetry
   ([api/selfie.py:658-664](api/selfie.py#L658-L664)), the code's own conventions
   store boarding footage inside `intro_interview`
   ([api/selfie.py:1143-1149](api/selfie.py#L1143-L1149)), and no label carries
   identity (whose face is in frame). The exclusion guarantee cannot be made.
3. **"Label restriction is not expressible in the composition step without
   restructuring"** — **NOT met** for the scene pipeline: the freefall
   deliverables prove the pattern, and the extension points are clear (section F).
   (It *is* unexpressible in the legacy single-master EDL, which has no scene
   concept.)

Per the stop conditions: this audit stops here and proposes no fix.

## Where prior handoff docs disagree with the code

- **[CLAUDE.md:57](CLAUDE.md#L57)** — "Score — run MediaPipe on the LRV proxy
  *only during freefall* (saves 95 % compute)". False for every production
  package: `score_scenes` scores **every scene in full**
  ([api/selfie.py:1091-1094](api/selfie.py#L1091-L1094)). Freefall-only scoring
  exists only in the un-wired legacy `analysis.score_freefall`
  ([analysis/__init__.py:38](analysis/__init__.py#L38)).
- **CLAUDE.md "Pipeline Stages" 2 and 4** describe the single-master path
  (`metadata.segment` timeline → `edl.compose_edl` → `edl.json`). Production jobs
  run `api/selfie.py`; `api/tasks.py:12` itself notes `edl.compose_edl` is not
  wired in. Anyone sizing the load-spec feature from the CLAUDE.md stage list is
  sizing against the wrong pipeline.
- **[PACKAGES.md:43-45](PACKAGES.md#L43-L45)** — "The classifier labels every clip
  with one of eight scene types". Overstated: `unknown` is a ninth outcome
  ([api/selfie.py:665](api/selfie.py#L665)); GPS-less clips can only ever receive
  five of the eight (never `takeoff`, `plane`, or classifier-emitted `landing`);
  and `landing` is a rename, not a classification
  ([api/selfie.py:966-980](api/selfie.py#L966-L980)). PACKAGES.md:45 does state
  the honest core assumption — "Staff film to this shot list" — which is exactly
  the assumption a multi-customer load master breaks.
- **The build spec's `scene_filter: "shared_only"` framing** (external to this
  repo; no such string exists anywhere in the codebase — verified by repo-wide
  search) assumes labels the classifier does not emit (`door/aerial`), emits only
  conditionally (`takeoff`, `plane`), or assigns by position rather than content
  (`interview` vs `boarding`). The "small change" estimate does not survive
  contact with the classifier.
