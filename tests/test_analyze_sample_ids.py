"""Analyze's files: a run of an experiment named by its sample's id, its labelled image folders joining it."""

import unittest
from importlib.util import find_spec


@unittest.skipUnless(find_spec("nicegui"), "the analyze extra is not installed")
class MatchByIdTests(unittest.TestCase):
    def rows(self):
        from admet.ui.analyze import MatrixRow

        video = MatrixRow(uid="v", source_path="/data/runs/r/K7Q-03_step02.avi", engine="opencv", sample_id="",
                          run="/data/runs/r", protocol="K7Q-03", run_time="2026-11-26 14:00", replicate=2,
                          condition="300/40/40", sample_code="K7Q-03-r2")
        plain = MatrixRow(uid="p", source_path="/data/runs/q/oil_step01.avi", engine="opencv", sample_id="",
                          run="/data/runs/q", protocol="oil", run_time="2026-11-26 13:00", replicate=1,
                          condition="300/40/40")
        images = MatrixRow(uid="i", source_path="/micro/K7Q-03-r2_set101", engine="cellpose", sample_id="",
                           sample_code="K7Q-03-r2")
        stray = MatrixRow(uid="s", source_path="/micro/K7Q-09-r1_set200", engine="cellpose", sample_id="",
                          sample_code="K7Q-09-r1")
        return video, plain, images, stray

    def test_a_labelled_image_folder_joins_the_run_of_its_id_in_its_condition(self):
        from admet.ui.analyze import _match_by_code, _name_rows

        video, plain, images, stray = rows = self.rows()
        _match_by_code(list(rows))
        _name_rows(list(rows))
        self.assertEqual((video.name, images.name, images.condition), ("K7Q-03-r2", "K7Q-03-r2", "300/40/40"))
        self.assertEqual(plain.name, "300/40/40_rep1")                         # a run of no experiment: as before
        self.assertEqual((stray.name, stray.condition, stray.unassigned), ("K7Q-09-r1", "", False))   # its own sample

    def test_a_folder_taken_out_by_hand_is_not_put_back(self):
        from admet.ui.analyze import _match_by_code

        video, _plain, images, _stray = rows = self.rows()
        _match_by_code(list(rows))
        images.sample_id, images.condition = "", ""                           # the user took it out
        _match_by_code(list(rows))
        self.assertEqual((images.sample_id, images.matched), ("", True))
        images.sample_id = "other sample"                                     # or put it elsewhere
        _match_by_code(list(rows))
        self.assertEqual((images.sample_id, images.condition), ("other sample", ""))


if __name__ == "__main__":
    unittest.main()
