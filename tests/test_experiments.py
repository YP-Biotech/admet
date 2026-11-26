"""An experiment designed as a table of runs, each run and each sample with an id short enough to type."""

import json
import tempfile
import unittest
from pathlib import Path

from admet.core.experiments import (ALPHABET, Experiment, ExperimentStore, find_sample_id, new_code,
                                    sample_id)
from admet.workflows.json_protocol import normalize, resolve, run_fields

DESIGN = {"experiment": "ratio sweep 26 Nov", "replicates": 2,
          "runs": [{"oil": 300, "cells": 40, "beads": 40},
                   {"oil": 250, "cells": 67, "beads": 67, "stop_oil_ul": 120, "stop_s": 90,
                    "condition": "boundary", "note": "near the bulge"},
                   {"mode": "pressure", "oil": 1300, "cells": 200, "beads": 200, "stop_s": 60, "video": True}]}


class IdTests(unittest.TestCase):
    def test_a_code_is_three_characters_nothing_can_be_mistaken_for(self):
        codes = {new_code() for _ in range(200)}
        self.assertTrue(all(len(code) == 3 and set(code) <= set(ALPHABET) for code in codes))
        self.assertFalse(set("".join(codes)) & set("01OIL"))
        self.assertNotIn(new_code(set(codes)), codes)

    def test_a_samples_id_is_found_in_a_folders_name_however_it_was_typed(self):
        for text in ("K7Q-03-r2", "K7Q-03-r2_set101.2026-11-26-14-03-11", "k7q_03_rep2 cells", "K7Q03r2",
                     "set101 K7Q.03.r2", "K7Q-003-r2"):
            self.assertEqual(find_sample_id(text), "K7Q-03-r2", text)
        self.assertEqual(find_sample_id("K7Q-12-r10"), "K7Q-12-r10")
        for text in ("set101.2026-11-26-14-03-11", "XK7Q-03-r2", "K7Q-3-r2", "K0Q-03-r2", "K7Q-03-r2345", ""):
            self.assertIsNone(find_sample_id(text), text)
        self.assertEqual(sample_id("K7Q-03", 2), "K7Q-03-r2")


class ExperimentTests(unittest.TestCase):
    def test_a_design_becomes_runs_with_ids_their_values_and_conditions(self):
        experiment = Experiment.from_json(DESIGN)
        code = experiment.code
        runs = experiment.runs
        self.assertEqual([run.run_id(code) for run in runs], [f"{code}-01", f"{code}-02", f"{code}-03"])
        self.assertEqual([run.condition_text() for run in runs], ["300/40/40", "boundary", "1300/200/200 mbar"])
        self.assertEqual([run.replicates for run in runs], [2, 2, 2])                  # the experiment's value
        self.assertEqual([(run.stop_oil_ul, run.stop_total_ul, run.stop_s) for run in runs],
                         [(150, None, None), (120, None, 90), (None, None, 60)])       # naming a stop sets them all
        self.assertEqual(runs[1].note, "near the bulge")

    def test_a_row_runs_its_replicates_each_ended_by_its_first_stop(self):
        experiment = Experiment.from_json(DESIGN)
        code = experiment.code
        one, two, three = (experiment.document(run) for run in experiment.runs)
        self.assertEqual([step["name"] for step in one["steps"]], [f"{code}-01 rep01", f"{code}-01 rep02"])
        self.assertEqual(one["steps"][0]["sensor_setpoints"], {"0": 300, "1": 40, "2": 40})
        self.assertEqual((one["steps"][0]["trigger_type"], one["steps"][0]["timeout_s"]), ("volume", 30 + 3 * 30))
        self.assertIn(f"sample {code}-01-r2", one["steps"][1]["confirm_message"])
        self.assertEqual(two["steps"][0]["trigger_type"], "any")
        self.assertEqual([stop["trigger_type"] for stop in two["steps"][0]["trigger_params"]["stops"]],
                         ["volume", "time"])
        self.assertEqual(two["steps"][0]["timeout_s"], 120)
        self.assertEqual(two["run"], {"name": f"{code}-02", "id": f"{code}-02", "condition": "boundary",
                                      "experiment": "ratio sweep 26 Nov"})
        self.assertEqual(three["steps"][0]["pressure_setpoints"], {"0": 1300, "1": 200, "2": 200})
        self.assertEqual((three["steps"][0]["record"], three["steps"][0]["record_max_s"]), (True, 100))
        for document in (one, two, three):
            normalize(resolve(document))                                               # the protocol accepts each
            self.assertEqual(run_fields(document["run"]), document["run"])

    def test_a_row_that_cannot_run_is_refused_and_names_the_run(self):
        experiment = Experiment("K7Q")
        run = experiment.add()
        for values, message in (({"mode": "vacuum"}, "mode"), ({"oil": -1}, "below 0"), ({"oil": None}, "number"),
                                ({"stop_oil_ul": None}, "needs a stop"), ({"replicates": 0}, "replicates"),
                                ({"replicates": 2.5}, "replicates"), ({"video_s": 500}, "video_s"),
                                ({"speed": 3}, "no speed")):
            with self.assertRaisesRegex(ValueError, message):
                run.set(**values)
        self.assertEqual(run.values(), Experiment("K7Q").add().values())                # nothing changed
        with self.assertRaisesRegex(ValueError, "run 2: replicates"):
            Experiment.from_json({"runs": [{}, {"replicates": 0}]})
        with self.assertRaisesRegex(ValueError, "run 1: no protocol"):
            Experiment.from_json({"runs": [{"protocol": "dropseq_3n"}]})

    def test_a_new_row_is_like_the_last_one(self):
        experiment = Experiment("K7Q")
        experiment.add(oil=250, stop_s=30, replicates=1)
        second = experiment.add(cells=80, condition="more cells")
        self.assertEqual((second.oil, second.cells, second.stop_s, second.replicates), (250, 80, 30, 1))
        self.assertEqual(second.condition, "more cells")

    def test_the_table_comes_back_as_it_went_out_and_runs_move_on(self):
        experiment = Experiment.from_json(DESIGN)
        first, second = (run.run_id(experiment.code) for run in experiment.runs[:2])
        experiment.mark(first, "done", "20261126_140000_" + first.lower())
        self.assertEqual(experiment.next_planned().run_id(experiment.code), second)
        again = Experiment.from_json(json.loads(json.dumps(experiment.to_json())))
        self.assertEqual(again.to_json(), experiment.to_json())
        with self.assertRaises(ValueError):
            experiment.mark(first, "lost")
        with self.assertRaises(LookupError):
            experiment.mark("ZZZ-99", "done")

    def test_the_store_keeps_each_experiment_under_its_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(tmp)
            experiment = Experiment.from_json(DESIGN, taken=store.codes())
            store.save(experiment)
            self.assertTrue((Path(tmp) / "control.db").is_file())
            self.assertEqual(store.load(experiment.code).to_json(), experiment.to_json())
            with self.assertRaises(LookupError):
                store.load("ZZZ")
            self.assertEqual(store.list()[0]["code"], experiment.code)
            self.assertEqual((store.list()[0]["runs"], store.list()[0]["done"]), (3, 0))
            self.assertEqual(store.codes(), {experiment.code})


class RunBlockTests(unittest.TestCase):
    def test_the_run_block_takes_an_experiments_id_and_name_and_refuses_another_id(self):
        self.assertEqual(run_fields({"name": "K7Q-03", "id": "K7Q-03", "experiment": "sweep"}),
                         {"name": "K7Q-03", "id": "K7Q-03", "experiment": "sweep"})
        with self.assertRaisesRegex(ValueError, "like K7Q-03"):
            run_fields({"id": "run three"})

    def test_a_plan_carries_the_runs_id_and_experiment_into_its_record(self):
        from admet.core.service import Admet

        experiment = Experiment.from_json(DESIGN)
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet(home=tmp)
            plan = admet.plan_protocol("run_json_protocol", {"protocol": experiment.document(experiment.runs[0])})
            admet.experiments.save(experiment)
            self.assertEqual(admet.experiments.codes(), {experiment.code})
        self.assertEqual((plan["run_code"], plan["experiment"], plan["tag"], plan["expected_duration_s"]),
                         (f"{experiment.code}-01", "ratio sweep 26 Nov", f"{experiment.code}-01", 60))


if __name__ == "__main__":
    unittest.main()
