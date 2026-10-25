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

    def test_a_cut_with_the_same_picture_on_both_sides_is_dropped(self):
        # an outside re-grade (25.10), checked by eye on a Prelinger film: haze and a plane turning inside one shot
        # were reported as cuts; the frames 4 apart across them differ by 5-14, real cuts here by 29 and more
        a, b = bytes([40] * 100), bytes([200] * 100)
        frames = [a] * 20 + [b] * 20
        same = [a] * 40
        self.assertEqual(cut_sheet.confirm_cuts([20 / 25], frames, 25), [0.8])
        self.assertEqual(cut_sheet.confirm_cuts([20 / 25], same, 25), [])
        near = [bytes([40 + i // 8] * 100) for i in range(40)]           # a slow drift: about 1 grey level apart
        self.assertEqual(cut_sheet.confirm_cuts([20 / 25], near, 25), [])

    def test_cut_strength_measures_the_picture_change_across_a_cut(self):
        a, b = bytes([40] * 100), bytes([200] * 100)
        self.assertEqual(cut_sheet.cut_strength(0.8, [a] * 20 + [b] * 20, 25), 160.0)
        self.assertEqual(cut_sheet.cut_strength(0.8, [a] * 40, 25), 0.0)

    def test_overlapping_transitions_are_merged(self):
        self.assertEqual(cut_sheet.merge_spans([(10, 20), (18, 30), (40, 50)]), [(10, 30), (40, 50)])
        self.assertEqual(cut_sheet.merge_spans([(40, 50), (10, 20)]), [(10, 20), (40, 50)])

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
        # every cut says how sure it is (an outside re-grade, 25.10: "which of 31 cuts do I trust?")
        for s in sheet["shots"][1:]:
            self.assertEqual(s["cut"], "hard")
            self.assertGreater(s["cut_strength"], 0)
            self.assertIn(s["confidence"], ("high", "low"))
        self.assertNotIn("cut", sheet["shots"][0])                  # the first shot starts at 0, not at a cut

    def test_low_confidence_cuts_are_listed_in_the_summary(self):
        sheet = {"source": "x.mp4", "duration_s": 10.0, "transitions": [],
                 "shots": [{"index": 1, "start_s": 0.0, "end_s": 4.0, "duration_s": 4.0, "duration_frames": 120},
                           {"index": 2, "start_s": 4.0, "end_s": 6.07, "duration_s": 2.07, "duration_frames": 62,
                            "cut": "hard", "cut_strength": 24.1, "confidence": "low"},
                           {"index": 3, "start_s": 6.07, "end_s": 10.0, "duration_s": 3.93, "duration_frames": 118,
                            "cut": "hard", "cut_strength": 88.0, "confidence": "high"}]}
        line = cut_sheet.summary(sheet, gradual=False)
        self.assertIn("3 shots", line)
        self.assertIn("1 cut to check by eye: 4.00 s", line)

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


FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


class Gradual(unittest.TestCase):
    """Found by an independent tester, 24.10: a reference cut with dissolves came back as '1 shot' with no warning."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        a, b = os.path.join(FIX, "ThirdAve1950_excerpt1.mp4"), os.path.join(FIX, "ThirdAve1950_excerpt2.mp4")
        # real dissolves: two excerpts of NASA's "Orion EFT1 Animation No Narration" (archive.org, CC0), 60 fps.
        # Labelled by eye on frames every 0.1 s: the launch pad dissolves into the rocket in the sky over ~1.0-1.6 s,
        # and space into the capsule over the Earth over ~1.65-2.3 s.
        cls.dissolve = os.path.join(FIX, "OrionEft1_dissolve2.mp4")
        cls.dissolve1 = os.path.join(FIX, "OrionEft1_dissolve1.mp4")
        # a fade to black over 4.0-5.0 s, black, and a fade back in over 5.5-6.5 s, on real footage
        cls.fade = os.path.join(d, "fade.mp4")
        ff("-i", a, "-filter_complex",
           "[0:v]split[p][q];"
           "[p]trim=0:5.5,setpts=PTS-STARTPTS,fps=30,fade=t=out:st=4:d=1[x];"
           "[q]trim=3.5:8.5,setpts=PTS-STARTPTS,fps=30,fade=t=in:st=0:d=1[y];[x][y]concat=n=2:v=1[out]",
           "-map", "[out]", "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.fade)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_real_dissolves_are_listed_with_their_span(self):
        for path, (lo, hi) in ((self.dissolve1, (1.0, 1.6)), (self.dissolve, (1.65, 2.3))):
            tr = cut_sheet.analyse(path, fps=30)["transitions"]
            self.assertEqual(len(tr), 1, (path, tr))
            self.assertTrue(lo - 0.4 <= tr[0]["start_s"] <= lo and hi <= tr[0]["end_s"] <= hi + 0.8, (path, tr))

    def test_one_dissolve_is_reported_once(self):
        # OrionEft1_dissolve3 (41.5-48 s of the same film): the globe dissolves into the capsule over ~2.0-3.6 s;
        # a stray run of 2 frames near its end must not become a second, overlapping transition
        tr = cut_sheet.analyse(os.path.join(FIX, "OrionEft1_dissolve3.mp4"), fps=30)["transitions"]
        self.assertEqual(len(tr), 1, tr)

    def test_a_glow_that_fades_back_is_not_a_transition(self):
        # OrionEft1_dissolve1 2.5-3.4 s: a haze brightens around the rocket and fades back to the same shot
        tr = cut_sheet.analyse(self.dissolve1, fps=30)["transitions"]
        self.assertFalse(any(t["start_s"] > 2.2 for t in tr), tr)

    def test_a_fade_through_black_is_listed(self):
        tr = cut_sheet.analyse(self.fade, fps=30)["transitions"]
        self.assertTrue(any(3.8 <= t["start_s"] <= 4.4 for t in tr), tr)

    def test_hard_cut_film_has_no_transitions(self):
        for name in ("ThirdAve1950_excerpt1.mp4", "ThirdAve1950_excerpt2.mp4"):
            self.assertEqual(cut_sheet.analyse(os.path.join(FIX, name), fps=30)["transitions"], [], name)

    def test_a_zoom_is_not_a_transition(self):
        v = os.path.join(self.tmp.name, "zoom.mp4")
        ff("-f", "lavfi", "-i", "mandelbrot=s=320x180:r=25", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", v)
        self.assertEqual(cut_sheet.analyse(v, fps=25)["transitions"], [])

    def test_gradual_turns_transitions_into_cuts(self):
        sheet = cut_sheet.analyse(self.dissolve, fps=30, gradual=True)
        self.assertTrue(any(1.6 <= s["start_s"] <= 2.4 for s in sheet["shots"]), sheet["shots"])

    def test_the_cli_warns_when_only_transitions_were_found(self):
        out = os.path.join(self.tmp.name, "o")
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "cut_sheet.py"), self.fade,
                            "-o", out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gradual", r.stderr)
        # and in the summary line on stdout, for whoever pipes stdout or ignores stderr (an outside re-grade, 25.10),
        # in words a reader gets at once: "1 shot", not "1 shots", and what the transitions are
        first = r.stdout.splitlines()[0]
        self.assertRegex(first, r"\d+ shots? \+ ")
        self.assertNotRegex(first, r"\b1 shots\b")
        self.assertEqual(cut_sheet._n(1, "shot", "shots"), "1 shot")
        self.assertRegex(first, r"\d+ dissolves?/fades? not counted as cuts \(--gradual counts them\)")

    def test_gradual_cuts_are_marked_as_such(self):
        sheet = cut_sheet.analyse(self.dissolve, fps=30, gradual=True)
        self.assertTrue(any(s.get("cut") == "gradual" for s in sheet["shots"][1:]), sheet["shots"])


if __name__ == "__main__":
    unittest.main()
