"""An analysis batch in a process of its own: it reports progress, its result, and stops when asked."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from admet.workflows.analyze_process import AnalysisProcess, live, stop_all
from admet.workflows.analyze_runner import AnalyzeTarget


def _video(path: Path) -> Path:
    import cv2

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 24, (160, 60), False)
    for k in range(12):
        frame = np.full((60, 160), 200, np.uint8)
        for cx in range(10 + k * 3, 160, 30):
            cv2.circle(frame, (cx, 30), 9, 40, -1)
        writer.write(frame)
    writer.release()
    return path


SETTINGS = {"microns_per_pixel": 2.5, "fps": 700.0, "start_frame": 0, "end_frame": None, "k_sigma": 4.0,
            "satellite_px": 6, "min_area": 50.0, "skip_left_pct": 0.0, "skip_right_pct": 0.0, "gate_pct": 50.0}


class AnalysisProcessTests(unittest.TestCase):
    def test_a_batch_runs_in_its_own_process_and_reports_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = _video(Path(tmp) / "v.avi")
            run = AnalysisProcess((AnalyzeTarget(video, "opencv", "v", dict(SETTINGS)),), Path(tmp) / "results")

            run.start()
            self.assertEqual(live(), 1)
            self.assertTrue(run.wait(60))

            report = run.result()
            self.assertEqual([job.status for job in report.jobs], ["complete"])
            self.assertTrue((Path(report.jobs[0].result_dir) / "result.json").is_file())   # written by the child
            self.assertEqual(run.progress, 100.0)
            self.assertEqual(live(), 0)

    def test_a_run_asked_to_stop_leaves_its_files_unrun_and_an_error_comes_back_as_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = _video(Path(tmp) / "v.avi")
            run = AnalysisProcess((AnalyzeTarget(video, "opencv", "v", dict(SETTINGS)),) * 2, Path(tmp) / "results")
            run.after_file.set()                                     # asked before it begins
            run.start()
            stop_all()
            self.assertTrue(run.wait(60))
            self.assertEqual({job.status for job in run.result().jobs}, {"stopped"})
            broken = AnalysisProcess(("not a target",), Path(tmp) / "results")
            broken.start()
            self.assertTrue(broken.wait(60))
            with self.assertRaises(RuntimeError):
                broken.result()


if __name__ == "__main__":
    unittest.main()
