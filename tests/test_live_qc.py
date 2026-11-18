"""Live QC from bursts: the engine's measurements without a tracker, with intervals and a state."""

import math
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from admet.engines.acquisition.camera.qc import (
    BurstQC,
    QCSummary,
    QCWorker,
    cv_interval,
    qc_log_path,
    interval,
    qc_as_dict,
    qc_line,
    qc_state,
)

try:
    import cv2
except ModuleNotFoundError:
    cv2 = None


def train_burst(start_frame: int, *, frames=8, speed=48.0, spacing=128.0, radius=22, width=320, height=60,
                seed=0) -> list[np.ndarray]:
    """Consecutive frames of a monodisperse train as the rig's camera sees it: bright channel, dark rims."""
    rng = np.random.default_rng(seed + start_frame)
    burst = []
    for index in range(start_frame, start_frame + frames):
        frame = np.full((height, width), 157, dtype=np.float32)
        frame[:4, :] = 100
        frame[-4:, :] = 100
        offset = (index * speed) % spacing
        for x in np.arange(offset - spacing, width + spacing, spacing):
            cv2.circle(frame, (int(round(x)), height // 2), radius, 105, 3)
        burst.append(np.clip(frame + rng.normal(0, 2.0, frame.shape), 0, 255).astype(np.uint8))
    return burst


class IntervalTests(unittest.TestCase):
    def test_the_mean_and_its_interval(self):
        self.assertEqual(interval([]), (0.0, math.inf))
        self.assertEqual(interval([3.0]), (3.0, math.inf))
        mean, half = interval([10.0, 12.0, 11.0, 13.0])
        self.assertEqual(mean, 11.5)
        self.assertAlmostEqual(half, 1.96 * 1.118 / 2, places=2)

    def test_the_cv_and_its_interval_shrink_with_the_sample(self):
        cv, half = cv_interval([100.0, 104.0, 96.0, 100.0] * 25)         # 100 values, CV 2.83 %
        self.assertAlmostEqual(cv, 2.83, places=2)
        self.assertAlmostEqual(half, 1.96 * cv / math.sqrt(200), places=3)
        self.assertEqual(cv_interval([100.0])[1], math.inf)

    def test_the_state_follows_the_cv_interval_against_the_limit(self):
        self.assertEqual(qc_state(3.0, 0.5, 100, 5), "STABLE")
        self.assertEqual(qc_state(7.0, 0.5, 100, 5), "UNSTABLE")
        self.assertEqual(qc_state(4.8, 0.5, 100, 5), "SETTLING")            # straddles 5 %
        self.assertEqual(qc_state(3.0, 0.5, 10, 5), "SETTLING")             # too few droplets
        self.assertEqual(qc_state(3.0, 0.5, 100, 2), "SETTLING")            # too few bursts
        self.assertEqual(qc_state(3.0, math.inf, 100, 5), "SETTLING")

    def test_the_line_is_compact_and_reads_a_dict_too(self):
        summary = QCSummary(at=0.0, rate_hz=1450.4, rate_half=40.2, count=21300.0, diameter=124.04, diameter_half=0.61,
                            diameter_unit="um", cv_percent=3.94, cv_half=0.38, n=212, bursts=9,
                            speed_px_frame=48.0, spacing_px=128.0, state="STABLE")
        self.assertEqual(qc_line(summary), "1450±40/s  21 300  d 124.0±0.6um  cv 3.9±0.4%  STABLE")
        as_dict = qc_as_dict(summary)
        self.assertEqual(qc_line(as_dict), qc_line(summary))
        early = QCSummary(at=0.0, rate_hz=0.0, rate_half=math.inf, count=0.0, diameter=54.0, diameter_half=math.inf,
                          diameter_unit="px", cv_percent=0.0, cv_half=math.inf, n=1, bursts=0,
                          speed_px_frame=0.0, spacing_px=0.0, state="SETTLING")
        self.assertEqual(qc_line(early), "d 54.0px  cv 0.0%  SETTLING")
        self.assertIsNone(qc_as_dict(early)["rate_half"])                  # no infinities in the metadata
        self.assertEqual(qc_line(None), "")


@unittest.skipIf(cv2 is None, "OpenCV is not installed")
class BurstMeasurementTests(unittest.TestCase):
    def test_bursts_of_a_steady_train_give_its_rate_size_and_cv_with_a_stable_state(self):
        qc = BurstQC({"min_area": 800}, microns_per_pixel=2.5)
        fps = 4000.0
        for k in range(8):
            sample = qc.analyse(train_burst(k * 4000), fps, at=float(k))
        summary = qc.summary(at=7.0)

        self.assertAlmostEqual(sample.speed_px_frame, 48.0, delta=1.0)
        self.assertAlmostEqual(sample.spacing_px, 128.0, delta=2.0)
        self.assertAlmostEqual(summary.rate_hz, 48 / 128 * fps, delta=40)            # 1500 a second
        self.assertLess(summary.rate_half, 40)
        self.assertAlmostEqual(summary.diameter, 2 * (22 + 3) * 2.5, delta=8)        # the rim's outer edge, in µm
        self.assertEqual(summary.diameter_unit, "um")
        self.assertLess(summary.cv_percent, 5.0)
        self.assertEqual(summary.state, "STABLE")
        self.assertGreater(summary.n, 30)
        self.assertAlmostEqual(summary.count, summary.rate_hz * 7, delta=summary.rate_hz * 0.5)

    def test_the_first_bursts_are_settling_and_the_count_restarts_with_the_log(self):
        qc = BurstQC({"min_area": 800})
        qc.analyse(train_burst(0), 4000.0, at=0.0)
        summary = qc.summary(at=0.0)
        self.assertEqual(summary.state, "SETTLING")
        self.assertEqual(summary.diameter_unit, "px")
        qc.analyse(train_burst(4000), 4000.0, at=1.0)
        self.assertGreater(qc.count, 0)
        qc.reset()                                                           # the next recording starts from nothing
        self.assertEqual((qc.count, len(qc.samples)), (0.0, 0))
        self.assertIsNone(qc.summary(at=2.0))


class WorkerTests(unittest.TestCase):
    def test_a_burst_offered_while_busy_is_dropped_and_summaries_reach_the_listener(self):
        class SlowQC:
            microns_per_pixel = 0.0

            def __init__(self):
                self.calls = []
                self.release = threading.Event()

            def analyse(self, frames, fps, at=None):
                self.calls.append(at)
                self.release.wait(2)

            def summary(self, at=None):
                return QCSummary(at=at or 0.0, rate_hz=1000.0, rate_half=10.0, count=5.0, diameter=50.0,
                                 diameter_half=1.0, diameter_unit="px", cv_percent=3.0, cv_half=0.3, n=40, bursts=4,
                                 speed_px_frame=48.0, spacing_px=128.0, state="STABLE")

            def reset(self):
                pass

        seen = []
        slow = SlowQC()
        worker = QCWorker(slow, on_summary=seen.append)
        try:
            worker.start_recording()
            self.assertTrue(worker.offer([np.zeros((2, 2), np.uint8)], 100.0, at=12.5))
            time.sleep(0.1)                                             # the worker is inside analyse now
            self.assertFalse(worker.offer([np.zeros((2, 2), np.uint8)], 100.0, at=13.5))
            self.assertEqual(worker.dropped, 1)
            slow.release.set()
            for _ in range(50):
                if seen:
                    break
                time.sleep(0.02)
            self.assertEqual(len(seen), 1)
            self.assertEqual((slow.calls, seen[0].at), ([12.5], 12.5))          # the burst's own time, kept
            worker.stop_recording()
            self.assertIsNone(worker.summary)                               # nothing shown between recordings
        finally:
            worker.stop()

    def test_the_old_qc_file_name_is_still_known_for_reading_old_runs(self):
        video = Path("/p/records/camera/run_a_step01_20261117_101500.avi")
        self.assertEqual(qc_log_path(video, {"run_id": "run_a"}), Path("/p/records/camera/run_a.qc.csv"))


class TimelineRowTests(unittest.TestCase):
    def test_a_qc_row_joins_the_fluidics_file_on_its_clock(self):
        import csv

        from admet.engines.acquisition.fluidics.csv_logger import CsvLogger

        with tempfile.TemporaryDirectory() as tmp:
            logger = CsvLogger(tmp, filename="run.csv")
            path = logger.start(3, 3, origin_monotonic=1000.0)
            logger.write_row("10:00:00.000", 5.0, [1300, 420, 410], [300, 40, 40], [10, 2, 2], [True, True, True])
            summary = QCSummary(at=1005.3, rate_hz=1170.0, rate_half=21.0, count=1234.0, diameter=124.0,
                                diameter_half=0.5, diameter_unit="um", cv_percent=2.7, cv_half=0.3, n=200, bursts=5,
                                speed_px_frame=48.0, spacing_px=128.0, state="STABLE")
            logger.write_qc(summary.at, {"recording_id": "run_a_step01", "step": 1, "part": 1}, summary)
            logger.write_row("10:00:00.100", 5.1, [1300, 420, 410], [300, 40, 40], [10, 2, 2], [True, True, True])
            logger.stop()
            rows = list(csv.DictReader(open(path, newline="")))

        self.assertEqual([r["elapsed_s"] for r in rows], ["5.000", "5.300", "5.100"])   # the QC row on the same clock
        qc = rows[1]
        self.assertEqual((qc["recording_id"], qc["step"], qc["state"], qc["rate_hz"]), ("run_a_step01", "1", "STABLE", "1170.00"))
        self.assertEqual((qc["flow_0_ul_min"], qc["pressure_0_mbar"]), ("", ""))         # fluidics columns empty
        self.assertEqual((rows[0]["state"], rows[0]["rate_hz"]), ("", ""))                # and the other way round
        self.assertRegex(qc["timestamp"], r"\d{2}:\d{2}:\d{2}\.\d{3}")


class PreviewLineTests(unittest.TestCase):
    def test_the_qc_line_is_painted_under_the_live_line_in_the_states_colour(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from admet.ui.control import QC_COLORS, PreviewDisplay

        app = QApplication.instance() or QApplication([])
        view = PreviewDisplay()
        view.resize(700, 220)
        view.set_frame(np.full((60, 320), 120, dtype=np.uint8))
        view.set_overlay("LIVE · 4032 fps")
        view.set_qc("1450±40/s  21 300  d 124.0±0.6um  cv 3.9±0.4%  STABLE", "STABLE")
        view.show()
        app.processEvents()
        image = view.grab().toImage()
        stable = QC_COLORS["STABLE"]
        top = view.frame_rect.y() + 60                                       # the band sits above the frame or on its top
        painted = any(image.pixelColor(x, y).green() > 180 and image.pixelColor(x, y).red() < 140
                      for y in range(0, top) for x in range(0, 300, 2))
        self.assertTrue(painted, "no pixel in the stable green in the band")
        self.assertEqual((stable.red(), stable.green(), stable.blue()), (90, 220, 130))
        view.close()


if __name__ == "__main__":
    unittest.main()
