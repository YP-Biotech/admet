import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from admet.engines.acquisition.camera.bench import STAGES, report_lines, run_stage


class NumberedCamera:
    """Hands over frames as fast as asked, numbered and stamped with the time it hands them over, in
    nanoseconds as a Basler camera stamps; every 100th number never arrives, as when pylon's buffers
    overflow."""

    def __init__(self):
        self.number = 0
        self.frame = np.full((60, 320), 128, dtype=np.uint8)

    def start_grabbing(self, *, latest_only=True):
        pass

    def stop_grabbing(self):
        pass

    def grab(self, timeout_ms=5):
        self.number += 2 if self.number % 100 == 99 else 1
        return self.frame.copy(), time.perf_counter_ns(), self.number


class UnnumberedCamera(NumberedCamera):
    """A camera whose frames come without usable numbers or stamps: the same ones every time."""

    def grab(self, timeout_ms=5):
        return self.frame.copy(), 0, 0


class CameraBenchTests(unittest.TestCase):
    def test_each_stage_counts_every_hand_over_from_the_camera_to_the_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            for stage in STAGES:
                result = run_stage(NumberedCamera(), stage, 0.3, Path(folder), fps_hint=0.0, width=320, height=60)
                self.assertEqual(result.made, result.reached_python + result.camera_skipped)
                self.assertGreater(result.camera_skipped, 0)
                self.assertTrue(result.numbered)
                self.assertAlmostEqual(result.camera_fps, result.made / result.seconds, delta=0.1 * result.camera_fps)
                self.assertEqual(result.taken, result.written + result.writer_dropped)
                self.assertEqual(result.lost, result.made - result.written)
                self.assertGreater(result.grab_us_p99, 0.0)
            self.assertEqual(list(Path(folder).iterdir()), [])            # the test recordings are gone

    def test_the_report_says_where_the_frames_went(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(NumberedCamera(), "grab", 0.2, Path(folder), fps_hint=0.0)
        lines = report_lines([result])
        self.assertTrue(lines[1].startswith("grab"))
        self.assertTrue(any("never reached Python" in line for line in lines))
        self.assertFalse(any("disk fell behind" in line for line in lines))


class UnnumberedBenchTests(unittest.TestCase):
    def test_without_the_cameras_numbers_the_rate_is_the_pcs_and_nothing_is_counted_as_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(UnnumberedCamera(), "grab", 0.2, Path(folder), fps_hint=4000.0)
        self.assertFalse(result.numbered)
        self.assertEqual((result.made, result.camera_skipped), (result.reached_python, 0))
        self.assertAlmostEqual(result.camera_fps, result.reached_python / result.seconds, delta=0.05 * result.camera_fps)
        self.assertTrue(any("no usable frame numbers" in line for line in report_lines([result])))
        self.assertTrue(report_lines([result])[-1].endswith("that the PC can see"))

    def test_the_folder_for_the_test_recordings_is_made_when_it_is_missing(self):
        from types import SimpleNamespace

        from admet.engines.acquisition.camera.bench import bench

        camera = NumberedCamera()
        camera.get_resulting_framerate = lambda: 4000.0
        controller = SimpleNamespace(camera=camera, connect=lambda settings: {"camera_connect_ok": True},
                                     recording_frame_size=lambda settings: (320, 60), disconnect=lambda: None)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "not" / "there"
            results = bench({}, seconds=0.1, out_dir=out, stages=("disk",), controller=controller)
            self.assertTrue(out.is_dir())
            self.assertEqual(list(out.iterdir()), [])                                   # its test files gone again
        self.assertEqual(results[0].stage, "disk")


class SystemCheckTests(unittest.TestCase):
    def test_the_disk_is_timed_on_a_file_it_then_removes_and_the_memory_is_read(self):
        from admet.engines.acquisition.camera.bench import disk_speed, memory_gb

        with tempfile.TemporaryDirectory() as tmp:
            speed = disk_speed(Path(tmp) / "out", 0.004, chunk_mb=1)
            self.assertGreater(speed, 0)
            self.assertEqual(list((Path(tmp) / "out").iterdir()), [])
        total, free = memory_gb()
        self.assertGreater(total, 0)
        self.assertTrue(free is None or 0 < free <= total)

    def test_a_disk_slower_than_the_camera_says_how_much_memory_a_minute_needs(self):
        from admet.engines.acquisition.camera.bench import SystemCheck, system_lines

        slow = SystemCheck(ram_total_gb=16.0, ram_free_gb=10.0, disk_mb_s=60.0, disk_gb=4.0, needed_mb_s=74.0,
                           folder="D:\\data")
        self.assertAlmostEqual(slow.ram_seconds, 10 * 1024 / 74)
        self.assertEqual(slow.behind_mb_s, 14.0)
        lines = system_lines(slow)
        self.assertIn("RAM 16.0 GB, 10.0 GB free: holds 138 s of recording", lines)
        self.assertTrue(any("falls 14 MB/s behind" in line and "0.8 GB" in line for line in lines))
        fast = SystemCheck(ram_total_gb=16.0, ram_free_gb=None, disk_mb_s=900.0, disk_gb=4.0, needed_mb_s=74.0,
                           folder="D:\\data")
        self.assertIn("the disk keeps up with the camera", system_lines(fast))
        self.assertIn("RAM 16.0 GB", system_lines(fast))
        self.assertIn("disk not measured", system_lines(SystemCheck(16.0, None, 0.0, 0.0, 74.0, ".")))


if __name__ == "__main__":
    unittest.main()


class ModelCameraTests(unittest.TestCase):
    """A camera modelled in software: the rig's rate on any computer, frames lost as pylon loses them."""

    def test_it_makes_frames_at_the_rate_given_numbered_and_timed_like_the_camera(self):
        from admet.engines.acquisition.camera.model import ModelCamera

        camera = ModelCamera(2000.0, 320, 60)
        camera.start_grabbing()
        grabbed, started = [], time.perf_counter()
        while time.perf_counter() - started < 0.3:
            frame = camera.grab()
            if frame is not None:
                grabbed.append(frame)
        numbers = [number for _frame, _ticks, number in grabbed]
        self.assertEqual(numbers, list(range(len(numbers))))                    # every one, in order
        self.assertAlmostEqual(len(numbers) / 0.3, 2000.0, delta=200.0)
        ticks = [ticks for _frame, ticks, _number in grabbed]
        self.assertEqual(ticks[1] - ticks[0], 500_000)                          # 0.5 ms apart, in ns
        self.assertEqual(grabbed[0][0].shape, (60, 320))
        self.assertEqual(camera.lost, 0)

    def test_python_falling_a_buffer_behind_loses_the_oldest_frames_and_the_numbers_show_it(self):
        from admet.engines.acquisition.camera.model import ModelCamera

        camera = ModelCamera(4000.0, buffer_frames=100)
        camera.start_grabbing()
        time.sleep(0.2)                                                         # 800 made, 100 kept
        _frame, _ticks, number = camera.grab()
        self.assertGreater(number, 600)
        self.assertEqual(camera.lost, number)

    def test_the_bench_runs_on_the_modelled_camera_and_keeps_up(self):
        from admet.engines.acquisition.camera.bench import bench
        from admet.engines.acquisition.camera.model import ModelCamera, ModelController

        with tempfile.TemporaryDirectory() as tmp:
            results = bench({}, seconds=0.5, out_dir=tmp, stages=("disk",),
                            controller=ModelController(ModelCamera(1000.0)))
        result, = results
        self.assertTrue(result.numbered)
        self.assertAlmostEqual(result.camera_fps, 1000.0, delta=50.0)
        self.assertEqual((result.camera_skipped, result.writer_dropped, result.lost), (0, 0, 0))

    def test_frames_come_from_a_recording_when_one_is_given(self):
        import cv2

        from admet.engines.acquisition.camera.model import ModelCamera

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 24, (32, 8), False)
            for value in (10, 20, 30):
                writer.write(np.full((8, 32), value, np.uint8))
            writer.release()
            camera = ModelCamera(100.0, video=path)
            self.assertEqual([int(frame[0, 0]) for frame in camera.frames], [10, 20, 30])
            self.assertEqual((camera.width, camera.height), (32, 8))
            with self.assertRaises(ValueError):
                ModelCamera(100.0, video=Path(tmp) / "nothing.avi")
