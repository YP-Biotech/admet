from __future__ import annotations

import io
import json
import tempfile
from dataclasses import replace
import unittest
import zipfile
from pathlib import Path

from admet.ui.analyze_results import (
    FileResult,
    AGGREGATED_FIELDS,
    LiveQC,
    StoredResult,
    aggregated_cells,
    live_cells,
    live_qc,
    results_cells,
    expected_rate_hz,
    dispersed_flow_ul_min,
    bin_labels,
    cv_chart,
    diameter_chart,
    diameter_time_chart,
    export_csv_zip,
    file_results,
    histogram_edges,
    inclusions_chart,
    rate_time_chart,
    result_charts,
    run_charts,
    results_table_html,
    scatter_chart,
    settings_summary,
    short_names,
    spark,
    timeline_chart,
)


def _result(**overrides) -> FileResult:
    values = {"run": "p", "sample_id": "s", "engine": "opencv", "source_path": "/v.avi", "file_id": "f",
              "analyzed_at": "",
              "settings": {}, "fps": 100.0, "scale": 2.0, "frames": 10, "droplets": 3, "rate_hz": 20.0,
              "speed_mm_s": 2.0}
    values.update(overrides)
    return FileResult(**values)


def _stored(root: Path = Path("/p/study.admetp")) -> StoredResult:
    """A video's stored analysis, as the runner leaves it: its rows and summary in a results folder."""
    return StoredResult(source_path=root / "media" / "v.avi", engine="opencv", sample_id="s",
                        result_dir=root.parent / "results" / "k1",
                        metadata={"analyzed_at": "2026-11-13T10:00:00+00:00", "total_droplets": 3, "file_id": "f1",
                                  "settings": {"start_frame": 0, "end_frame": 10, "k_sigma": 4}},
                        run=str(root.parent / "study"))


def _imaging(**metadata) -> StoredResult:
    return StoredResult(source_path=Path("/p/study.admetp/media/imgs"), engine="cellpose", sample_id="imgs",
                        result_dir=Path("/p/results/k2"), metadata={"file_id": "f2", **metadata})


def _rows() -> list[dict]:
    """Three droplets, each seen for three frames moving 10 px a frame, one every five frames."""
    rows = [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "run_context",
             "values": {"analysis": {"fps": 100.0, "microns_per_pixel": 2.0}, "detection": {"gate_pct": 50}}}]
    for droplet in range(3):
        for step in range(3):
            frame = droplet * 5 + step
            rows.append({"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "detection",
                         "values": {"frame": frame, "droplet_id": droplet + 1, "centroid_x": 100 + 10 * step,
                                    "centroid_y": 50, "equivalent_diameter": 20 + droplet, "area": 314 + step}})
    return rows


class FileResultTests(unittest.TestCase):
    def test_one_value_per_droplet_with_rate_and_speed(self):
        result, = file_results(_stored(), _rows())

        self.assertEqual(result.droplets, 3)
        self.assertEqual(result.diameters, (40.0, 42.0, 44.0))           # medians per droplet, in µm
        self.assertEqual(result.timed_diameters, ((0.0, 40.0), (0.05, 42.0), (0.1, 44.0)))   # when each arrived
        self.assertEqual(len(result.areas), 3)
        self.assertAlmostEqual(result.periods_s[0], 0.05)
        self.assertAlmostEqual(result.rate_hz, 20.0)
        self.assertAlmostEqual(result.speed_mm_s, 2.0)                   # 10 px/frame · 2 µm/px · 100 fps
        self.assertEqual(result.analyzed_at[:10], "2026-11-13")
        self.assertEqual(result.settings["k_sigma"], 4)
        self.assertEqual(result.settings["fps"], 100.0)
        self.assertEqual(result.source_path, "/p/study.admetp/media/v.avi")
        self.assertIn("µm/px 2", settings_summary(result.settings))

    def test_the_scale_and_fps_of_the_day_apply_without_a_new_run(self):
        scales = {("opencv", "/p/study.admetp/media/v.avi"): (4.0, 200.0)}

        result, = file_results(_stored(), _rows(), scales=scales)

        self.assertEqual(result.diameters, (80.0, 84.0, 88.0))           # px × 4 µm/px, not the run's 2
        self.assertAlmostEqual(result.rate_hz, 40.0)                     # 5 frames apart at 200 fps
        self.assertAlmostEqual(result.speed_mm_s, 8.0)                   # 10 px/frame · 4 µm/px · 200 fps
        self.assertEqual((result.scale, result.fps, result.settings["fps"]), (4.0, 200.0, 200.0))
        self.assertEqual(result.expected_hz, 0.0)                        # no flow known: nothing expected

    def test_the_expected_rate_comes_from_the_dispersed_flow_and_the_droplet_size(self):
        record = {"channels": [{"label": "Oil L", "mode": "flow", "flow_ul_min": 300.0},
                               {"label": "Cells M", "mode": "flow", "flow_ul_min": 40.0},
                               {"label": "Beads M", "mode": "flow", "flow_ul_min": 40.0}]}
        self.assertEqual(dispersed_flow_ul_min(record), 80.0)
        self.assertEqual(dispersed_flow_ul_min({"channels": [{"label": "Oil L", "flow_ul_min": 300.0}]}), 0.0)
        self.assertEqual(dispersed_flow_ul_min(None), 0.0)
        self.assertAlmostEqual(expected_rate_hz(80.0, 124.0), 1336, delta=2)   # 1.333 µL/s over 1.0 nL
        self.assertEqual(expected_rate_hz(80.0, 0.0), 0.0)

        scales = {("opencv", "/p/study.admetp/media/v.avi"): (2.0, 100.0, 80.0)}
        result, = file_results(_stored(), _rows(), scales=scales)

        self.assertEqual(result.dispersed_ul_min, 80.0)
        self.assertAlmostEqual(result.expected_hz, expected_rate_hz(80.0, 42.0), delta=1)   # the median diameter
        self.assertIn("expected", results_cells(result, "#000")["rate"])

    def test_a_rate_from_speed_over_spacing_is_taken_as_the_engine_gave_it(self):
        rows = _rows()
        rows[0]["values"]["generation"] = {"method": "spacing", "spacing_px": 50.0, "period_frames": 5.0,
                                           "crossings": 0, "rate_hz": 20.0}

        result, = file_results(_stored(), rows)

        self.assertAlmostEqual(result.rate_hz, 20.0)                     # 100 fps over 5 frames
        self.assertEqual(result.rate_note, "speed over spacing")

    def test_two_files_with_the_same_sample_name_stay_two_results(self):
        results = []
        for file_id, source in (("f1", "media/a.avi"), ("f2", "media/b.avi")):
            rows = [{"engine": "opencv", "item_id": "cells", "file_id": file_id, "kind": "run_context",
                     "values": {"analysis": {"fps": 50.0, "microns_per_pixel": 1.0}}},
                    {"engine": "opencv", "item_id": "cells", "file_id": file_id, "kind": "detection",
                     "values": {"frame": 1, "droplet_id": 1, "centroid_x": 5.0, "centroid_y": 5.0,
                                "equivalent_diameter": 30, "area": 700}}]
            stored = StoredResult(source_path=Path("/p/study.admetp") / source, engine="opencv", sample_id="cells",
                                  result_dir=Path("/p/results") / file_id, metadata={"file_id": file_id})
            results.extend(file_results(stored, rows))

        self.assertEqual([(r.sample_id, r.file_id, Path(r.source_path).name) for r in results],
                         [("cells", "f1", "a.avi"), ("cells", "f2", "b.avi")])

    def test_every_row_of_a_long_file_counts_and_the_plot_points_are_spread(self):
        rows = [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "run_context",
                 "values": {"analysis": {"fps": 100.0, "microns_per_pixel": 1.0}}}]
        rows += [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "detection",
                  "values": {"frame": i, "droplet_id": i, "centroid_x": 1.0, "centroid_y": 1.0,
                             "equivalent_diameter": 10, "area": 78}} for i in range(70_000)]

        result, = file_results(_stored(), iter(rows))

        self.assertEqual(len(result.diameters), 70_000)
        self.assertEqual(len(result.detections), 6000)
        self.assertGreater(result.detections[-1][0], 60_000)       # a sample from the whole file, not its start

    def test_an_imaging_droplet_counts_once_per_image(self):
        """Cellpose numbers droplets from 0 in every image; the same id in two images is two droplets."""
        rows = [{"engine": "cellpose", "item_id": "imgs", "kind": "droplet",
                 "values": {"frame": frame, "droplet_id": droplet, "center_x": 10.0 * droplet, "center_y": 5.0,
                            "diameter_um": 140.0 + droplet, "area_um2": 15000.0, "inclusions": frame + droplet}}
                for frame in (1, 2) for droplet in (0, 1)]
        run = _imaging(droplet_count=4)

        result, = file_results(run, rows)

        self.assertEqual(result.droplets, 4)
        self.assertEqual(result.diameters, (140.0, 141.0, 140.0, 141.0))
        self.assertEqual(result.inclusion_counts, (1, 2, 2, 3))
        self.assertEqual(len(result.detections), 4)
        self.assertEqual(result.rate_hz, 0.0)

    def test_hand_corrections_switch_droplets_off_and_change_inclusions(self):
        rows = [{"engine": "cellpose", "item_id": "imgs", "kind": "droplet",
                 "values": {"frame": 1, "droplet_id": droplet, "center_x": 10.0 * droplet, "center_y": 5.0,
                            "diameter_um": 140.0, "area_um2": 15000.0, "inclusions": 1}}
                for droplet in (0, 1, 2)]
        run = _imaging(droplet_count=3)
        fixes = {("cellpose", "/p/study.admetp/media/imgs"): {"1:0": {"disabled": True}, "1:2": {"inclusions": 2}}}

        result, = file_results(run, rows, fixes)

        self.assertEqual(result.droplets, 2)
        self.assertEqual(result.inclusion_counts, (1, 3))

    def test_the_table_holds_the_numbers_and_their_sparklines(self):
        result, = file_results(_stored(), _rows())

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
        chart = diameter_chart([_result(sample_id="a", source_path="/a.avi", diameters=(10.0, 11.0, 12.0)),
                                _result(sample_id="b", diameters=(20.0, 21.0, 22.0))])

        self.assertEqual([series["name"] for series in chart["series"]], ["a", "b"])
        self.assertNotEqual(chart["series"][0]["itemStyle"]["color"], chart["series"][1]["itemStyle"]["color"])
        self.assertEqual(sum(chart["series"][0]["data"]), 3)

    def test_cv_chart_has_one_bar_per_sample_in_its_colour(self):
        chart = cv_chart([_result(sample_id="a", source_path="/a.avi", diameters=(10.0, 10.0, 12.0)),
                          _result(sample_id="b", source_path="/b.avi", diameters=(20.0, 20.0)),
                          _result(sample_id="none", source_path="/n.avi")])

        self.assertEqual(chart["xAxis"]["data"], ["a", "b"])
        values = [point["value"] for point in chart["series"][0]["data"]]
        self.assertAlmostEqual(values[0], 8.84, places=2)
        self.assertEqual(values[1], 0.0)
        self.assertNotEqual(chart["series"][0]["data"][0]["itemStyle"]["color"],
                            chart["series"][0]["data"][1]["itemStyle"]["color"])

    def test_only_plots_with_data_for_the_ticked_files_are_shown(self):
        video = _result(sample_id="v", diameters=(10.0, 11.0), spans=((1, 0, 5),), arrivals_s=(0.0, 0.1),
                        detections=((0, 1.0, 2.0, 3.0),))
        imaging = _result(sample_id="i", engine="cellpose", diameters=(140.0, 141.0), inclusion_counts=(0, 1, 1, 3),
                          detections=((1, 10.0, 20.0, 15000.0),))

        self.assertEqual([name for name, _ in result_charts([video])],
                         ["diameter_distribution", "cv_per_sample", "droplet_timeline", "rate_over_time",
                          "droplet_positions", "area_along_x"])
        timed = _result(sample_id="t", diameters=(10.0, 11.0, 12.0), arrivals_s=(0.0, 0.4, 1.2),
                        timed_diameters=((0.0, 10.0), (0.4, 11.0), (1.2, 12.0)))
        self.assertIn("diameter_over_time", [name for name, _ in result_charts([timed])])
        self.assertEqual(diameter_time_chart([timed])["series"][0]["data"], [[0.5, 10.5], [1.5, 12.0]])
        self.assertEqual(rate_time_chart([timed])["series"][0]["data"], [[0.5, 2.0], [1.5, 1.0]])
        self.assertEqual([name for name, _ in result_charts([imaging])],
                         ["diameter_distribution", "cv_per_sample", "droplet_positions", "area_along_x",
                          "inclusions_per_droplet"])
        self.assertEqual(result_charts([]), [])
        chart = inclusions_chart([imaging])
        self.assertEqual(chart["xAxis"]["data"], ["0", "1", "3"])
        self.assertEqual(chart["series"][0]["data"], [1, 2, 1])

    def test_timeline_counts_droplets_against_seconds_from_each_files_start(self):
        chart = timeline_chart([_result(sample_id="a", arrivals_s=(0.0, 0.01, 0.02)),
                                _result(sample_id="b", arrivals_s=(0.0, 0.05))])

        self.assertEqual(chart["series"][0]["data"], [[0.0, 1], [0.01, 2], [0.02, 3]])
        self.assertEqual(chart["series"][1]["data"], [[0.0, 1], [0.05, 2]])
        self.assertEqual(chart["yAxis"]["name"], "droplets")

    def test_plots_name_samples_by_what_differs(self):
        names = ["run_18dc8be5506449fda417fd6206c9230f_step01_20261113_183825",
                 "run_18dc8be5506449fda417fd6206c9230f_step02_20261113_184149"]

        self.assertEqual(short_names(names), {names[0]: "step01_20261113_183825", names[1]: "step02_20261113_184149"})
        self.assertEqual(short_names(["a", "b"]), {"a": "a", "b": "b"})
        whole = ["dropseq_1n 16:43", "dropseq_3n 19:30 step 1"]                   # too short to cut at a word: kept whole
        self.assertEqual(short_names(whole), {name: name for name in whole})
        self.assertEqual(short_names(["only"]), {"only": "only"})
        chart = cv_chart([_result(sample_id=names[0], source_path="/1.avi", diameters=(1.0, 2.0)),
                          _result(sample_id=names[1], source_path="/2.avi", diameters=(1.0, 2.0))])
        self.assertEqual(chart["xAxis"]["data"], ["step01_20261113_183825", "step02_20261113_184149"])

    def test_the_rate_falls_back_to_arrivals_when_the_gate_saw_few_droplets(self):
        rows = [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "run_context",
                 "values": {"analysis": {"fps": 100.0, "microns_per_pixel": 1.0},
                            "generation": {"crossings": 2, "rate_hz": 3.0}}}]
        rows += [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "detection",
                  "values": {"frame": 2 * i, "droplet_id": i, "centroid_x": 1.0, "centroid_y": 1.0,
                             "equivalent_diameter": 10, "area": 78}} for i in range(50)]

        result, = file_results(_stored(), rows)

        self.assertAlmostEqual(result.rate_hz, 50.0)                        # 49 intervals over 98 frames at 100 fps
        self.assertEqual(result.rate_note, "from arrivals; gate saw 2 of 50")
        self.assertEqual(result.arrivals_s[:3], (0.0, 0.02, 0.04))

    def test_scatter_chart_keeps_axis_labels_inside_grid(self):
        chart = scatter_chart("t", "x", "y", [_result(detections=((0, 1.0, 2.0, 3.0),))], lambda d: [d[1], d[2]])

        self.assertTrue(chart["grid"]["containLabel"])
        self.assertEqual(chart["series"][0]["data"], [[1.0, 2.0]])


class ExportTests(unittest.TestCase):
    def test_csv_export_zips_a_summary_and_every_droplet_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored = _stored(Path(tmp) / "study.admetp")
            stored.rows_path.parent.mkdir(parents=True)
            stored.rows_path.write_text("\n".join(json.dumps(row) for row in _rows()))
            results = file_results(stored, _rows())

            archive = zipfile.ZipFile(io.BytesIO(export_csv_zip([stored], results)))

            self.assertEqual(sorted(archive.namelist()), ["droplets/study_s_opencv.csv", "summary.csv"])
            summary = archive.read("summary.csv").decode().splitlines()
            self.assertTrue(summary[0].startswith("run,sample,engine"))
            self.assertIn("study,s,opencv", summary[1])
            droplets = archive.read("droplets/study_s_opencv.csv").decode().splitlines()
            self.assertEqual(droplets[0], "kind,frame,droplet_id,centroid_x,centroid_y,equivalent_diameter,area")
            self.assertEqual(len(droplets), 10)

    def test_csv_export_leaves_out_droplets_switched_off_and_keeps_the_raw_count(self):
        rows = [{"engine": "cellpose", "item_id": "imgs", "kind": "droplet",
                 "values": {"frame": 1, "droplet_id": droplet, "inclusions": 1}} for droplet in (0, 1)]
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "study.admetp"
            stored = StoredResult(source_path=project / "media" / "imgs", engine="cellpose", sample_id="imgs",
                                  result_dir=Path(tmp) / "results" / "k2", metadata={"file_id": "f2"},
                                  run=str(Path(tmp) / "study"))
            stored.rows_path.parent.mkdir(parents=True)
            stored.rows_path.write_text("\n".join(json.dumps(row) for row in rows))
            fixes = {("cellpose", str(project / "media/imgs")): {"1:0": {"disabled": True},
                                                                  "1:1": {"inclusions": 1}}}

            archive = zipfile.ZipFile(io.BytesIO(export_csv_zip([stored], file_results(stored, rows, fixes), fixes)))

            lines = archive.read("droplets/study_imgs_cellpose.csv").decode().splitlines()
            self.assertEqual(lines, ["kind,frame,droplet_id,inclusions,inclusions_raw", "droplet,1,1,2,1"])


def _run_csv(directory: Path, stem: str = "oil_step01_x") -> Path:
    """A run's timeline with readings and the live QC rows of two recordings."""
    import csv

    columns = ["timestamp", "elapsed_s", "pressure_0_mbar", "flow_0_ul_min", "recording_id", "step", "part", "rate_hz",
               "rate_half", "count", "diameter", "diameter_unit", "diameter_half", "cv_percent", "cv_half", "n",
               "bursts", "speed_px_frame", "spacing_px", "state"]
    rows = [["10:00:00.000", "0.000", "1300", "300", *[""] * 16]]
    for k in range(5):
        rows.append([f"10:00:0{k + 1}.100", f"{k + 1}.100", "", "", stem, "1", "1", str(1400 + 10 * k), "20",
                     str(1400 * (k + 1)), "124.0", "um", "0.5", f"{3.0 + 0.1 * k:.1f}", "0.3", "200", str(k + 1),
                     "48", "128", "SETTLING" if k < 2 else "STABLE"])
    rows.append(["10:00:07.100", "7.100", "", "", "other_step02_x", "2", "1", "900", "20", "900", "100.0", "um",
                 "0.5", "6.0", "0.3", "200", "1", "48", "128", "UNSTABLE"])
    path = Path(directory) / "run.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return path


class LiveQCTests(unittest.TestCase):
    def test_the_live_qc_of_a_video_comes_from_its_own_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "20261119_100000_oil"
            run.mkdir()
            _run_csv(run)

            live = live_qc(run, run / "oil_step01_x.avi")

            self.assertEqual((live.bursts, live.state, live.diameter_unit), (5, "STABLE", "um"))
            self.assertEqual(live.rate_hz, 1420.0)                                   # the median over the bursts
            self.assertEqual((live.count, live.diameter), (7000.0, 124.0))
            self.assertAlmostEqual(live.cv_percent, 3.4)                             # the last, over the whole recording
            self.assertIsNone(live_qc(run, run / "unknown.avi"))
            self.assertIsNone(live_qc(Path(tmp) / "nowhere", run / "oil_step01_x.avi"))
            cells = live_cells(live, "", "#000")
            self.assertEqual(set(cells), set(results_cells(_result(), "#000")))
            self.assertIn("1420", cells["rate"])
            self.assertIn("124.0 ± 0.5", cells["diameter"])
            self.assertIn("STABLE", cells["analysed"])
            self.assertIn("↳", cells["sample"])
            self.assertIn("oil", live_cells(live, "oil", "#000")["sample"])
            self.assertEqual((live.run, Path(live.source_path).name), (run.name, "oil_step01_x.avi"))
            self.assertEqual(live.points[0], (0.0, 1400.0, 1400.0, 124.0, 3.0))   # s from its first burst
            self.assertEqual([point[0] for point in live.points], [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_a_live_line_is_plotted_as_its_own_series_beside_the_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "20261119_100000_oil"
            run.mkdir()
            _run_csv(run)
            live = replace(live_qc(run, run / "oil_step01_x.avi"), sample_id="oil")
        result = _result(sample_id="oil", run=run.name, source_path=live.source_path,      # the same file
                         diameters=(120.0, 124.0, 128.0), arrivals_s=(0.0, 0.5, 1.0),
                         timed_diameters=((0.0, 120.0), (0.5, 124.0), (1.0, 128.0)))

        charts = dict(result_charts([result], [live]))

        self.assertEqual(list(charts), ["diameter_distribution", "cv_per_sample", "droplet_timeline",
                                        "diameter_over_time", "rate_over_time"])
        timeline = charts["droplet_timeline"]["series"]
        self.assertEqual([series["name"] for series in timeline], ["oil", "oil live"])
        self.assertEqual(timeline[1]["data"], [[0.0, 1400.0], [1.0, 2800.0], [2.0, 4200.0], [3.0, 5600.0], [4.0, 7000.0]])
        self.assertEqual(timeline[0]["lineStyle"]["color"], timeline[1]["lineStyle"]["color"])   # one file, one style
        self.assertLess(timeline[1]["lineStyle"]["width"], timeline[0]["lineStyle"]["width"])   # the live line, thinner
        per_sample = charts["cv_per_sample"]
        self.assertEqual(per_sample["xAxis"]["data"], ["oil"])                                  # one place per file
        self.assertEqual([series["name"] for series in per_sample["series"]], ["analysed", "live"])
        self.assertEqual(per_sample["series"][1]["data"][0]["value"], 3.4)
        self.assertIn("decal", per_sample["series"][1]["data"][0]["itemStyle"])                # the live bar, hatched
        self.assertEqual(charts["diameter_over_time"]["series"][1]["data"][0], [0.0, 124.0])
        self.assertEqual(charts["rate_over_time"]["series"][1]["data"][-1], [4.0, 1440.0])
        self.assertEqual([series["name"] for series in charts["diameter_distribution"]["series"]], ["oil"])
        self.assertEqual(list(dict(result_charts([], [live]))),
                         ["cv_per_sample", "droplet_timeline", "diameter_over_time", "rate_over_time"])
        in_px = replace(live, diameter_unit="px")
        self.assertEqual(diameter_time_chart([], [in_px])["series"], [])
        views = {view: [name for name, _ in result_charts([result], [live], view=view)]
                 for view in ("aggregated", "opencv", "cellpose")}
        self.assertEqual(views["aggregated"], ["diameter_per_sample", "cv_per_sample", "rate_per_sample"])
        self.assertEqual(views["opencv"], ["diameter_distribution", "droplet_timeline", "diameter_over_time",
                                           "rate_over_time"])
        self.assertEqual(views["cellpose"], [])                                            # a video is not an image folder
        opencv = dict(result_charts([result], [live], view="opencv"))
        self.assertEqual([s["name"] for s in opencv["droplet_timeline"]["series"]], ["oil"])   # the live line stays out

    def test_a_runs_timeline_is_plotted_as_the_rig_shows_it(self):
        from admet.ui.series_style import series_styles

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "20261119_100000_oil"
            run.mkdir()
            _run_csv(run)
            (run / "run.json").write_text(json.dumps({"artifacts": {"polling_origin_monotonic": 100.0}, "events": [
                {"monotonic": 102.0, "step_index": 0, "step_name": "collect", "outcome": "running"},
                {"monotonic": 105.0, "step_index": 0, "step_name": "collect", "outcome": "completed"}]}))
            style = series_styles(["oil"])["oil"]

            charts = dict(run_charts(run, {"oil_step01_x": ("oil", style)}))

        self.assertEqual(list(charts), ["pressure", "flow", "droplets", "droplet_cv"])
        self.assertEqual(charts["pressure"]["series"][0]["name"], "Oil L")
        self.assertEqual(charts["pressure"]["series"][0]["data"], [[0.0, 1300.0]])
        self.assertEqual(charts["flow"]["series"][0]["data"], [[0.0, 300.0]])
        self.assertEqual(charts["flow"]["xAxis"]["name"], "s since the run began")
        spans = charts["pressure"]["series"][0]["markArea"]["data"]
        self.assertEqual([(a["name"], a["xAxis"], b["xAxis"]) for a, b in spans],
                         [("oil", 1.1, 5.1), ("other_step02_x", 7.1, 7.1)])            # each recording's span
        self.assertEqual([m["name"] for m in charts["flow"]["series"][0]["markLine"]["data"]], ["▶ 1", "■ 1"])
        droplets = charts["droplets"]["series"]
        self.assertEqual([s["name"] for s in droplets], ["oil", "other_step02_x"])
        self.assertEqual(droplets[0]["data"][0], [1.1, 1400.0])
        self.assertEqual(droplets[0]["lineStyle"]["color"], style.color)
        self.assertEqual(charts["droplet_cv"]["series"][0]["markLine"]["data"], [{"yAxis": 5.0}])
        self.assertEqual(run_charts(Path(tmp) / "nowhere"), [])

    def test_the_aggregated_line_sets_the_live_number_under_the_analysis(self):
        from admet.ui.series_style import series_styles

        result = _result(sample_id="oil", diameters=(120.0, 124.0, 128.0))
        live = LiveQC(rate_hz=1420.0, rate_half=20.0, diameter=124.0, diameter_half=0.5, diameter_unit="um",
                      cv_percent=3.4, cv_half=0.3, count=7000.0, bursts=5, state="STABLE")
        style = series_styles(["oil"])["oil"]

        both = aggregated_cells("oil", style, result, live)
        alone = aggregated_cells("oil", None, None, live, active=False)

        self.assertEqual(set(both), set(AGGREGATED_FIELDS))
        self.assertIn("124.0 ± 3.3", both["diameter"])
        self.assertIn("124.0 um live", both["diameter"])
        self.assertIn("1420 live", both["rate"])
        self.assertIn(style.color, both["sample"])
        self.assertIn("not analysed", alone["sample"])
        self.assertIn("results-off", alone["droplets"])
        self.assertIn("7 000 live", alone["droplets"])

    def test_the_summary_csv_carries_the_live_columns(self):
        result = _result(run="oil", source_path="/v.avi")
        live = LiveQC(rate_hz=1420.0, rate_half=20.0, diameter=124.0, diameter_half=0.5, diameter_unit="um",
                      cv_percent=3.4, cv_half=0.3, count=7000.0, bursts=5, state="STABLE")

        archive = zipfile.ZipFile(io.BytesIO(export_csv_zip([], [result], live={("oil", "opencv", "/v.avi"): live})))

        header, line = archive.read("summary.csv").decode().splitlines()[:2]
        self.assertTrue(header.endswith("live_rate_hz,live_diameter,live_diameter_unit,live_cv_percent,live_state"))
        self.assertTrue(line.endswith("1420,124,um,3.4,STABLE"))
        plain = zipfile.ZipFile(io.BytesIO(export_csv_zip([], [result]))).read("summary.csv").decode().splitlines()[1]
        self.assertTrue(plain.endswith(",,,,"))


if __name__ == "__main__":
    unittest.main()
