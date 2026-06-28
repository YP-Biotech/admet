from __future__ import annotations

from dataclasses import dataclass

ACQUISITION_INTERVAL_MS = 100
STATS_WINDOW_SAMPLES = 300
STABILITY_TOLERANCE_UL_MIN = 2.0
STABILITY_DURATION_S = 5.0
STABILITY_WINDOW_SAMPLES = int(STABILITY_DURATION_S / (ACQUISITION_INTERVAL_MS / 1000.0))

PIPELINE_TICK_MS = 200

SIM_INSTR_TYPE = 4
SIM_INSTRUMENTS = [
    {"serial": 1001, "config": [1, 100, 0, 5, 7, 0, 0, 0, 0, 0]},
    {"serial": 1002, "config": [1, 101, 0, 5, 4, 0, 0, 0, 0, 0]},
    {"serial": 1003, "config": [1, 102, 0, 5, 4, 0, 0, 0, 0, 0]},
]

SENSOR_REAL_SMAX = {
    "Flow_L": 5000.0,
    "Flow_M": 80.0,
}

PRESSURE_CHANNEL_NAMES = [
    "Oil Pressure",
    "Cells Pressure",
    "Beads Pressure",
]
SENSOR_CHANNEL_NAMES = [
    "Oil Flow (L)",
    "Cells Flow (M)",
    "Beads Flow (M)",
]

SENSOR_CALIBRATIONS = {
    "None": 0,
    "H2O": 1,
    "IPA": 2,
    "HFE": 3,
    "FC40": 4,
    "Oil": 5,
}


@dataclass(frozen=True)
class ProtocolStep:
    name: str
    sensor_setpoints: dict[int, float]
    trigger_type: str
    trigger_params: dict
    on_complete: str = "hold"
    confirm_message: str = ""
    repeat: int = 1
    group: str = ""


PIPELINES: dict[str, list[ProtocolStep]] = {
    "Drop-Seq": [
        ProtocolStep(
            name="Prerun",
            sensor_setpoints={0: 250.0, 1: 67.0, 2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 75.0},
            on_complete="zero",
        ),
        ProtocolStep(
            name="Run-prestab",
            sensor_setpoints={0: 250.0, 1: 0.0, 2: 0.0},
            trigger_type="condition",
            trigger_params={"sensor_index": 0, "min_value": 125.0},
            confirm_message="Prerun complete. Start stabilization?",
            group="run",
            repeat=3,
        ),
        ProtocolStep(
            name="Run-stab",
            sensor_setpoints={0: 250.0, 1: 67.0, 2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 250.0},
            on_complete="zero",
            group="run",
            repeat=3,
        ),
    ],
    "Priming": [
        ProtocolStep(
            name="Prime Oil",
            sensor_setpoints={0: 250.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 40.0},
            on_complete="zero",
            confirm_message="Prime Oil Flow (L) at 250 ul/min for 40 ul. Proceed?",
        ),
        ProtocolStep(
            name="Prime Cells",
            sensor_setpoints={1: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 1, "target_volume_ul": 5.0},
            on_complete="zero",
            confirm_message="Prime Cells Flow (M) at 67 ul/min for 5 ul. Proceed?",
        ),
        ProtocolStep(
            name="Prime Beads",
            sensor_setpoints={2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 2, "target_volume_ul": 5.0},
            on_complete="zero",
            confirm_message="Prime Beads Flow (M) at 67 ul/min for 5 ul. Proceed?",
        ),
    ],
}
