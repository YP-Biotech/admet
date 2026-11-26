"""Launch the control desktop, serve project analysis, or describe the Python API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admet", description="ADMET desktop and instrument control")
    sub = parser.add_subparsers(dest="command", required=True)

    control = sub.add_parser("control", help="open the control desktop, which drives the rig")
    control.add_argument("--home", help="the admet home to use instead of ADMET_HOME or ~/.admet")

    analyze = sub.add_parser(
        "analyze", help="serve the analysis over the network; it reads recorded data and never drives the rig"
    )
    analyze.add_argument("--host", default="0.0.0.0", help="address to listen on (default: every interface)")
    analyze.add_argument("--port", type=int, default=8080)
    analyze.add_argument("--data", help="another folder of runs to watch beside the home's data folder, "
                                        "for example the export folder on the server")
    analyze.add_argument("--home", help="the admet home to use instead of ADMET_HOME or ~/.admet")

    describe = sub.add_parser(
        "describe", help="what exists: every operation, or one operation or engine"
    )
    describe.add_argument(
        "target",
        nargs="?",
        default="",
        help="an operation or engine id; omit for everything",
    )

    rig = sub.add_parser("rig", help="show the home's rig settings: where rig.json is and what it holds")
    rig.add_argument("--home", help="the admet home to use instead of ADMET_HOME or ~/.admet")

    export_run = sub.add_parser("export-run", help="write a run's record and timeline as run.json and run.csv")
    export_run.add_argument("run", help="the run folder, holding its run.db")
    export_run.add_argument("--into", help="the folder to write the two files in (default: the run folder)")

    sdk = sub.add_parser(
        "install-sdk", help="download Fluigent's SDK from its official GitHub into ADMET's vendor folder"
    )
    sdk.add_argument("--version", help="an fgt-SDK release tag (default: the one ADMET is tested with)")
    sdk.add_argument("--force", action="store_true", help="replace an SDK already there")

    bench = sub.add_parser(
        "camera-bench", help="record with the rig's camera for a few seconds a stage and count where frames are lost"
    )
    bench.add_argument("--seconds", type=float, default=10.0, help="how long each stage records (default 10)")
    bench.add_argument("--out", help="the folder to write the test recordings in, on the disk recordings go to "
                                     "(default: the system's temporary folder); they are removed after")
    bench.add_argument("--stage", action="append", choices=("grab", "disk", "preview+qc"),
                       help="run only this stage; repeat for several (default: all three)")
    bench.add_argument("--width", type=int, default=320)
    bench.add_argument("--height", type=int, default=60)
    bench.add_argument("--offset-x", type=int, default=0)
    bench.add_argument("--offset-y", type=int, default=0)
    bench.add_argument("--exposure", type=float, default=59.0, help="µs")
    bench.add_argument("--gain", type=float, default=0.0)
    bench.add_argument("--fps", type=float, default=0.0, help="limit the camera to this rate; 0 runs it free")
    bench.add_argument("--camera", type=int, default=0, help="the camera's index")
    bench.add_argument("--disk-gb", type=float, default=4.0,
                       help="GB written to measure the disk's sustained speed in --out, then removed (0 skips it)")
    bench.add_argument("--json", action="store_true", help="print the counts as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "control":
        from admet.ui.app import main as desktop_main

        return desktop_main(["--home", args.home] if args.home else [])

    if args.command == "analyze":
        return run_analyze_server(args.host, args.port, data=args.data, home=args.home)

    if args.command == "describe":
        from admet.core.service import Admet

        return _attempt(lambda: Admet().describe(args.target))

    if args.command == "rig":
        return run_rig(home=args.home)

    if args.command == "export-run":
        from admet.core import rundb

        source = Path(args.run) / rundb.DB_NAME
        if not source.is_file():
            print(f"{args.run} has no {rundb.DB_NAME}", file=sys.stderr)
            return 1
        into = Path(args.into or args.run)
        into.mkdir(parents=True, exist_ok=True)
        rundb.export_json(source, into / "run.json")
        rows = rundb.export_csv(source, into / "run.csv")
        print(f"{into / 'run.json'} and {into / 'run.csv'} ({rows} timeline rows)")
        return 0

    if args.command == "install-sdk":
        from admet.engines.acquisition.fluidics.sdk import FLUIGENT_SDK_VERSION, install_sdk

        version = args.version or FLUIGENT_SDK_VERSION
        try:
            path = install_sdk(version, force=args.force)
        except Exception as exc:
            print(f"Could not install the Fluigent SDK {version}: {exc}", file=sys.stderr)
            return 1
        print(f"Fluigent SDK {version} installed in {path}")
        return 0

    if args.command == "camera-bench":
        return run_camera_bench(args)

    parser.error(f"unknown command {args.command!r}")
    return 2


def run_camera_bench(args: argparse.Namespace) -> int:
    import tempfile
    from dataclasses import asdict, replace

    from admet.engines.acquisition.camera.bench import (STAGES, as_dicts, bench, report_lines, system_check,
                                                        system_lines)

    settings = {"camera_index": args.camera, "camera_width": args.width, "camera_height": args.height,
                "camera_offset_x": args.offset_x, "camera_offset_y": args.offset_y,
                "camera_exposure_us": args.exposure, "camera_gain": args.gain,
                "camera_framerate_enabled": args.fps > 0, "camera_framerate_hz": args.fps or 30.0}
    frame_bytes = args.width * args.height
    try:
        # The disk first, while its cache holds nothing of the bench's own recordings.
        check = system_check(args.out or tempfile.gettempdir(), frame_bytes, 0.0, disk_gb=args.disk_gb)
        results = bench(settings, seconds=args.seconds, out_dir=args.out, stages=tuple(args.stage or STAGES))
    except Exception as exc:
        print(f"Camera bench failed: {exc}", file=sys.stderr)
        return 1
    fps = max((result.camera_fps for result in results), default=0.0)
    check = replace(check, needed_mb_s=round(frame_bytes * fps / 2**20, 1))
    if args.json:
        return _print({"system": {**asdict(check), "ram_seconds": check.ram_seconds, "behind_mb_s": check.behind_mb_s},
                       "stages": as_dicts(results)})
    print("\n".join([*system_lines(check), "", *report_lines(results)]))
    return 0


def run_rig(*, home: str | None = None) -> int:
    """The home's rig settings: where rig.json is, and each setting it keeps."""
    from admet.core.home import AdmetHome

    resolved = AdmetHome.resolve(home).ensure()
    rig = resolved.rig()
    print(f"rig: {resolved.rig_path}")
    for key in sorted(rig):
        value = rig[key]
        size = f"{len(value)} entries" if isinstance(value, (dict, list)) else str(value)
        print(f"  {key}: {size}")
    if not rig:
        print("  nothing kept yet")
    return 0


def analyze_registry(home: str | None = None, data: str | None = None):
    """The registry of the home's analyze room. It watches the home's data folder from the start, what the
    rig records, and any other folder given; both are scanned for videos that arrived since."""
    from admet.core.home import AdmetHome
    from admet.core.registry import Registry

    resolved = AdmetHome.resolve(home).ensure()
    store = Registry(resolved.analyze_dir)
    store.add_root(resolved.data_dir)
    store.scan(resolved.data_dir)
    if data:
        store.add_root(data)
        store.scan(data)
    return store


def run_analyze_server(host: str, port: int, *, data: str | None = None, home: str | None = None) -> int:
    """One server for every browser that connects; each visit gets its own view of one registry."""
    from nicegui import ui

    from admet.engines import create_engine_registry
    from admet.ui.analyze import render_workflow
    from admet.ui.web_quirks import quiet_aborted_requests
    from admet.workflows import create_analyze_workflow

    quiet_aborted_requests()
    store = analyze_registry(home, data)
    registry = create_engine_registry("analyze")

    def root() -> None:
        workflow = create_analyze_workflow()
        render_workflow(workflow, workflow.initial_state(), registry=registry, store=store)

    try:
        ui.run(root=root, host=host, port=port, reload=False, show=False, title="admet analyze")
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C is how the server is meant to be stopped, not a failure.
        pass
    _finish_running_analyses()
    print("admet analyze stopped")
    return 0


def _finish_running_analyses() -> None:
    """Let analyses that are running finish their current file and save, before the process ends.

    A second Ctrl+C stops them at once; files already finished are still saved.
    """
    import time

    from admet.workflows import analyze_process
    from admet.workflows.analyze_runner import SHUTDOWN, running_batches

    def running() -> int:
        return running_batches() + analyze_process.live()

    SHUTDOWN.after_file.set()
    analyze_process.stop_all()
    if not running():
        return
    print(f"waiting for {running()} running analysis to finish its current file and save; "
          "Ctrl+C again to stop now")
    while running():
        try:
            time.sleep(0.2)
            analyze_process.stop_all(now=SHUTDOWN.now.is_set())   # polls them too
        except KeyboardInterrupt:
            if SHUTDOWN.now.is_set():
                raise
            SHUTDOWN.now.set()
            analyze_process.stop_all(now=True)
            print("stopping now; saving the files already finished")


def _attempt(call: Any) -> int:
    """Run something that can refuse, and report the refusal as the answer."""
    try:
        return _print(call())
    except Exception as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 1


def _print(payload: Any) -> int:
    print(json.dumps(payload, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
