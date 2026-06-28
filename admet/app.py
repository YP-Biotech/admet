from __future__ import annotations

import argparse

from admet.engines.analyze import create_analyze_registry
from admet.engines.control import create_engine as create_control_engine
from admet.workflows import create_analyze_workflow, create_control_workflow


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="admet")
    parser.add_argument("mode", choices=("analyze", "control"))
    parser.add_argument("--engine", default="dummy", help="engine id for analyze mode")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    from nicegui import ui

    if args.mode == "analyze":
        workflow = create_analyze_workflow()
        registry = create_analyze_registry()
        try:
            engine = registry.create(args.engine)
        except LookupError as exc:
            parser.error(str(exc))
        native = False
    else:
        workflow = create_control_workflow()
        engine = create_control_engine()
        native = True

    from admet.ui import render_workflow

    def root() -> None:
        render_workflow(workflow, workflow.initial_state(), engine.settings, engine=engine)

    ui.run(
        root=root,
        native=native,
        reload=False,
        show=False,
        port=args.port,
        title=f"admet {args.mode}",
    )


if __name__ == "__main__":
    main()
