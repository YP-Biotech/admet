import unittest

from admet.engines.acquisition.pipeline import (
    PipelineStep,
    StepStatus,
)
from admet.engines.acquisition.triggers import (
    ConditionTrigger,
    ConfirmationTrigger,
    ThresholdTrigger,
    TimeTrigger,
    VolumeTrigger,
    create_trigger,
)


class VolumeModeTests(unittest.TestCase):
    """integral stops at the target; adaptive stops early by the expected tail."""

    def run_until_stop(self, trigger, flow=60.0, step_ul=0.05, flows=None):
        trigger.reset()
        volume = 0.0
        readings = iter(flows) if flows is not None else None
        while True:
            reading = next(readings, flow) if readings is not None else flow
            if trigger.check(lambda _sensor: reading, lambda _sensor: volume):
                return volume
            volume += step_ul

    def test_integral_stops_at_the_target(self):
        stopped = self.run_until_stop(VolumeTrigger(0, 10.0, mode="integral", tail_s=0.7))

        self.assertAlmostEqual(stopped, 10.0, delta=0.05)

    def test_adaptive_stops_early_by_the_expected_tail(self):
        # 0.7 s of flow at 60 uL/min still arrives after the stop: 0.7 uL.
        stopped = self.run_until_stop(VolumeTrigger(0, 10.0, mode="adaptive", tail_s=0.7))

        self.assertAlmostEqual(stopped, 10.0 - 0.7, delta=0.05)

    def test_adaptive_uses_the_learned_tail_when_none_is_fixed(self):
        trigger = VolumeTrigger(0, 10.0)
        trigger.tail_source = lambda: 0.7

        self.assertAlmostEqual(self.run_until_stop(trigger), 9.3, delta=0.05)
        self.assertEqual(trigger.tail_used_s, 0.7)

    def test_adaptive_without_a_tail_behaves_as_integral(self):
        # The first run on a channel: nothing learned yet.
        self.assertAlmostEqual(self.run_until_stop(VolumeTrigger(0, 10.0)), 10.0, delta=0.05)

    def test_adaptive_is_the_default(self):
        self.assertEqual(VolumeTrigger(0, 10.0).mode, "adaptive")

    def test_one_misread_does_not_stop_the_step_early(self):
        # A 5000 uL/min misread would predict a huge tail; the median ignores it.
        flows = [60.0] * 50 + [5000.0] + [60.0] * 500
        stopped = self.run_until_stop(VolumeTrigger(0, 10.0, tail_s=0.7), flows=flows)

        self.assertAlmostEqual(stopped, 9.3, delta=0.05)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            VolumeTrigger(0, 10.0, mode="setpoints")


class TailTrackerTests(unittest.TestCase):
    def test_k_is_the_median_of_recent_stops(self):
        from admet.engines.acquisition.engine import TailTracker

        tracker = TailTracker(keep=3)
        self.assertIsNone(tracker.k(1))
        for k in (0.5, 0.9, 0.7, 5.0):
            tracker.record(1, k)

        self.assertEqual(tracker.k(1), 0.9)          # 0.9, 0.7, 5.0 kept
        self.assertEqual(tracker.describe(1)["tail_samples"], 3)


class TriggerBehaviorTests(unittest.TestCase):
    def test_time_volume_and_threshold_triggers_progress_to_completion(self):
        time_trigger = TimeTrigger(duration_s=0.0)
        time_trigger.reset()
        self.assertTrue(time_trigger.check(lambda _: 0.0, lambda _: 0.0))
        self.assertEqual(time_trigger.progress(), 1.0)
        self.assertIn("0", time_trigger.description())

        volume_trigger = VolumeTrigger(sensor_index=0, target_volume_ul=100.0)
        volume_trigger.reset()
        self.assertFalse(volume_trigger.check(lambda _: 0.0, lambda _: 10.0))
        self.assertTrue(volume_trigger.check(lambda _: 0.0, lambda _: 110.0))
        self.assertAlmostEqual(volume_trigger.progress(), 1.0)
        self.assertIn("sensor 0", volume_trigger.description())

        threshold_trigger = ThresholdTrigger(
            sensor_index=0,
            target=100.0,
            tolerance_pct=5.0,
            stable_duration_s=0.0,
        )
        threshold_trigger.reset()
        self.assertFalse(threshold_trigger.check(lambda _: 0.0, lambda _: 0.0))
        self.assertTrue(threshold_trigger.check(lambda _: 100.0, lambda _: 0.0))
        self.assertEqual(threshold_trigger.progress(), 1.0)

    def test_condition_trigger_handles_bounds_and_latches(self):
        trigger = ConditionTrigger(sensor_index=0, min_value=10.0, max_value=50.0)
        trigger.reset()

        self.assertFalse(trigger.check(lambda _: 5.0, lambda _: 0.0))
        self.assertFalse(trigger.check(lambda _: 60.0, lambda _: 0.0))
        self.assertTrue(trigger.check(lambda _: 30.0, lambda _: 0.0))
        self.assertTrue(trigger.check(lambda _: 5.0, lambda _: 0.0))
        self.assertEqual(trigger.progress(), 1.0)

    def test_confirmation_trigger_blocks_until_confirmed_and_resets(self):
        trigger = ConfirmationTrigger(message="Proceed?")
        trigger.reset()

        self.assertEqual(trigger.message, "Proceed?")
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))
        trigger.confirm()
        self.assertTrue(trigger.check(lambda _: 0.0, lambda _: 0.0))
        self.assertEqual(trigger.progress(), 1.0)
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))

    def test_create_trigger_maps_protocol_specs(self):
        specs = (
            ("time", {"duration_s": 5.0}, TimeTrigger),
            ("volume", {"sensor_index": 0, "target_volume_ul": 10.0}, VolumeTrigger),
            ("threshold", {"sensor_index": 0, "target": 100.0}, ThresholdTrigger),
            ("condition", {"sensor_index": 0, "min_value": 50.0}, ConditionTrigger),
            ("confirmation", {"message": "OK?"}, ConfirmationTrigger),
        )

        for trigger_type, params, expected_type in specs:
            with self.subTest(trigger_type=trigger_type):
                self.assertIsInstance(create_trigger(trigger_type, params), expected_type)
        with self.assertRaises(ValueError):
            create_trigger("invalid", {})

    def test_pipeline_step_defaults_match_engine_contract(self):
        step = PipelineStep("run_priming", {0: 10.0}, TimeTrigger(0.0))

        self.assertEqual(step.status, StepStatus.PENDING)
        self.assertEqual(step.on_complete, "hold")
        self.assertEqual(step.confirm_message, "")
        self.assertEqual(step.error_msg, "")


if __name__ == "__main__":
    unittest.main()


class SeveralStopsTests(unittest.TestCase):
    """any ends a step on the first of its stops; total_volume counts the channels together."""

    @staticmethod
    def drive(trigger, ticks, flows, step_ul):
        trigger.reset()
        volumes = {sensor: 0.0 for sensor in flows}
        for tick in range(ticks):
            if trigger.check(lambda sensor: flows[sensor], lambda sensor: volumes[sensor]):
                return tick
            for sensor in volumes:
                volumes[sensor] += step_ul[sensor]
        return None

    def test_total_volume_stops_on_what_the_channels_delivered_together(self):
        trigger = create_trigger("total_volume", {"sensors": [0, 1, 2], "target_volume_ul": 10.0})
        self.assertEqual(self.drive(trigger, 100, {0: 60, 1: 6, 2: 6}, {0: 1.0, 1: 0.5, 2: 0.5}), 5)
        self.assertEqual(trigger.progress(), 1.0)
        with self.assertRaisesRegex(ValueError, "sensors"):
            create_trigger("total_volume", {"sensors": 0, "target_volume_ul": 10.0})

    def test_the_first_stop_met_ends_the_step_and_says_which_it_was(self):
        stops = [{"trigger_type": "volume", "trigger_params": {"sensor_index": 0, "target_volume_ul": 50.0,
                                                               "mode": "integral"}},
                 {"trigger_type": "total_volume", "trigger_params": {"sensors": [0, 1], "target_volume_ul": 30.0}}]
        trigger = create_trigger("any", {"stops": stops})
        self.assertEqual(self.drive(trigger, 100, {0: 60, 1: 60}, {0: 1.0, 1: 1.0}), 15)
        self.assertIs(trigger.ended_by, trigger.stops[1])
        self.assertIn(" or ", trigger.description())
        stops[0]["trigger_params"]["target_volume_ul"] = 20.0
        trigger = create_trigger("any", {"stops": stops})
        self.assertEqual(self.drive(trigger, 100, {0: 60, 1: 60}, {0: 2.0, 1: 0.5}), 10)
        self.assertIs(trigger.ended_by, trigger.stops[0])

    def test_stops_are_checked_when_made(self):
        for stops, message in (([], "at least one"),
                               ([{"trigger_type": "any", "trigger_params": {"stops": []}}], "trigger_type"),
                               ([{"trigger_type": "confirmation", "trigger_params": {"message": "x"}}], "trigger_type"),
                               ([{"trigger_type": "time", "trigger_params": {}}], "needs duration_s"),
                               ([{"trigger_type": "time", "trigger_params": {"duration_s": 1, "x": 2}}], "takes")):
            with self.assertRaisesRegex(ValueError, message):
                create_trigger("any", {"stops": stops})
