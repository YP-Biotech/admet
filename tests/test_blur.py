"""A droplet moving while the shutter is open reads longer along the flow; by how much, found on the droplets."""

import unittest

import numpy as np

from admet.engines.opencv.blur import blur_px, droplet_share, extent, measure_share, smear_matrix, unsmear


def ring_profile(length=50, band=7, depth=30.0, pad=20):
    profile = np.zeros(length + 2 * pad)
    profile[pad:pad + band] = depth
    profile[pad + length - band:pad + length] = depth
    return profile


def smeared_frame(blur, x0=120, depth=60.0, radius=24, band=6, height=60, width=320):
    """An empty channel with one ring droplet, smeared BLUR px along x as a moving one is."""
    yy, xx = np.mgrid[:height, :width]
    still = np.where(np.abs(np.hypot(xx - x0, yy - 30) - (radius - band / 2)) < band / 2, depth, 0.0)
    if blur < 1:
        return np.clip(210 - still, 0, 255).astype(np.uint8)
    whole = int(blur)
    shifts = [still[:, :width - s] for s in range(whole + 1)]
    smear = np.zeros_like(still)
    for s, shifted in enumerate(shifts):
        smear[:, s:] += shifted * (1.0 if s < whole else blur - whole)
    smear /= whole + (blur - whole)
    return np.clip(210 - smear, 0, 255).astype(np.uint8)


class BlurTests(unittest.TestCase):
    def test_the_smear_is_the_step_a_frame_times_the_share_of_it_the_shutter_is_open(self):
        self.assertAlmostEqual(blur_px(44.5, 59.0, 4000.0), 10.502, places=3)         # the rig on 17 Nov
        self.assertEqual(blur_px(44.5, 59.0, 0.0), 0.0)
        self.assertEqual(blur_px(None, 59.0, 4000.0), 0.0)

    def test_unsmearing_gives_back_the_still_length_within_a_pixel(self):
        rng = np.random.default_rng(3)
        errors = []
        for blur in (5.0, 8.0, 10.5):
            for band, depth in ((7, 30.0), (9, 45.0)):
                still = ring_profile(band=band, depth=depth)
                seen = smear_matrix(len(still), blur) @ still + rng.normal(0, 1.0, len(still) + int(np.ceil(blur)) - 1)
                errors.append(extent(unsmear(seen, blur), 7.9) - extent(still, 7.9))
        self.assertLess(abs(float(np.mean(errors))), 1.0)
        self.assertLessEqual(max(abs(e) for e in errors), 2)

    def test_the_share_measured_on_smeared_droplets_is_what_the_threshold_lets_through(self):
        from admet.engines.opencv.detection import detect

        background = np.full((60, 320), 210, np.uint8)
        frames = [smeared_frame(10.0, x0=x0) for x0 in range(90, 230, 4)]
        measured = measure_share(frames, background, {}, 10.0)
        still = detect(smeared_frame(0.0), background, {})["properties"][0]["bbox_width"]
        seen = detect(frames[0], background, {})["properties"][0]["bbox_width"]
        self.assertAlmostEqual(measured["share"], (seen - still) / 10.0, delta=0.15)
        self.assertGreaterEqual(measured["droplets"], 20)
        self.assertIsNone(droplet_share(np.zeros((60, 320)), {"bbox_x": 100, "bbox_width": 50, "centroid_y": 30}, 0.5, 8.0))
        self.assertIsNone(measure_share(frames[:3], background, {}, 10.0))           # too few droplets to say


if __name__ == "__main__":
    unittest.main()
