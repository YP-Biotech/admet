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
                    StageInstruction("Add a folder of runs (a data or export folder), or single videos and image "
                                     "folders, to analyse.", guard="not has_matrix_rows"),
                    StageInstruction("Samples are listed under their generation condition, with their video, "
                                     "images and live QC. Tick Use on the ones to take on; put an image folder "
                                     "into its sample from the picker. Then go to 2. Analyse."),
                ),
                settings_panel=SettingsSpec("matrix", title="Files"),
                actions=(
                    StageAction("Add folder…", "browse_folder"),
                    StageAction("Add files…", "browse_source", variant="secondary"),
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
                    StageInstruction("Analyse all runs every ticked file that has no result yet or whose detection "
                                     "settings changed since. Select a file only to crop it or change its detection."),
                ),
                settings_panel=SettingsSpec("matrix", title="Ticked files"),
                actions=(
                    StageAction("Analyse all", "analyze", guard="targets_ready", completes=True),
                    StageAction("Run selected", "analyze_selected", guard="selected_ready", variant="secondary"),
                    StageAction("Stop", "stop_analysis", guard="running", variant="warning"),
                    StageAction("Reset ticked", "reset_ticked_settings", guard="has_targets", variant="secondary"),
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
                    StageAction("Export…", "export_csv", guard="has_results"),
                    StageAction("Export plots", "export_plots", guard="has_results", variant="secondary"),
                ),
                editor=EditorSpec("results_plots"),
            ),
        ),
    )
