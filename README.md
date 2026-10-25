# cut-sheet

Turn a reference video into an edit timeline you can build on in [Remotion](https://www.remotion.dev).

> RU: «копировать ритм референса» — самый долгий ручной шаг в видео на Remotion: смотреть ролик и выписывать склейки. cut-sheet делает это сам: находит склейки, считает длины планов и темп и пишет готовый `<Series>` с одной секвенцией на каждый план.

## What hurts

A five-stage video workflow (reference → data → structure → Remotion → edits) gets its quality from the first and last stages. The first one — study the reference, write down where every cut lands and how the pace moves — is hours of scrubbing by hand.

## What it does

```sh
git clone https://github.com/klavdiy-core/cut-sheet && cd cut-sheet
python cut_sheet.py tests/fixtures/ThirdAve1950_excerpt1.mp4 -o cuts --fps 30 --thumbs   # a real 10 s film clip ships with it
python cut_sheet.py reference.mp4 -o cuts --fps 30 --thumbs                               # then your own
```

```
third60.mp4: 59.993 s, 12 shots, mean 5.0 s (median 3.07, 0.834..17.818), 11.0 cuts/min
    1     0.000 ->    7.674   7.674 s    230 f
    2     7.674 ->   14.014   6.340 s    190 f
  ...
wrote third60.cuts.json, third60.cuts.tsx, third60.thumbs/
```

- **`<name>.cuts.json`** — every shot: start, end, length in seconds and in frames at your composition's fps; stats: shot count, mean / median / shortest / longest shot, cuts per minute, and the pace in 10-second windows.
- **`<name>.cuts.tsx`** — a Remotion component: `shots`, `FPS`, `TOTAL_FRAMES`, and `<CutSheet>`, a `<Series>` with one `<Series.Sequence premountFor={FPS}>` per shot. Without props it shows a numbered placeholder per shot; pass `renderShot={(shot) => ...}` to put your own footage in each slot. It type-checks with `tsc --strict` against the real `remotion` package.
- **`<name>.thumbs/`** (with `--thumbs`) — a frame from the middle of each shot.

Frames come from rounded cut times, so the shots add up to exactly `round(duration × fps)` frames: no drift at the end of a long timeline.

```tsx
import { Composition } from "remotion";
import { CutSheet, FPS, TOTAL_FRAMES } from "./third60.cuts";

export const Root = () => (
  <Composition id="MyCut" component={CutSheet} fps={FPS} durationInFrames={TOTAL_FRAMES} width={1920} height={1080} />
);
```

Needs `ffmpeg` and `ffprobe` on PATH. The script itself is standard-library Python 3.8+.

## How it finds cuts, and how well

ffmpeg gives a scene-change score for every frame. A fixed threshold (the usual `select='gt(scene,0.3)'`) misses quiet cuts and still trips on fast motion. On faded 1950s film half the cuts score 0.15–0.26, while planks rushing past the camera score up to 0.25 inside one shot. So cut-sheet takes every local peak above 0.1 and calls it a cut only when

- the **colour histogram changes** across it (≥ 0.25 of the mass moves), or
- the peak stands **far above its neighbourhood** (≥ 6× the mean of the surrounding half-second).

Motion inside a shot keeps the colours and keeps the neighbourhood busy; a cut does neither.

Then each cut is **checked on the picture itself**: the frames four apart on its two sides must really differ. Haze
clearing, or a plane turning inside one shot, can trip the score while the same picture sits on both sides; those
measured 5–14 grey levels apart, real cuts 29 and more (two of them between shots of the same grey street).

Measured by looking at the frames on both sides of every candidate (not by trusting the tool):

| clip | role | fixed threshold 0.3 | cut-sheet |
|---|---|---|---|
| *Third Avenue El* (1950s), 60 s, 22 cuts | rules tuned here | 11 / 22 found, 0 false | **21 / 22 found, 1 false** |
| *Despotism* (1946), 60 s, 5 hard cuts | checked blind afterwards | 4 / 5 found, 0 false | **5 / 5 found, 1 false** |

A third clip came from an outside tester (*San Francisco Scenes*, Prelinger, 74 s, hazy aerial footage): 22 cuts
were reported, and by eye most of the doubtful ones were the same shot on both sides. With the picture check, 14 are
reported; one false cut there (a flying boat in haze, 44.5 s) is still reported.

Three clips, all black-and-white Prelinger Archives films from archive.org. That is a small test, said plainly;
colour footage and fast modern editing are untested; if it gets your footage wrong, `--threshold 0.3` gives you ffmpeg's plain behaviour, and an issue with a timestamp helps.

## Dissolves and fades

Gradual transitions are not cuts, so they are listed apart: `transitions` in the JSON, in the summary line ("1 shots + 3 gradual transition(s) not counted (--gradual)") and with their times on stderr. A reference built on dissolves no longer comes back as a bare "1 shot". `--gradual` ends a shot at the middle of each one. Two detections of the same transition are merged into one span.

How: around each frame, the picture must change a lot, sit on a straight blend between the frames before and after (every pixel at the same pace), have a calmer stretch on at least one side, and end on a different picture than it started. A zoom or a crowd moving changes the picture without blending it; a glow that fades back returns to the same picture. **A slow pan over a hazy, low-contrast scene can still look like a blend:** on the outside tester's film, 2 of 10 spans were a pan across the Golden Gate Bridge, and one of them is still listed.

How well, checked by eye on frames every 0.1 s (not by the tool's own numbers): two NASA animations of 50 s each (8 dissolves and fades) and three films with only hard cuts (a 1953 newsreel with a handheld camera, two *Third Avenue El* excerpts). **9 found, 8 real, none visible missed;** the one false span is a slow lighting change on a capsule. Span edges are good to about 0.3 s.

## Limits

- **A dissolve between two shots full of motion can be missed.** Such a blend looks like the motion itself; one planted on *Third Avenue El* (a train passing in both shots) was not found. Dissolves with a calm side, and every fade through black, are.
- Wipes and other shaped transitions are not detected.
- A cut between two very similar dark shots can slip by (the one miss above: a dark sign to a dark street).
- `--min-shot` (default 0.2 s) merges flashes shorter than that into the previous shot. Damaged film with white or black flash frames can still leave a few "shots" of 0.2–0.3 s; raise `--min-shot` for such material.
- A slow pan in haze can be listed as a transition (above). Each one is in the JSON with its times, so check them by eye before you trust them.
- The pace is cuts per minute, not audio tempo.

## Test

```sh
python -m unittest discover -s tests
```

Synthetic videos with known cuts (found within a frame; low-contrast cuts a fixed threshold misses; a zoom that is not a cut; frames adding up exactly at another fps), and two excerpts of real film (22 s in total) with every cut and every non-cut peak labelled by eye. Each detection rule was removed on purpose to make sure a test goes red without it; a rule that no test needed ("score ≥ 0.4 is always a cut") was dropped.

Dissolves: three excerpts of NASA's *Orion EFT1 Animation No Narration* (60 fps) with each dissolve's span labelled by eye; a fade to black and back on *Third Avenue El*; the glow that is not a transition; the hard-cut films with no transitions; a zoom. Every rule of the dissolve finder was removed on purpose: three that no test and no real clip needed were dropped.

Test footage: excerpts of *Orion EFT1 Animation No Narration*, NASA, [archive.org/details/OrionEft1AnimationNoNarration](https://archive.org/details/OrionEft1AnimationNoNarration), CC0. Excerpts of *Third Avenue El* by Carson Davidson, Prelinger Archives, [archive.org/details/ThirdAve1950](https://archive.org/details/ThirdAve1950), stated Public Domain on the item page.

## License

MIT
