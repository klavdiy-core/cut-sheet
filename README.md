# cut-sheet

Turn a reference video into an edit timeline you can build on in [Remotion](https://www.remotion.dev).

> RU: «копировать ритм референса» — самый долгий ручной шаг в видео на Remotion: смотреть ролик и выписывать склейки. cut-sheet делает это сам: находит склейки, считает длины планов и темп и пишет готовый `<Series>` с одной секвенцией на каждый план.

## What hurts

A five-stage video workflow (reference → data → structure → Remotion → edits) gets its quality from the first and last stages. The first one — study the reference, write down where every cut lands and how the pace moves — is hours of scrubbing by hand.

## What it does

```sh
python cut_sheet.py reference.mp4 -o cuts --fps 30 --thumbs
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

Measured by looking at the frames on both sides of every candidate (not by trusting the tool):

| clip | role | fixed threshold 0.3 | cut-sheet |
|---|---|---|---|
| *Third Avenue El* (1950s), 60 s, 22 cuts | rules tuned here | 11 / 22 found, 0 false | **21 / 22 found, 1 false** |
| *Despotism* (1946), 60 s, 5 hard cuts | checked blind afterwards | 4 / 5 found, 0 false | **5 / 5 found, 1 false** |

Two clips, 27 cuts, both Prelinger Archives films from archive.org. That is a small test, said plainly; if it gets your footage wrong, `--threshold 0.3` gives you ffmpeg's plain behaviour, and an issue with a timestamp helps.

## Limits

- **Hard cuts only.** Dissolves, fades to black and wipes are gradual; *Despotism* has four, and none is reported. They end up inside a shot.
- A cut between two very similar dark shots can slip by (the one miss above: a dark sign to a dark street).
- `--min-shot` (default 0.2 s) merges flashes shorter than that into the previous shot.
- The pace is cuts per minute, not audio tempo.

## Test

```sh
python -m unittest discover -s tests
```

Synthetic videos with known cuts (found within a frame; low-contrast cuts a fixed threshold misses; a zoom that is not a cut; frames adding up exactly at another fps), and two excerpts of real film (22 s in total) with every cut and every non-cut peak labelled by eye. Each detection rule was removed on purpose to make sure a test goes red without it; a rule that no test needed ("score ≥ 0.4 is always a cut") was dropped.

Test footage: excerpts of *Third Avenue El* by Carson Davidson, Prelinger Archives, [archive.org/details/ThirdAve1950](https://archive.org/details/ThirdAve1950), stated Public Domain on the item page.

## License

MIT
