from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from admet.ui.analyze_results import (
    FileResult,
    StoredRun,
    bin_labels,
    diameter_chart,
    export_csv_zip,
    file_results,
    histogram_edges,
    results_table_html,
    scatter_chart,
    settings_summary,
    spark,
    timeline_chart,
)


def _result(**overrides) -> FileResult:
    values = {"project": "p", "sample_id": "s", "engine": "opencv", "source_path": "/v.avi", "analyzed_at": "",
              "settings": {}, "fps": 100.0, "scale": 2.0, "frames": 10, "droplets": 3, "rate_hz": 20.0,
              "speed_mm_s": 2.0}
    values.update(overrides)
    return FileResult(**values)


def _run(project: Path) -> StoredRun:
    return StoredRun(project_path=project, run_id="analysis", raw_path=project / "analysis" / "raw.jsonl",
                     jobs=({"engine": "opencv", "sample_id": "s", "file_id": "f1", "status": "complete",
                            "source_path": "media/v.avi",
                            "metadata": {"analyzed_at": "2026-11-13T10:00:00+00:00", "total_droplets": 3}},),
                     matrix=({"file_id": "f1", "settings": {"start_frame": 0, "end_frame": 10, "k_sigma": 4}},))


def _rows() -> list[dict]:
    """Three droplets, each seen for three frames moving 10 px a frame, one every five frames."""
    rows = [{"engine": "opencv", "item_id": "s", "kind": "run_context",
             "values": {"analysis": {"fps": 100.0, "microns_per_pixel": 2.0}, "detection": {"gate_pct": 50}}}]
    for droplet in range(3):
        for step in range(3):
            frame = droplet * 5 + step
            rows.append({"engine": "opencv", "item_id": "s", "kind": "detection",
                         "values": {"frame": frame, "droplet_id": droplet + 1, "centroid_x": 100 + 10 * step,
                                    "centroid_y": 50, "equivalent_diameter": 20 + droplet, "area": 314 + step}})
    return rows


class FileResultTests(unittest.TestCase):
    def test_one_value_per_droplet_with_rate_and_speed(self):
        result, = file_results(_run(Path("/p/study.admetp")), _rows())

        self.assertEqual(result.droplets, 3)
        self.assertEqual(result.diameters, (40.0, 42.0, 44.0))           # medians per droplet, in µm
        self.assertEqual(len(result.areas), 3)
        self.assertAlmostEqual(result.periods_s[0], 0.05)
        self.assertAlmostEqual(result.rate_hz, 20.0)
        self.assertAlmostEqual(result.speed_mm_s, 2.0)                   # 10 px/frame · 2 µm/px · 100 fps
        self.assertEqual(result.analyzed_at[:10], "2026-11-13")
        self.assertEqual(result.settings["k_sigma"], 4)
        self.assertEqual(result.settings["fps"], 100.0)
        self.assertIn("µm/px 2", settings_summary(result.settings))

    def test_an_imaging_droplet_counts_once_per_image(self):
        """Cellpose numbers droplets from 0 in every image; the same id in two images is two droplets."""
        rows = [{"engine": "cellpose", "item_id": "imgs", "kind": "droplet",
                 "values": {"frame": frame, "droplet_id": droplet, "center_x": 10.0 * droplet, "center_y": 5.0,
                            "diameter_um": 140.0 + droplet, "area_um2": 15000.0, "inclusions": frame + droplet}}
                for frame in (1, 2) for droplet in (0, 1)]
        run = StoredRun(Path("/p/study.admetp"), "analysis", Path("analysis/raw.jsonl"),
                        jobs=({"engine": "cellpose", "sample_id": "imgs", "file_id": "f2", "status": "complete",
                               "metadata": {"droplet_count": 4}},))

        result, = file_results(run, rows)

        self.assertEqual(result.droplets, 4)
        self.assertEqual(result.diameters, (140.0, 141.0, 140.0, 141.0))
        self.assertEqual(result.inclusion_counts, (1, 2, 2, 3))
        self.assertEqual(len(result.detections), 4)
        self.assertEqual(result.rate_hz, 0.0)

    def test_the_table_holds_the_numbers_and_their_sparklines(self):
        result, = file_results(_run(Path("/p/study.admetp")), _rows())

        table = results_table_html([result])

        self.assertIn("42.0 ± 1.6", table)
        self.assertIn('class="spark"', table)
        self.assertIn("k 4", table)
        self.assertNotIn("Inclusions", table)

    def test_spark_is_empty_below_two_values(self):
        self.assertEqual(spark([1.0]), "")
        self.assertIn("<rect", spark([1.0, 1.1, 1.2, 5.0]))


class ChartTests(unittest.TestCase):
    def test_histogram_bins_follow_a_tight_distribution(self):
        values = [49.9 + 0.01 * (i % 20) for i in range(400)]

        edges = histogram_edges(values)
        labels = bin_labels(edges)

        self.assertEqual(len(labels), len(set(labels)))                 # neighbours read differently
        self.assertGreaterEqual(len(labels), 8)

    def test_diameter_chart_colours_each_file(self):
        chart = diameter_chart([_result(sample_id="a", diameters=(10.0, 11.0, 12.0)),
                                _result(sample_id="b", diameters=(20.0, 21.0, 22.0))])

        self.assertEqual([series["name"] for series in chart["series"]], ["a", "b"])
        self.assertNotEqual(chart["series"][0]["itemStyle"]["color"], chart["series"][1]["itemStyle"]["color"])
        self.assertEqual(sum(chart["series"][0]["data"]), 3)

    def test_timeline_uses_line_segments_per_droplet(self):
        chart = timeline_chart([_result(sample_id="a", spans=((1, 0, 12), (2, 3, 18)))])

        self.assertEqual(chart["series"][0]["data"][:3], [[0, 1], [12, 1], ["-", "-"]])
        self.assertEqual(chart["yAxis"]["name"], "droplet id")

    def test_scatter_chart_keeps_axis_labels_inside_grid(self):
        chart = scatter_chart("t", "x", "y", [_result(detections=((0, 1.0, 2.0, 3.0),))], lambda d: [d[1], d[2]])

        self.assertTrue(chart["grid"]["containLabel"])
        self.assertEqual(chart["series"][0]["data"], [[1.0, 2.0]])


class ExportTests(unittest.TestCase):
    def test_csv_export_zips_a_summary_and_every_droplet_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "study.admetp"
            (project / "analysis").mkdir(parents=True)
            (project / "analysis" / "raw.jsonl").write_text("\n".join(json.dumps(row) for row in _rows()))
            run = _run(project)
            results = file_results(run, _rows())

            archive = zipfile.ZipFile(io.BytesIO(export_csv_zip([run], results)))

            self.assertEqual(sorted(archive.namelist()), ["droplets/study_s_opencv.csv", "summary.csv"])
            summary = archive.read("summary.csv").decode().splitlines()
            self.assertTrue(summary[0].startswith("project,sample,engine"))
            self.assertIn("study.admetp,s,opencv", summary[1])
            droplets = archive.read("droplets/study_s_opencv.csv").decode().splitlines()
            self.assertEqual(droplets[0], "kind,frame,droplet_id,centroid_x,centroid_y,equivalent_diameter,area")
            self.assertEqual(len(droplets), 10)


if __name__ == "__main__":
    unittest.main()
