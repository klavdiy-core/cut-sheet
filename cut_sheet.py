#!/usr/bin/env python3
"""cut-sheet: turn a reference video into an edit timeline you can build on in Remotion.

Finds the cuts with ffmpeg's scene detection, lists every shot (start, end, length, in seconds and in frames at
your target fps), measures the pace (mean shot length, cuts per minute, pace over time), saves a thumbnail per shot,
and writes <name>.cuts.json plus <name>.cuts.tsx: a <Series> with one <Series.Sequence> per shot, ready to fill.
Needs ffmpeg and ffprobe on PATH. Python standard library only.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys

__version__ = "0.2.0"
PTS = re.compile(r"Parsed_showinfo.*?\bpts_time:\s*([0-9.]+)")


# ---------- ffmpeg ----------

def _run(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {r.stderr.strip()[-400:]}")
    return r


def probe(path):
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
              "stream=avg_frame_rate,r_frame_rate,width,height:format=duration", "-of", "json", path])
    d = json.loads(r.stdout)
    if not d.get("streams"):
        raise RuntimeError(f"{path}: no video stream")
    st = d["streams"][0]
    num, _, den = (st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return {"duration": float(d["format"]["duration"]), "fps": round(fps, 3),
            "width": st.get("width"), "height": st.get("height")}


def parse_showinfo(text):
    return [float(m.group(1)) for m in PTS.finditer(text)]


def detect_cuts(path, threshold=0.3):
    """Times (s) of the first frame of each new shot."""
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an", "-sn",
              "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    return parse_showinfo(r.stderr)


SCORE_LINE = re.compile(r"pts_time:([0-9.]+)|lavfi\.scene_score=([0-9.]+)")


def parse_scores(text):
    """(pts_time, scene_score) per frame from ffmpeg's metadata=print log."""
    out, t = [], None
    for m in SCORE_LINE.finditer(text):
        if m.group(1) is not None:
            t = float(m.group(1))
        elif t is not None:
            out.append((t, float(m.group(2))))
            t = None
    return out


def _histograms(path, wanted, w=64, h=48):
    """Coarse colour histograms (8x8x8 bins) of the frames whose index is in `wanted`, streamed."""
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-sn", "-vf", f"scale={w}:{h}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    size, out, i = w * h * 3, {}, 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            if i in wanted:
                bins = [0] * 512
                for k in range(0, size, 3):
                    bins[(buf[k] >> 5) * 64 + (buf[k + 1] >> 5) * 8 + (buf[k + 2] >> 5)] += 1
                out[i] = bins
            i += 1
    finally:
        p.stdout.close()
        p.wait()
    return out


def detect_cuts_adaptive(path, floor=0.1, hist_min=0.25, ratio_min=6.0, window=12):
    """A frame is a cut when its scene score is a local peak above `floor` and either the colour histogram changes a
    lot across it (>= hist_min) or the peak stands far above its neighbourhood (>= ratio_min x the local mean).
    Camera moves and motion inside a shot raise the score but keep the colours and the neighbourhood busy.
    Tuned on 27 eye-labelled points of one film, checked blind on another (see README)."""
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an", "-sn",
              "-vf", "select='gte(scene,0)',metadata=print:key=lavfi.scene_score", "-f", "null", "-"])
    frames = parse_scores(r.stderr)
    sc = [s for _, s in frames]
    n = len(sc)
    peaks = [j for j in range(1, n) if sc[j] >= floor and sc[j] == max(sc[max(0, j - 3):j + 4])]
    need = {max(0, j - 2) for j in peaks} | {min(n - 1, j + 2) for j in peaks}
    hists = _histograms(path, need)
    cuts = []
    for j in peaks:
        nb = sc[max(0, j - window):max(0, j - 1)] + sc[j + 2:j + window + 1]
        ratio = sc[j] / ((sum(nb) / len(nb) if nb else 0) + 1e-3)
        a, b = hists.get(max(0, j - 2)), hists.get(min(n - 1, j + 2))
        hd = 0.5 * sum(abs(x - y) for x, y in zip(a, b)) / sum(a) if a and b else 0.0
        if hd >= hist_min or ratio >= ratio_min:
            cuts.append(frames[j][0])
    return cuts


def _gray_frames(path, w=64, h=36):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-sn", "-vf", f"scale={w}:{h}",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE)
    size, out = w * h, []
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            out.append(buf)
    finally:
        p.stdout.close()
        p.wait()
    return out


def _blend(a, b, m):
    """How far between a and b the frame m is (alpha, by projection), and what is left over (mean |residual|)."""
    ab = [y - x for x, y in zip(a, b)]
    den = sum(v * v for v in ab) or 1
    alpha = sum((z - x) * v for x, z, v in zip(a, m, ab)) / den
    res = sum(abs(z - x - alpha * v) for x, z, v in zip(a, m, ab)) / len(a)
    return alpha, res


def detect_transitions(path, fps, lag_s=0.4, min_change=12.0, linear_max=0.3, plateau=1.5):
    """Dissolves and fades: the picture blends from one shot into another.
    For a centre frame m with a = m-lag and b = m+lag:
    - the change |a-b| is large;
    - the window just before a or just after b changes much less (a steady shot on at least one side). A zoom, a pan
      or a train going by changes all the time, so it has no such plateau;
    - the frames around m sit on the straight path a -> b, partway along it (a blend, not a new picture);
    - the picture before the span differs from the one after it (a glow that fades back is not a transition).
    Returns [{start_s, end_s}] (the span is the centres widened by the lag). Checked by eye on 7 real clips: 8 of
    9 found spans are real dissolves or fades, no visible one missed (see README)."""
    f = _gray_frames(path)
    n, lag = len(f), max(3, round(fps * lag_s))
    if n < 6 * lag + 1:
        return []
    px = len(f[0])

    def diff(p, q):
        return sum(abs(x - y) for x, y in zip(p, q)) / px

    centres = []
    for i in range(3 * lag, n - 3 * lag):
        a, b, m = f[i - lag], f[i + lag], f[i]
        d = diff(a, b)
        if d < min_change:
            continue
        # the windows before and after span the same 2*lag frames, so steady motion scores about 1, not 2.
        # One steady side is enough: a fade often starts right after a cut.
        if d < plateau * min(diff(f[i - 3 * lag], a), diff(b, f[i + 3 * lag])):
            continue
        # every pixel on one straight path a -> b, all at the same pace: a blend. A moving object changes different
        # pixels at different times, so its frames do not sit on that path.
        q = max(1, lag // 2)
        fits = [_blend(a, b, f[k]) for k in (i - q, i, i + q)]
        alphas = [al for al, _ in fits]
        if all(r < linear_max * d for _, r in fits) and 0.2 <= alphas[1] <= 0.8:
            centres.append(i)
    runs, cur = [], []
    for i in centres:
        if cur and i - cur[-1] > 2:
            runs.append(cur)
            cur = []
        cur.append(i)
    if cur:
        runs.append(cur)
    spans = [(max(0, r[0] - lag), min(n - 1, r[-1] + lag)) for r in runs if len(r) >= 3]   # widened by the lag
    # a transition leads to another picture; a glow or flash that fades back to the same shot does not
    return [{"start_s": round(s / fps, 3), "end_s": round(e / fps, 3)} for s, e in spans
            if diff(f[max(0, s - lag)], f[min(n - 1, e + lag)]) >= min_change]


# ---------- timeline ----------

def build_shots(cuts, duration, fps, min_shot=0.2):
    """Shots between cuts. A cut closer than min_shot to the previous one (a flash) is dropped.
    Frames are taken from rounded boundaries, so the shots add up to exactly round(duration * fps)."""
    bounds = [0.0]
    for c in sorted(cuts):
        if c - bounds[-1] >= min_shot and duration - c >= min_shot:
            bounds.append(c)
    bounds.append(duration)
    frames = [round(b * fps) for b in bounds]
    shots = []
    for i in range(len(bounds) - 1):
        if frames[i + 1] <= frames[i]:
            continue
        shots.append({
            "index": len(shots) + 1,
            "start_s": round(bounds[i], 3),
            "end_s": round(bounds[i + 1], 3),
            "duration_s": round(bounds[i + 1] - bounds[i], 3),
            "from_frame": frames[i],
            "duration_frames": frames[i + 1] - frames[i],
        })
    return shots


def stats(shots, duration, window=10.0):
    lengths = [s["duration_s"] for s in shots]
    cuts = [s["start_s"] for s in shots[1:]]
    pace, t = [], 0.0
    while t < duration:
        n = sum(1 for c in cuts if t <= c < t + window)
        span = min(window, duration - t)
        pace.append({"from_s": round(t, 1), "cuts_per_min": round(60 * n / span, 1) if span > 0 else 0})
        t += window
    return {
        "shots": len(shots),
        "mean_shot_s": round(statistics.mean(lengths), 3) if lengths else 0,
        "median_shot_s": round(statistics.median(lengths), 3) if lengths else 0,
        "shortest_s": min(lengths) if lengths else 0,
        "longest_s": max(lengths) if lengths else 0,
        "cuts_per_min": round(60 * len(cuts) / duration, 1) if duration else 0,
        "pace_by_window": pace,
    }


def analyse(path, fps=30, threshold=None, min_shot=0.2, gradual=False):
    """threshold=None: the adaptive detector; a number: a plain ffmpeg scene threshold.
    gradual=True: a dissolve or fade also ends a shot (at its middle); otherwise it is only listed."""
    info = probe(path)
    cuts = detect_cuts_adaptive(path) if threshold is None else detect_cuts(path, threshold)
    transitions = detect_transitions(path, info["fps"] or fps)
    if gradual:
        cuts = list(cuts) + [round((t["start_s"] + t["end_s"]) / 2, 3) for t in transitions]
    shots = build_shots(cuts, info["duration"], fps, min_shot)
    return {
        "source": os.path.basename(path),
        "source_fps": info["fps"],
        "size": [info["width"], info["height"]],
        "fps": fps,
        "duration_s": round(info["duration"], 3),
        "total_frames": round(info["duration"] * fps),
        "threshold": threshold,
        "shots": shots,
        "transitions": transitions,
        "gradual_as_cuts": gradual,
        "stats": stats(shots, info["duration"]),
    }


# ---------- outputs ----------

def thumbs(path, sheet, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for s in sheet["shots"]:
        t = (s["start_s"] + s["end_s"]) / 2
        _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
              "-vf", "scale=320:-2", os.path.join(out_dir, f"shot_{s['index']:03d}.jpg")])


def to_tsx(sheet):
    st = sheet["stats"]
    rows = "\n".join(
        f"  {{ index: {s['index']}, from: {s['from_frame']}, durationInFrames: {s['duration_frames']}, "
        f"startS: {s['start_s']}, endS: {s['end_s']} }}," for s in sheet["shots"])
    return f"""// Generated by cut-sheet from {sheet['source']}: {st['shots']} shots, mean {st['mean_shot_s']} s,
// {st['cuts_per_min']} cuts/min. Timings are the reference's; the content is yours.
import React from "react";
import {{ AbsoluteFill, Series }} from "remotion";

export const FPS = {sheet['fps']};
export const TOTAL_FRAMES = {sheet['total_frames']};

export type Shot = {{ index: number; from: number; durationInFrames: number; startS: number; endS: number }};

export const shots: Shot[] = [
{rows}
];

const Placeholder: React.FC<{{ shot: Shot }}> = ({{ shot }}) => (
  <AbsoluteFill
    style={{{{
      justifyContent: "center",
      alignItems: "center",
      backgroundColor: shot.index % 2 ? "#1d1d22" : "#2a2a31",
      color: "#e8b04a",
      fontSize: 96,
      fontFamily: "monospace",
    }}}}
  >
    {{`shot ${{shot.index}} · ${{(shot.endS - shot.startS).toFixed(2)}} s`}}
  </AbsoluteFill>
);

/** One sequence per shot of the reference. Pass renderShot to put your own footage in each slot. */
export const CutSheet: React.FC<{{ renderShot?: (shot: Shot) => React.ReactNode }}> = ({{ renderShot }}) => (
  <Series>
    {{shots.map((shot) => (
      <Series.Sequence key={{shot.index}} durationInFrames={{shot.durationInFrames}} premountFor={{FPS}}>
        {{renderShot ? renderShot(shot) : <Placeholder shot={{shot}} />}}
      </Series.Sequence>
    ))}}
  </Series>
);
"""


# ---------- CLI ----------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="cut-sheet", description=__doc__.splitlines()[0])
    ap.add_argument("video")
    ap.add_argument("-o", "--out", default=".", help="output folder (default: here)")
    ap.add_argument("--fps", type=int, default=30, help="frame rate of your Remotion composition (default 30)")
    ap.add_argument("-t", "--threshold", type=float, default=None,
                    help="use a plain scene threshold 0..1 instead of the adaptive detector (e.g. 0.3)")
    ap.add_argument("--min-shot", type=float, default=0.2, help="merge 'shots' shorter than this, in s (default 0.2)")
    ap.add_argument("--thumbs", action="store_true", help="save a thumbnail per shot")
    ap.add_argument("--gradual", action="store_true", help="a dissolve or fade also ends a shot (at its middle)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    try:
        sheet = analyse(a.video, a.fps, a.threshold, a.min_shot, a.gradual)
        os.makedirs(a.out, exist_ok=True)
        stem = os.path.splitext(os.path.basename(a.video))[0]
        with open(os.path.join(a.out, f"{stem}.cuts.json"), "w", encoding="utf-8") as f:
            json.dump(sheet, f, indent=2)
        with open(os.path.join(a.out, f"{stem}.cuts.tsx"), "w", encoding="utf-8") as f:
            f.write(to_tsx(sheet))
        if a.thumbs:
            thumbs(a.video, sheet, os.path.join(a.out, f"{stem}.thumbs"))
    except (RuntimeError, OSError, ValueError) as e:
        print(f"cut-sheet: {e}", file=sys.stderr)
        return 2
    st = sheet["stats"]
    print(f"{sheet['source']}: {sheet['duration_s']} s, {st['shots']} shots, mean {st['mean_shot_s']} s "
          f"(median {st['median_shot_s']}, {st['shortest_s']}..{st['longest_s']}), {st['cuts_per_min']} cuts/min")
    for s in sheet["shots"][:40]:
        print(f"  {s['index']:>3}  {s['start_s']:8.3f} -> {s['end_s']:8.3f}  {s['duration_s']:6.3f} s  "
              f"{s['duration_frames']:>5} f")
    if len(sheet["shots"]) > 40:
        print(f"  ... {len(sheet['shots']) - 40} more in the JSON")
    tr = sheet["transitions"]
    if tr and not a.gradual:      # said loudly: "1 shot" from a cut built on dissolves would look like success
        spans = ", ".join(f"{t['start_s']:.2f}-{t['end_s']:.2f}" for t in tr[:10])
        print(f"cut-sheet: {len(tr)} gradual transition(s) (dissolve or fade) at {spans} s are listed in the JSON, "
              f"not counted as cuts; --gradual counts them", file=sys.stderr)
    print(f"wrote {stem}.cuts.json, {stem}.cuts.tsx" + (f", {stem}.thumbs/" if a.thumbs else "") + f" in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
