from __future__ import annotations

from typing import Any

from admet.core.schema import ParamKind, ParamSchema
from admet.core.workflow import Workflow, WorkflowState


def render_workflow(workflow: Workflow, state: WorkflowState, settings: ParamSchema | None = None) -> None:
    from nicegui import ui

    ui.label(workflow.label).classes("text-h4")
    with ui.stepper().props("vertical").classes("w-full") as stepper:
        for stage in workflow.stages:
            with ui.step(stage.label):
                ui.label(state.statuses[stage.id].value)
        stepper.value = workflow.current_stage(state).label

    if settings is not None:
        render_settings(settings)


def render_settings(settings: ParamSchema) -> dict[str, Any]:
    from nicegui import ui

    values = settings.defaults()
    for param in settings.params:
        if param.kind is ParamKind.BOOLEAN:
            ui.checkbox(param.label, value=bool(param.default)).bind_value(values, param.name)
        elif param.kind is ParamKind.CHOICE:
            options = {option.value: option.label for option in param.options}
            ui.select(options, label=param.label, value=param.default).bind_value(values, param.name)
        elif param.kind in {ParamKind.INTEGER, ParamKind.FLOAT}:
            ui.number(
                label=param.label,
                value=param.default,
                min=param.minimum,
                max=param.maximum,
                step=param.step,
            ).bind_value(values, param.name)
        else:
            ui.input(param.label, value=param.default or "").bind_value(values, param.name)
    return values

