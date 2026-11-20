"""Every plotted series its own colour and pattern; replicates as shades of one hue."""

import unittest

from admet.ui import theme
from admet.ui.series_style import DASHES, SHADES, oklch, sample_identity, series_styles, shade_of


class IdentityTests(unittest.TestCase):
    def test_a_replicate_marker_at_the_end_of_a_name_names_its_group(self):
        self.assertEqual(sample_identity("oil rep 2"), ("oil", "2"))
        self.assertEqual(sample_identity("oil_rep02"), ("oil", "2"))
        self.assertEqual(sample_identity("Oil A replicate 3"), ("Oil A", "3"))
        self.assertEqual(sample_identity("oil r2"), ("oil", "2"))
        self.assertEqual(sample_identity("oil #2"), ("oil", "2"))
        self.assertEqual(sample_identity("dropseq_3n 19:30 step 2"), ("dropseq_3n 19:30", "2"))
        self.assertEqual(sample_identity("dropseq_3n 19:30 set01_rep01"), ("dropseq_3n 19:30 set01", "1"))
        self.assertEqual(sample_identity("dropseq_1n 16:43"), ("dropseq_1n 16:43", ""))     # a run's time is no replicate
        self.assertEqual(sample_identity("chamber2"), ("chamber2", ""))                       # no marker: its own group
        self.assertEqual(sample_identity(""), ("", ""))


class StyleTests(unittest.TestCase):
    def test_replicates_share_a_hue_as_shades_and_groups_take_hues_in_order(self):
        styles = series_styles(["oil rep 1", "oil rep 2", "oil rep 3", "water", "beads #1", "beads #2"])

        self.assertEqual([styles[n].hue for n in ("oil rep 1", "oil rep 2", "oil rep 3")], [0, 0, 0])
        self.assertEqual((styles["water"].hue, styles["beads #1"].hue, styles["beads #2"].hue), (1, 2, 2))
        self.assertEqual([styles[n].shade for n in ("oil rep 1", "oil rep 2", "oil rep 3")], [0, 1, 2])
        lightness = [oklch(styles[n].color)[0] for n in ("oil rep 1", "oil rep 2", "oil rep 3")]
        self.assertLess(lightness[1], lightness[0] - 0.06)                                   # darker
        self.assertGreater(lightness[2], lightness[0] + 0.06)                                # lighter
        self.assertEqual(styles["oil rep 1"].color, theme.CHART_COLORS[0])
        self.assertTrue(all(abs(oklch(styles[n].color)[2] - oklch(theme.CHART_COLORS[0])[2]) < 2
                            for n in ("oil rep 2", "oil rep 3")))                            # the same hue

    def test_no_two_series_share_colour_and_pattern(self):
        names = [f"group {g} rep {r}" for g in range(20) for r in range(12)]                 # 240 series
        styles = series_styles(names)

        seen = {(s.color, str(s.dash), s.symbol) for s in styles.values()}
        self.assertEqual(len(seen), len(names))
        self.assertEqual(len({(s.color, str(s.decal)) for s in styles.values()}), len(names))   # bars too
        beyond = styles["group 9 rep 1"]                                                     # the ninth group: round two
        self.assertEqual((beyond.hue, beyond.dash), (1, DASHES[1]))
        self.assertIsNotNone(beyond.decal)
        sixth = styles["group 0 rep 6"]                                                      # past five shades
        self.assertEqual((sixth.dash, sixth.symbol, sixth.marked), (DASHES[0], "triangle", True))
        self.assertFalse(styles["group 0 rep 1"].marked)

    def test_a_group_keeps_its_hue_when_earlier_files_are_unticked(self):
        listed = ["oil", "water", "beads"]
        self.assertEqual(series_styles(["beads"], listed=listed)["beads"].hue, 2)
        self.assertEqual(series_styles(["beads", "oil"], listed=listed)["oil"].hue, 0)

    def test_shades_stay_inside_the_readable_band_for_every_hue(self):
        for color in theme.CHART_COLORS:
            shades = [shade_of(color, shade) for shade in range(SHADES)]
            self.assertEqual(len(set(shades)), SHADES, color)
            for shade in shades:
                lightness = oklch(shade)[0]
                self.assertTrue(0.42 <= lightness <= 0.78, (color, shade, lightness))
            self.assertEqual(shades[0], color)

    def test_live_and_hatched_item_styles(self):
        style = series_styles(["oil"])["oil"]
        self.assertEqual(style.item(), {"color": style.color})
        self.assertIn("decal", style.item(live=True))
        self.assertEqual(style.line()["type"], "solid")
        self.assertLess(style.line(live=True)["width"], style.line()["width"])


if __name__ == "__main__":
    unittest.main()
