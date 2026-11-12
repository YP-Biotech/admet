"""Per-step video: recorded only while a record step runs, closed on every interruption, capped."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from admet.engines.acquisition.pipeline import RECORD_CAP_S
from admet.engines.acquisition.step_video import StepVideo
from tests.test_control_engine import FakeCameraAcquisition, FakeVideoWriter


def step(name, record=False, record_max_s=None):
    return SimpleNamespace(name=name, record=record, record_max_s=record_max_s)


def event(index, outcome="running", state="running", name="s", gate=""):
    return SimpleNamespace(current_step=index, outcome=outcome, state=state, step_name=name,
                           confirmation_message=gate)


class StepVideoTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.camera = FakeCameraAcquisition()
        self.failures = []
        self.limits = []
        start = self.camera.start_recording

        def start_and_note_limit(writer, *, max_frames=None, max_time=None):
            self.limits.append(max_time)
            return start(writer, max_frames=max_frames, max_time=max_time)

        self.camera.start_recording = start_and_note_limit

    def video(self, steps):
        return StepVideo(steps, self.camera, video_dir=self.dir, label="run_ab12", frame_size=(16, 12), fps=24.0,
                         writer_factory=FakeVideoWriter, on_failure=self.failures.append)

    def test_only_record_steps_record_and_never_at_their_gate(self):
        video = self.video([step("prime"), step("collect", record=True)])
        video.on_event(event(0))
        self.assertFalse(self.camera.recording)
        video.on_event(event(0, "completed"))
        video.on_event(event(1, gate="Fresh collection in place?"))       # waiting at the gate
        self.assertFalse(self.camera.recording)
        video.on_event(event(1))                                            # the step starts
        self.assertTrue(self.camera.recording)
        video.on_event(event(1, "completed"))

        self.assertFalse(self.camera.recording)
        self.assertEqual([(v["step"], v["part"], v["stopped_by"]) for v in video.videos], [(2, 1, "completed")])
        self.assertTrue(Path(video.videos[0]["video_path"]).name.startswith("run_ab12_step02_"))
        self.assertTrue(Path(video.videos[0]["video_path"]).is_file())

    def test_pause_closes_the_file_and_resuming_opens_a_second_part(self):
        video = self.video([step("collect", record=True)])
        video.on_event(event(0))
        video.on_event(event(0, state="paused"))
        self.assertFalse(self.camera.recording)
        video.on_event(event(0))                                            # resumed
        self.assertTrue(self.camera.recording)
        video.on_event(event(0, "cancelled"))

        self.assertEqual([(v["part"], v["stopped_by"]) for v in video.videos], [(1, "paused"), (2, "cancelled")])
        self.assertIn("_part2_", video.videos[1]["video_path"])

    def test_every_way_a_run_ends_closes_the_video(self):
        for ending in (event(0, "skipped"), event(0, "error"), event(-1, "cancelled", name="")):
            video = self.video([step("collect", record=True)])
            video.on_event(event(0))
            video.on_event(ending)
            self.assertFalse(self.camera.recording, ending)
        video = self.video([step("collect", record=True)])
        video.on_event(event(0))
        video.close("emergency stop")
        self.assertEqual(video.videos[-1]["stopped_by"], "emergency stop")
        self.assertFalse(self.camera.recording)

    def test_a_step_never_records_past_its_limit(self):
        self.assertEqual(self.video([step("a", record=True, record_max_s=500)]).limit(0), RECORD_CAP_S)
        video = self.video([step("collect", record=True, record_max_s=0.6)])
        video.on_event(event(0))
        self.assertAlmostEqual(self.limits[-1], 0.6)
        time.sleep(0.65)
        video.on_event(event(0))                                            # the protocol's tick checks it too
        self.assertFalse(self.camera.recording)
        self.assertEqual(video.videos[-1]["stopped_by"], "limit")
        video.on_event(event(0, state="paused"))
        video.on_event(event(0))                                            # the limit is used up: no new part
        self.assertFalse(self.camera.recording)
        self.assertEqual(len(video.videos), 1)

    def test_the_camera_stopping_at_its_limit_is_recorded(self):
        video = self.video([step("collect", record=True)])
        video.on_event(event(0))
        self.camera.stop_recording()
        self.camera.callback()                                              # the camera closed the file itself
        self.assertEqual(video.videos[-1]["stopped_by"], "limit")

    def test_a_camera_that_cannot_start_pauses_the_protocol(self):
        video = self.video([step("collect", record=True)])
        self.camera.start_recording = lambda writer, **_limits: False
        video.on_event(event(0))

        self.assertEqual(len(self.failures), 1)
        self.assertEqual(video.videos[-1]["stopped_by"], "camera failed to start")



class RecordFieldTests(unittest.TestCase):
    def test_record_fields_are_checked(self):
        from admet.workflows.json_protocol import normalize

        def document(**fields):
            return {"name": "t", "steps": [{"sensor_setpoints": {"0": 10}, "trigger_type": "time",
                                            "trigger_params": {"duration_s": 1}, **fields}]}

        self.assertEqual(normalize(document(record=True, record_max_s=30))["steps"][0]["record_max_s"], 30)
        for fields in ({"record": "yes"}, {"record": True, "record_max_s": 150}, {"record_max_s": 30},
                       {"record": True, "record_max_s": 0}):
            with self.assertRaises(ValueError, msg=fields):
                normalize(document(**fields))


if __name__ == "__main__":
    unittest.main()
