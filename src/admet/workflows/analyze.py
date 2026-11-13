from __future__ import annotations

from .model import EditorSpec, SettingsSpec, Stage, StageAction, StageInstruction, Workflow


def create_analyze_workflow() -> Workflow:
    """Three steps: pick the files, set each up and run, view what was found."""
    return Workflow(
        workflow_id="analyze",
        label="Analysis",
        stages=(
            Stage(
                "files",
                "1. Files",
                description="The files to analyse.",
                instruction_cards=(
                    StageInstruction("Open a project: pick one at the top, or browse for it.",
                                     guard="not project_ready"),
                    StageInstruction("Add the videos or image folders to analyse.", guard="not has_matrix_rows"),
                    StageInstruction("Tick Use on the files to run and name the samples, then go to 2. Analyse."),
                ),
                settings_panel=SettingsSpec("matrix", title="Files"),
                actions=(
                    StageAction("Add files…", "browse_source", guard="project_ready"),
                    StageAction("Clear list", "clear_matrix", guard="has_matrix_rows", variant="secondary"),
                ),
                editor=EditorSpec("file_facts"),
            ),
            Stage(
                "analyse",
                "2. Analyse",
                action="analyze",
                description="Set each file up and run.",
                instruction_cards=(
                    StageInstruction("No files ticked. Tick Use on 1. Files.", guard="not has_targets"),
                    StageInstruction("Fill in µm/px and FPS for every ticked file: the red cells are missing.",
                                     guard="not targets_ready"),
                    StageInstruction("Select a file, set its crop, frames and detection on the preview, then Run."),
                ),
                settings_panel=SettingsSpec("matrix", title="Ticked files"),
                actions=(
                    StageAction("Run all ticked", "analyze", guard="targets_ready", completes=True),
                    StageAction("Run selected", "analyze_selected", guard="selected_ready", variant="secondary"),
                    StageAction("Reset file settings", "reset_row_settings", guard="has_selected",
                                variant="secondary"),
                ),
                editor=EditorSpec("file_editor"),
            ),
            Stage(
                "results",
                "3. Results",
                description="What the analysis found.",
                instruction_cards=(
                    StageInstruction("Nothing analysed yet. Run the ticked files on 2. Analyse.",
                                     guard="not has_results"),
                    StageInstruction("View only. Export takes the ticked files' results."),
                ),
                settings_panel=SettingsSpec("results", title="Per file"),
                actions=(
                    StageAction("Export CSV", "export_csv", guard="has_results"),
                    StageAction("Export plots", "export_plots", guard="has_results", variant="secondary"),
                ),
                editor=EditorSpec("results_plots"),
            ),
        ),
    )
