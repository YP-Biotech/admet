import json
import tempfile
import unittest
from pathlib import Path

from admet.core.engine import ActionSpec, EngineRegistry, Param, ParamKind, ParamSchema
from admet.core.registry import content_key
from admet.core.run import RunJob, RunResult
from admet.workflows.analyze_runner import AnalyzeBatchRunner, AnalyzeTarget, infer_engine


def _registry(engine=None):
    registry = EngineRegistry()
    registry.register("opencv", lambda: engine or FakeAnalyzeEngine("opencv"))
    return registry


class AnalyzeBatchRunnerTests(unittest.TestCase):
    def test_infer_engine_from_source_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_dir = Path(tmpdir) / "images"
            image_dir.mkdir()

            self.assertEqual(infer_engine(Path(tmpdir) / "sample.avi"), "opencv")
            self.assertEqual(infer_engine(image_dir), "cellpose")

    def test_a_file_is_analysed_into_its_own_results_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.avi"
            source.write_bytes(b"video")
            engine = FakeAnalyzeEngine("opencv")
            settings = {"microns_per_pixel": 1.2, "fps": 30.0, "start_frame": 0, "end_frame": 3}

            report = AnalyzeBatchRunner(_registry(engine), results_dir=root / "results").run(
                (AnalyzeTarget(source_path=source, engine="opencv", sample_id="set01", settings=settings),))

            job, = report.jobs
            result_dir = Path(job.result_dir)
            self.assertEqual((job.status, job.sample_id, job.source_path), ("complete", "set01", str(source)))
            self.assertEqual(result_dir, root / "results" / content_key(source, {"start_frame": 0, "end_frame": 3}))
            rows = [json.loads(line) for line in (result_dir / "rows.jsonl").read_text().splitlines()]
            self.assertEqual((rows[0]["engine"], rows[0]["file_id"], rows[0]["item_id"]),
                             ("opencv", result_dir.name, "set01"))
            summary = json.loads((result_dir / "result.json").read_text())
            self.assertEqual((summary["source_path"], summary["sample_id"], summary["engine"]),
                             (str(source), "set01", "opencv"))
            self.assertEqual(summary["settings"], settings)
            self.assertTrue(summary["analyzed_at"])
            self.assertEqual(engine.calls[0]["cache_dir"], result_dir)
            self.assertEqual(engine.calls[0]["settings"], {"microns_per_pixel": 1.2, "fps": 30.0, "start_frame": 0,
                                                           "end_frame": 3})

    def test_the_same_file_analysed_the_same_way_is_read_back_not_run_again(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.avi"
            source.write_bytes(b"video")
            engine = FakeAnalyzeEngine("opencv")
            runner = AnalyzeBatchRunner(_registry(engine), results_dir=root / "results")
            target = AnalyzeTarget(source_path=source, engine="opencv", sample_id="s1")

            first = runner.run((target,))
            second = runner.run((target,))
            fresh = runner.run((AnalyzeTarget(source_path=source, engine="opencv", sample_id="s1", cache_policy="discard"),))

            self.assertEqual([job.status for job in (*first.jobs, *second.jobs, *fresh.jobs)],
                             ["complete", "cached", "complete"])
            self.assertEqual(len(engine.calls), 2)
            self.assertEqual(first.jobs[0].result_dir, second.jobs[0].result_dir)
            self.assertTrue(second.jobs[0].metadata["cached"])
            self.assertEqual(len(list((root / "results").iterdir())), 1)

    def test_a_copy_of_the_file_elsewhere_finds_the_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "a.avi"
            source.write_bytes(b"video bytes")
            copy = root / "server" / "b.avi"
            copy.parent.mkdir()
            copy.write_bytes(b"video bytes")
            engine = FakeAnalyzeEngine("opencv")
            runner = AnalyzeBatchRunner(_registry(engine), results_dir=root / "results")

            runner.run((AnalyzeTarget(source_path=source, engine="opencv"),))
            report = runner.run((AnalyzeTarget(source_path=copy, engine="opencv"),))

            self.assertEqual(report.jobs[0].status, "cached")
            self.assertEqual(len(engine.calls), 1)

    def test_reports_monotonic_intra_file_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for name in ("a", "b"):
                (root / f"{name}.avi").write_bytes(name.encode())
            seen: list[float] = []

            AnalyzeBatchRunner(_registry(), results_dir=root / "results").run(
                (AnalyzeTarget(root / "a.avi", engine="opencv"), AnalyzeTarget(root / "b.avi", engine="opencv")),
                on_progress=lambda pct: seen.append(round(pct, 3)))

        self.assertEqual((seen[0], seen[-1]), (0.0, 100.0))
        self.assertEqual(seen, sorted(seen))
        # two files: 50% of file 1 -> 25% overall, 50% of file 2 -> 75% overall
        self.assertIn(25.0, seen)
        self.assertIn(75.0, seen)

    def test_an_unknown_source_is_skipped_without_aborting_the_rest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "notes.txt").write_text("not analysis input", encoding="utf-8")
            (root / "a.avi").write_bytes(b"a")

            report = AnalyzeBatchRunner(_registry(), results_dir=root / "results").run(
                (AnalyzeTarget(root / "notes.txt"), AnalyzeTarget(root / "a.avi"),
                 AnalyzeTarget(root / "a.avi", engine="nothing"),
                 AnalyzeTarget(root / "a.avi", engine="opencv", cache_policy="skip")))
            kept = len(list((root / "results").iterdir()))

        self.assertEqual([job.status for job in report.jobs], ["skipped", "complete", "skipped", "skipped"])
        self.assertIn("cannot infer analyze engine", report.jobs[0].warnings[0])
        self.assertIn("unknown analyze engine", report.jobs[2].warnings[0])
        self.assertEqual(report.jobs[3].result_dir, "")
        self.assertEqual(kept, 1)                                                      # nothing kept for a skip


class StopTests(unittest.TestCase):
    """Ctrl+C on the server: finish the file in hand, or stop now, and keep finished work."""

    def batch(self, root, engine, stop):
        sources = []
        for name in ("a", "b"):
            source = root / f"{name}.avi"
            source.write_bytes(name.encode())
            sources.append(source)
        targets = tuple(AnalyzeTarget(source_path=source, engine="opencv", sample_id=source.stem) for source in sources)
        return AnalyzeBatchRunner(_registry(engine), results_dir=root / "results").run(targets, stop=stop)

    def test_stop_after_the_current_file_saves_it_and_skips_the_rest(self):
        from admet.workflows.analyze_runner import AnalysisStop

        stop = AnalysisStop()
        engine = FakeAnalyzeEngine("opencv")
        original = engine.run
        engine.run = lambda job: (stop.after_file.set(), original(job))[1]   # Ctrl+C during file a
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.batch(Path(tmpdir), engine, stop)
            finished = [p.parent.name for p in (Path(tmpdir) / "results").rglob("result.json")]

        self.assertEqual([job.status for job in report.jobs], ["complete", "stopped"])
        self.assertEqual(finished, [Path(report.jobs[0].result_dir).name])

    def test_stop_now_abandons_the_file_in_hand_and_resumes_from_what_was_kept(self):
        from admet.workflows.analyze_runner import AnalysisStop

        stop = AnalysisStop()
        engine = FakeAnalyzeEngine("opencv")
        original = engine.run

        def run(job):
            if job.metadata["item_id"] == "b":
                stop.now.set()                     # second Ctrl+C while file b runs
                job.progress(50, "half way")
            return original(job)

        engine.run = run
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report = self.batch(root, engine, stop)
            self.assertEqual([job.status for job in report.jobs], ["complete", "stopped"])
            self.assertEqual(len(list((root / "results").rglob("result.json"))), 1)   # b left unfinished

            engine.run = original
            resumed = self.batch(root, engine, AnalysisStop())

        self.assertEqual([job.status for job in resumed.jobs], ["cached", "complete"])


class FakeAnalyzeEngine:
    name = "Fake Analyze"
    settings = ParamSchema(
        (
            Param("video_path", "Video Path", ParamKind.PATH, default="", required=True),
            Param("microns_per_pixel", "Microns Per Pixel", ParamKind.FLOAT, default=1.0),
            Param("fps", "FPS", ParamKind.FLOAT, default=0.0),
            Param("start_frame", "Start Frame", ParamKind.INTEGER, default=0),
            Param("end_frame", "End Frame", ParamKind.INTEGER, default=None),
        )
    )
    actions = (
        ActionSpec(
            "run_analysis",
            "Analyze",
            "analysis",
            params=("video_path", "microns_per_pixel", "fps", "start_frame", "end_frame"),
        ),
    )

    def __init__(self, engine_id: str):
        self.id = engine_id
        self.calls = []

    def run(self, job: RunJob) -> RunResult:
        self.calls.append(
            {
                "settings": dict(job.settings),
                "inputs": dict(job.inputs),
                "cache_dir": job.cache_dir,
                "metadata": dict(job.metadata),
            }
        )
        if job.sink is not None:
            for frame in range(3):
                job.sink.write({
                    "job_id": job.id, "engine": self.id, "kind": "detection",
                    "file_id": job.metadata.get("file_id"), "item_id": job.metadata.get("item_id"),
                    "values": {"frame": frame, "droplet_id": 1, "centroid_x": 10.0 + frame, "centroid_y": 5.0,
                               "equivalent_diameter": 20.0, "area": 314.0},
                })
        if job.progress is not None:
            job.progress(50, "half")
        return RunResult(job_id=job.id, engine=self.id, action=job.action, status="complete",
                         metadata={"total_droplets": 1, "frames": 3})
