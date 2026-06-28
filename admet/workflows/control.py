from admet.core.workflow import Stage, Workflow


def create_control_workflow() -> Workflow:
    return Workflow(
        workflow_id="control",
        label="Acquisition",
        stages=(
            Stage("scene", "Scene"),
            Stage("fluigent", "Fluigent", action="connect_fluidics"),
            Stage("runs", "Runs", action="run_protocol", confirmation_required=True),
            Stage("wash", "Wash", action="wash", skippable=True, confirmation_required=True),
            Stage("calibration", "Calibration", action="calibrate", skippable=True),
        ),
    )

