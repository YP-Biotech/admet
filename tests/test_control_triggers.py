import unittest

from admet.engines.control.pipeline import (
    ConditionTrigger,
    ConfirmationTrigger,
    PipelineStep,
    StepStatus,
    ThresholdTrigger,
    TimeTrigger,
    VolumeTrigger,
    create_trigger,
)


class TimeTriggerTests(unittest.TestCase):
    def test_not_triggered_immediately(self):
        trigger = TimeTrigger(duration_s=10.0)
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))

    def test_triggered_after_duration(self):
        trigger = TimeTrigger(duration_s=0.0)
        trigger.reset()
        self.assertTrue(trigger.check(lambda _: 0.0, lambda _: 0.0))

    def test_progress(self):
        trigger = TimeTrigger(duration_s=1.0)
        trigger.reset()
        self.assertGreaterEqual(trigger.progress(), 0.0)
        self.assertLessEqual(trigger.progress(), 1.0)

    def test_description(self):
        self.assertIn("30", TimeTrigger(duration_s=30.0).description())


class VolumeTriggerTests(unittest.TestCase):
    def test_not_triggered_at_zero(self):
        trigger = VolumeTrigger(sensor_index=0, target_volume_ul=10.0)
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))

    def test_triggered_at_target(self):
        trigger = VolumeTrigger(sensor_index=0, target_volume_ul=10.0)
        trigger.reset()
        trigger.check(lambda _: 0.0, lambda _: 0.0)
        self.assertTrue(trigger.check(lambda _: 0.0, lambda _: 10.0))

    def test_progress_tracking(self):
        trigger = VolumeTrigger(sensor_index=0, target_volume_ul=100.0)
        trigger.reset()
        trigger.check(lambda _: 0.0, lambda _: 0.0)
        trigger.check(lambda _: 0.0, lambda _: 50.0)
        self.assertAlmostEqual(trigger.progress(), 0.5)

    def test_description(self):
        description = VolumeTrigger(sensor_index=0, target_volume_ul=75.0).description()
        self.assertIn("75", description)
        self.assertIn("sensor 0", description)


class ThresholdTriggerTests(unittest.TestCase):
    def test_not_triggered_out_of_range(self):
        trigger = ThresholdTrigger(
            sensor_index=0,
            target=100.0,
            tolerance_pct=5.0,
            stable_duration_s=1.0,
        )
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))

    def test_not_triggered_immediately_in_range(self):
        trigger = ThresholdTrigger(
            sensor_index=0,
            target=100.0,
            tolerance_pct=5.0,
            stable_duration_s=10.0,
        )
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 100.0, lambda _: 0.0))

    def test_triggered_with_zero_stable_duration(self):
        trigger = ThresholdTrigger(
            sensor_index=0,
            target=100.0,
            tolerance_pct=5.0,
            stable_duration_s=0.0,
        )
        trigger.reset()
        self.assertTrue(trigger.check(lambda _: 100.0, lambda _: 0.0))

    def test_description(self):
        self.assertIn("100.0", ThresholdTrigger(sensor_index=0, target=100.0).description())


class ConditionTriggerTests(unittest.TestCase):
    def test_min_value_not_met(self):
        trigger = ConditionTrigger(sensor_index=0, min_value=50.0)
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 10.0, lambda _: 0.0))

    def test_min_value_met(self):
        trigger = ConditionTrigger(sensor_index=0, min_value=50.0)
        trigger.reset()
        self.assertTrue(trigger.check(lambda _: 60.0, lambda _: 0.0))

    def test_max_value_not_met(self):
        trigger = ConditionTrigger(sensor_index=0, max_value=50.0)
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 60.0, lambda _: 0.0))

    def test_max_value_met(self):
        trigger = ConditionTrigger(sensor_index=0, max_value=50.0)
        trigger.reset()
        self.assertTrue(trigger.check(lambda _: 40.0, lambda _: 0.0))

    def test_range(self):
        trigger = ConditionTrigger(sensor_index=0, min_value=10.0, max_value=50.0)
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 5.0, lambda _: 0.0))
        trigger.reset()
        self.assertTrue(trigger.check(lambda _: 30.0, lambda _: 0.0))

    def test_stays_triggered(self):
        trigger = ConditionTrigger(sensor_index=0, min_value=50.0)
        trigger.reset()
        trigger.check(lambda _: 60.0, lambda _: 0.0)
        self.assertTrue(trigger.check(lambda _: 10.0, lambda _: 0.0))

    def test_progress(self):
        trigger = ConditionTrigger(sensor_index=0, min_value=50.0)
        trigger.reset()
        self.assertEqual(trigger.progress(), 0.0)
        trigger.check(lambda _: 60.0, lambda _: 0.0)
        self.assertEqual(trigger.progress(), 1.0)


class ConfirmationTriggerTests(unittest.TestCase):
    def test_blocks_until_confirmed(self):
        trigger = ConfirmationTrigger(message="Proceed?")
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))
        trigger.confirm()
        self.assertTrue(trigger.check(lambda _: 0.0, lambda _: 0.0))

    def test_message(self):
        self.assertEqual(ConfirmationTrigger(message="Ready?").message, "Ready?")

    def test_reset_clears(self):
        trigger = ConfirmationTrigger(message="Go?")
        trigger.confirm()
        self.assertTrue(trigger.check(lambda _: 0.0, lambda _: 0.0))
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))


class CreateTriggerTests(unittest.TestCase):
    def test_supported_types(self):
        self.assertIsInstance(create_trigger("time", {"duration_s": 5.0}), TimeTrigger)
        self.assertIsInstance(
            create_trigger("volume", {"sensor_index": 0, "target_volume_ul": 10.0}),
            VolumeTrigger,
        )
        self.assertIsInstance(
            create_trigger("threshold", {"sensor_index": 0, "target": 100.0}),
            ThresholdTrigger,
        )
        self.assertIsInstance(
            create_trigger("condition", {"sensor_index": 0, "min_value": 50.0}),
            ConditionTrigger,
        )
        self.assertIsInstance(
            create_trigger("confirmation", {"message": "OK?"}),
            ConfirmationTrigger,
        )

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            create_trigger("invalid", {})


class PipelineStepTests(unittest.TestCase):
    def test_defaults(self):
        step = PipelineStep("prime", {0: 10.0}, TimeTrigger(0.0))

        self.assertEqual(step.status, StepStatus.PENDING)
        self.assertEqual(step.on_complete, "hold")
        self.assertEqual(step.confirm_message, "")
        self.assertEqual(step.error_msg, "")


if __name__ == "__main__":
    unittest.main()
