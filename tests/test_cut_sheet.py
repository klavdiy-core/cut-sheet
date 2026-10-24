import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cut_sheet  # noqa: E402

SHOTS = [1.5, 0.8, 2.2, 1.0]          # seconds; cuts at 1.5, 2.3, 4.5; total 5.5
COLORS = ["red", "blue", "white", "green"]


def make_video(path, shots=SHOTS, colors=COLORS, fps=25, noise=True):
    """Solid-colour shots of known length, with grain so it is not a trivially clean signal."""
    inputs, filters = [], []
    for i, (d, c) in enumerate(zip(shots, colors)):
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=320x180:r={fps}:d={d}"]
        filters.append(f"[{i}:v]" + ("noise=alls=12:allf=t," if noise else "") + f"format=yuv420p[v{i}]")
    graph = ";".join(filters) + ";" + "".join(f"[v{i}]" for i in range(len(shots))) + \
        f"concat=n={len(shots)}:v=1:a=0[out]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path], check=True)


class Pure(unittest.TestCase):
    def test_parses_showinfo(self):
        log = ("[Parsed_showinfo_1 @ 0x1] n:   0 pts:  37 pts_time:1.48    duration:1 fmt:yuv420p\n"
               "[Parsed_showinfo_1 @ 0x1] n:   1 pts:  57 pts_time:2.28 duration:1\n"
               "something else\n")
        self.assertEqual(cut_sheet.parse_showinfo(log), [1.48, 2.28])

    def test_frames_add_up_exactly(self):
        shots = cut_sheet.build_shots([0.37, 1.71, 2.33, 4.9], duration=6.13, fps=30)
        self.assertEqual(sum(s["duration_frames"] for s in shots), round(6.13 * 30))
        self.assertEqual(shots[0]["from_frame"], 0)
        for a, b in zip(shots, shots[1:]):
            self.assertEqual(a["from_frame"] + a["duration_frames"], b["from_frame"])

    def test_short_flashes_are_merged(self):
        shots = cut_sheet.build_shots([1.0, 1.05, 3.0], duration=4.0, fps=30, min_shot=0.25)
        self.assertEqual([round(s["start_s"], 2) for s in shots], [0.0, 1.0, 3.0])

    def test_stats(self):
        st = cut_sheet.stats(cut_sheet.build_shots([2.0, 3.0], duration=6.0, fps=30), duration=6.0)
        self.assertEqual(st["shots"], 3)
        self.assertAlmostEqual(st["mean_shot_s"], 2.0)
        self.assertAlmostEqual(st["cuts_per_min"], 20.0)


class OnVideo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = os.path.join(cls.tmp.name, "ref.mp4")
        make_video(cls.video)

    def test_finds_the_cuts_within_a_frame(self):
        sheet = cut_sheet.analyse(self.video, fps=25)
        cuts = [s["start_s"] for s in sheet["shots"][1:]]
        self.assertEqual(len(cuts), 3, cuts)
        for got, want in zip(cuts, [1.5, 2.3, 4.5]):
            self.assertLessEqual(abs(got - want), 1 / 25 + 1e-6, cuts)
        self.assertAlmostEqual(sheet["duration_s"], 5.5, delta=0.05)

    def test_target_fps_changes_frames_not_seconds(self):
        sheet = cut_sheet.analyse(self.video, fps=60)
        self.assertEqual(sum(s["duration_frames"] for s in sheet["shots"]), round(sheet["duration_s"] * 60))
        self.assertEqual(sheet["fps"], 60)

    def test_cli_writes_json_and_ts(self):
        out = os.path.join(self.tmp.name, "out")
        code = cut_sheet.main([self.video, "-o", out, "--fps", "30", "--thumbs"])
        self.assertEqual(code, 0)
        with open(os.path.join(out, "ref.cuts.json")) as f:
            data = json.load(f)
        self.assertEqual(len(data["shots"]), 4)
        with open(os.path.join(out, "ref.cuts.tsx"), encoding="utf-8") as f:
            ts = f.read()
        self.assertIn("Series.Sequence", ts)
        self.assertIn("premountFor", ts)
        first = data["shots"][0]["duration_frames"]
        self.assertLessEqual(abs(first - 45), 1)       # 1.5 s at 30 fps (ffmpeg's color source rounds up to 38/25 s)
        self.assertIn(f"durationInFrames: {first},", ts)
        thumbs = sorted(os.listdir(os.path.join(out, "ref.thumbs")))
        self.assertEqual(len(thumbs), 4)

    def test_low_contrast_cuts_are_found(self):
        # old faded film: neighbouring shots differ less than a fixed 0.3 scene threshold can see
        v = os.path.join(self.tmp.name, "lowc.mp4")
        make_video(v, shots=[1.6, 1.6, 1.6], colors=["0x404040", "0x806050", "0x405080"])
        self.assertEqual(cut_sheet.detect_cuts(v, 0.3), [])           # what a plain threshold misses
        cuts = cut_sheet.analyse(v, fps=25)["shots"][1:]
        self.assertEqual(len(cuts), 2)
        for got, want in zip([s["start_s"] for s in cuts], [1.6, 3.2]):
            self.assertLessEqual(abs(got - want), 0.08)

    def test_real_film_against_eye_labelled_cuts(self):
        # 10 s of "Third Avenue El" (Carson Davidson, 1950s), Prelinger Archives, item ThirdAve1950, stated Public
        # Domain. Every cut and every non-cut peak below was labelled by looking at the frames on both sides.
        v = os.path.join(os.path.dirname(__file__), "fixtures", "ThirdAve1950_excerpt1.mp4")
        cuts = [0.93, 2.67, 3.44, 6.34, 7.74, 9.01]
        not_cuts = [1.17, 2.17, 6.01]      # planks and a hand moving inside one shot: scene score 0.16-0.25
        found = [s["start_s"] for s in cut_sheet.analyse(v, fps=30)["shots"][1:]]
        for c in cuts:
            self.assertTrue(any(abs(c - f) < 0.1 for f in found), (c, found))
        for c in not_cuts:
            self.assertFalse(any(abs(c - f) < 0.1 for f in found), (c, found))
        plain = cut_sheet.detect_cuts(v, 0.3)
        self.assertLess(len(plain), len(cuts))   # why the adaptive detector exists

    def test_real_film_cuts_that_keep_the_colours(self):
        # 12 s later in the same film: two cuts between shots of the same grey street, so the colour histogram barely
        # moves; only "the peak stands far above its neighbours" catches them. The other peaks are one long shot of
        # the train passing houses.
        v = os.path.join(os.path.dirname(__file__), "fixtures", "ThirdAve1950_excerpt2.mp4")
        found = [s["start_s"] for s in cut_sheet.analyse(v, fps=30)["shots"][1:]]
        self.assertEqual(len(found), 2, found)
        for c, f in zip([2.24, 10.94], found):
            self.assertLess(abs(c - f), 0.1, found)

    def test_a_zoom_is_not_a_cut(self):
        v = os.path.join(self.tmp.name, "zoom.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=s=320x180:r=25", "-t", "4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", v], check=True)
        self.assertEqual(len(cut_sheet.analyse(v, fps=25)["shots"]), 1)

    def test_no_cuts_is_one_shot(self):
        one = os.path.join(self.tmp.name, "one.mp4")
        make_video(one, shots=[2.0], colors=["red"])
        sheet = cut_sheet.analyse(one, fps=30)
        self.assertEqual(len(sheet["shots"]), 1)


if __name__ == "__main__":
    unittest.main()
