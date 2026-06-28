from __future__ import annotations

import argparse

from admet.engines.dummy import create_engine
from admet.workflows import create_analyze_workflow, create_control_workflow


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="admet")
    parser.add_argument("mode", choices=("analyze", "control"))
    args = parser.parse_args(argv)

    from nicegui import ui

    if args.mode == "analyze":
        workflow = create_analyze_workflow()
        native = False
    else:
        workflow = create_control_workflow()
        native = True

    from admet.ui import render_workflow

    def root() -> None:
        render_workflow(workflow, workflow.initial_state(), create_engine().settings)

    ui.run(root=root, native=native, reload=False, show=False, title=f"admet {args.mode}")


if __name__ == "__main__":
    main()
