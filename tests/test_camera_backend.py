import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from admet.engines.acquisition.camera import (
    Camera,
    CameraAcquisitionThread,
    PypylonUnavailableError,
    VideoWorker,
)


class FakeParam:
    def __init__(self, value=None, minimum=None, maximum=None, inc=None, symbolics=None):
        self.Value = value
        self.Min = minimum
        self.Max = maximum
        self.Inc = inc
        self.Symbolics = symbolics

    def SetValue(self, value):
        self.Value = value

    def Execute(self):
        self.Value = "executed"


class FakeTransportDevice:
    def GetModelName(self):
        return "Basler Test"

    def GetSerialNumber(self):
        return "123"


class FakeTransportLayer:
    def GetFriendlyName(self):
        return "Basler Camera Emulator"

    def GetDeviceClass(self):
        return "BaslerCamEmu"


class FakeGrabResult:
    def __init__(self, frame):
        self.frame = frame
        self.released = False

    def GrabSucceeded(self):
        return True

    def GetArray(self):
        return self.frame

    def Release(self):
        self.released = True


class FakeInstantCamera:
    def __init__(self, device):
        self.transport_device = device
        self.opened = False
        self.grabbing = False
        self.strategy = None
        self.frame = np.array([[1, 2], [3, 4]], dtype=np.uint8)
        self.UserSetSelector = FakeParam()
        self.UserSetLoad = FakeParam()
        self.DeviceLinkThroughputLimitMode = FakeParam("On")
        self.MaxNumBuffer = FakeParam(10)
        self.ExposureAuto = FakeParam("Continuous")
        self.GainAuto = FakeParam("Continuous")
        self.BalanceWhiteAuto = FakeParam("Continuous")
        self.ResultingFrameRate = FakeParam(120.0)

    def Open(self):
        self.opened = True

    def IsOpen(self):
        return self.opened

    def Close(self):
        self.opened = False

    def GetDeviceInfo(self):
        return self.transport_device

    def IsGrabbing(self):
        return self.grabbing

    def StartGrabbing(self, strategy):
        self.strategy = strategy
        self.grabbing = True

    def StopGrabbing(self):
        self.grabbing = False

    def RetrieveResult(self, timeout_ms, timeout_handling):
        return FakeGrabResult(self.frame)


class FakeTlFactory:
    def __init__(self):
        self.devices = [FakeTransportDevice()]

    def EnumerateDevices(self):
        return list(self.devices)

    def CreateDevice(self, device):
        return device

    def EnumerateTls(self):
        return [FakeTransportLayer()]


class FakePylon:
    GrabStrategy_LatestImageOnly = "latest"
    GrabStrategy_OneByOne = "one_by_one"
    TimeoutHandling_Return = "return"

    class TlFactory:
        factory = FakeTlFactory()

        @classmethod
        def GetInstance(cls):
            return cls.factory

    InstantCamera = FakeInstantCamera


class EmptyFakePylon:
    GrabStrategy_LatestImageOnly = "latest"
    GrabStrategy_OneByOne = "one_by_one"
    TimeoutHandling_Return = "return"

    class TlFactory:
        factory = FakeTlFactory()
        factory.devices = []

        @classmethod
        def GetInstance(cls):
            return cls.factory

    InstantCamera = FakeInstantCamera


class CameraTests(unittest.TestCase):
    def test_missing_pypylon_is_reported_lazily(self):
        camera = Camera()

        with patch("builtins.__import__", side_effect=ImportError("missing")):
            with self.assertRaises(PypylonUnavailableError):
                camera.enumerate_cameras()

    def test_open_apply_settings_and_grab_frame(self):
        camera = Camera(FakePylon)

        self.assertEqual(camera.enumerate_cameras(), ["Basler Test (123)"])
        self.assertTrue(camera.open())
        self.assertEqual(camera.device.ExposureAuto.Value, "Off")
        self.assertTrue(camera.apply_settings({"MaxNumBuffer": 25}))
        self.assertEqual(camera.get_parameter("MaxNumBuffer")["value"], 25)

        camera.start_grabbing(latest_only=False)
        self.assertEqual(camera.device.strategy, "one_by_one")
        self.assertTrue(np.array_equal(camera.grab_frame(), camera.device.frame))
        self.assertEqual(camera.get_resulting_framerate(), 120.0)

        camera.close()
        self.assertIsNone(camera.device)

    def test_preflight_treats_no_devices_as_refreshable_state(self):
        camera = Camera(EmptyFakePylon)

        status = camera.preflight()

        self.assertTrue(status.pypylon_available)
        self.assertTrue(status.refresh_ok)
        self.assertEqual(status.camera_count, 0)
        self.assertIn("refresh", status.message)

    def test_preflight_reports_transport_layers(self):
        camera = Camera(EmptyFakePylon)

        status = camera.preflight()

        self.assertIn("Basler Camera Emulator / BaslerCamEmu", status.transport_layers)


class VideoWorkerTests(unittest.TestCase):
    def test_writes_one_raw_file_and_encodes_it_once_every_frame_is_written(self):
        import json
        import threading

        encoded = {}

        def fake_encoder(raw, video, width, height, fps):
            encoded["bytes"] = raw.read_bytes()
            encoded["shape"] = (width, height, fps)
            video.write_bytes(b"AVI")
            return True, ""

        finished = threading.Event()
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = VideoWorker(tmpdir, "sample", 2, 2, 30.0, encoder=fake_encoder)
            worker.on_finished = lambda path, ok, reason: finished.set()
            self.assertTrue(worker.start())
            self.assertTrue(worker.write(np.array([[0, 256], [512, 1024]], dtype=np.uint16)))
            self.assertTrue(worker.write(np.array([[3, 3], [3, 3]], dtype=np.uint8)))
            path = Path(worker.stop())
            self.assertTrue(finished.wait(5))

            self.assertTrue(path.exists())
            self.assertEqual(encoded["bytes"], bytes([0, 1, 2, 4, 3, 3, 3, 3]))
            self.assertEqual(encoded["shape"], (2, 2, 30.0))
            self.assertFalse((Path(tmpdir) / "sample.raw").exists())          # gone once the video is whole
            info = json.loads((Path(tmpdir) / "sample.video.json").read_text())
            self.assertEqual((info["status"], info["frames_written"], info["frames_dropped"]), ("done", 2, 0))

    def test_a_recording_left_unencoded_is_finished_later_with_its_whole_frames(self):
        import json

        from admet.engines.acquisition.camera.video import encode_leftovers

        seen = {}

        def fake_encoder(raw, video, width, height, fps):
            seen["size"] = raw.stat().st_size
            video.write_bytes(b"AVI")
            return True, ""

        with tempfile.TemporaryDirectory() as tmpdir:
            camera = Path(tmpdir)
            (camera / "cut.raw").write_bytes(bytes(4 * 3 + 2))                 # three whole 2x2 frames, then half
            (camera / "cut.video.json").write_text(json.dumps(
                {"video": "cut.avi", "raw": "cut.raw", "width": 2, "height": 2, "fps": 24.0, "status": "recording"}))
            done = encode_leftovers(camera, encoder=fake_encoder)

            self.assertEqual(done, [str(camera / "cut.avi")])
            self.assertEqual(seen["size"], 12)
            info = json.loads((camera / "cut.video.json").read_text())
            self.assertEqual((info["status"], info["frames_written"]), ("done", 3))
            self.assertEqual(encode_leftovers(camera, encoder=fake_encoder), [])   # nothing left


class FakeRecordingCamera:
    def __init__(self):
        self.strategies = []
        self.grabbing = False

    def start_grabbing(self, *, latest_only=True):
        self.strategies.append(latest_only)
        self.grabbing = True

    def stop_grabbing(self):
        self.grabbing = False

    def grab_frame(self, timeout_ms=5):
        return np.ones((2, 2), dtype=np.uint8)


class FakeWriter:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.frames = []
        self.frame_count = 0

    def start(self):
        self.started = True
        return True

    def write(self, frame):
        self.frames.append(frame.copy())
        self.frame_count += 1
        return True

    def stop(self):
        self.stopped = True
        return "video.avi"


class CameraAcquisitionThreadTests(unittest.TestCase):
    def test_recording_writes_frames_and_switches_grab_strategy(self):
        camera = FakeRecordingCamera()
        writer = FakeWriter()
        previews = []
        complete_calls = []
        acquisition = CameraAcquisitionThread(camera, preview_callback=previews.append)
        acquisition.set_recording_complete_callback(lambda: complete_calls.append(True))

        self.assertTrue(acquisition.start_recording(writer, max_frames=2))
        acquisition.process_frame(np.ones((2, 2), dtype=np.uint8))
        acquisition.frame_processed()
        acquisition.process_frame(np.ones((2, 2), dtype=np.uint8))

        self.assertFalse(acquisition.recording)
        self.assertTrue(writer.started)
        self.assertTrue(writer.stopped)
        self.assertEqual(len(writer.frames), 2)
        self.assertEqual(previews[0].shape, (2, 2))
        self.assertEqual(camera.strategies, [False, True])
        self.assertEqual(complete_calls, [True])
        self.assertEqual(acquisition.last_recording_frames, 2)
        self.assertEqual(acquisition.last_writer_frame_count, 2)

    def test_preview_can_be_disabled(self):
        previews = []
        acquisition = CameraAcquisitionThread(
            FakeRecordingCamera(),
            preview_callback=previews.append,
        )
        acquisition.set_preview_enabled(False)

        acquisition.process_frame(np.ones((2, 2), dtype=np.uint8))

        self.assertEqual(previews, [])


if __name__ == "__main__":
    unittest.main()
