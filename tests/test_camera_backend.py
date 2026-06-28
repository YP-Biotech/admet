import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from admet.engines.control.camera import Camera, PypylonUnavailableError, VideoWorker


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


class VideoWorkerTests(unittest.TestCase):
    def test_writes_raw_frames_and_uses_injected_encoder(self):
        encoded = {}

        def fake_encoder(frames_dir, video_path, width, height, fps):
            raw_frames = sorted(frames_dir.glob("*.raw"))
            encoded["count"] = len(raw_frames)
            encoded["first_bytes"] = raw_frames[0].read_bytes()
            encoded["shape"] = (width, height, fps)
            video_path.write_bytes(b"AVI")
            shutil.rmtree(frames_dir)
            return str(video_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            worker = VideoWorker(tmpdir, "sample", 2, 2, 30.0, encoder=fake_encoder)
            self.assertTrue(worker.start())
            self.assertTrue(worker.write(np.array([[0, 256], [512, 1024]], dtype=np.uint16)))
            path = Path(worker.stop())

            self.assertTrue(path.exists())
            self.assertEqual(encoded["count"], 1)
            self.assertEqual(encoded["first_bytes"], bytes([0, 1, 2, 4]))
            self.assertEqual(encoded["shape"], (2, 2, 30.0))


if __name__ == "__main__":
    unittest.main()
