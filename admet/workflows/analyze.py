from admet.core.workflow import Stage, Workflow


def create_analyze_workflow() -> Workflow:
    return Workflow(
        workflow_id="analyze",
        label="Analysis",
        stages=(
            Stage("import", "Import"),
            Stage("matrix", "Batch Matrix"),
            Stage("analyze", "Analyze", action="analyze"),
            Stage("view", "View"),
            Stage("export", "Export", action="export", skippable=True),
        ),
    )

