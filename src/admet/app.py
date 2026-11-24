"""Launch the control desktop, serve project analysis, or describe the Python API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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

    migrate = sub.add_parser("migrate", help="lay an old .admetp project out as run folders, as control records now")
    migrate.add_argument("project", help="the .admetp project folder")
    migrate.add_argument("--into", help="the data folder to lay the runs out in (default: the home's data folder)")
    migrate.add_argument("--copy", action="store_true", help="copy the videos instead of moving them")
    migrate.add_argument("--home", help="the admet home to use instead of ADMET_HOME or ~/.admet")
    migrate.add_argument("--rig", action="store_true",
                         help="take the project's rig settings into the home's rig.json (one project's rig is kept)")

    sdk = sub.add_parser(
        "install-sdk", help="download Fluigent's SDK from its official GitHub into ADMET's vendor folder"
    )
    sdk.add_argument("--version", help="an fgt-SDK release tag (default: the one ADMET is tested with)")
    sdk.add_argument("--force", action="store_true", help="replace an SDK already there")

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

    if args.command == "migrate":
        from admet.core.home import AdmetHome
        from admet.core.migrate import migrate_project, rig_settings
        from admet.core.registry import Registry

        home = AdmetHome.resolve(args.home).ensure()
        try:
            report = migrate_project(args.project, args.into or home.data_dir, copy=args.copy,
                                     registry=Registry(home.analyze_dir))
            lines = report.lines()
            if args.rig:
                settings = rig_settings(args.project)
                home.update_rig(**settings)
                lines.append(f"rig settings taken into {home.rig_path}: {', '.join(sorted(settings)) or 'none found'}")
        except (OSError, ValueError, KeyError) as exc:
            print(f"Could not migrate {args.project}: {exc}", file=sys.stderr)
            return 1
        print("\n".join(lines))
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

    parser.error(f"unknown command {args.command!r}")
    return 2


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

    from admet.workflows.analyze_runner import SHUTDOWN, running_batches

    SHUTDOWN.after_file.set()
    if not running_batches():
        return
    print(f"waiting for {running_batches()} running analysis to finish its current file and save; "
          "Ctrl+C again to stop now")
    while running_batches():
        try:
            time.sleep(0.2)
        except KeyboardInterrupt:
            if SHUTDOWN.now.is_set():
                raise
            SHUTDOWN.now.set()
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
