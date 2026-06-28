from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


class FluigentSDKUnavailableError(ImportError):
    """Raised when the Fluigent SDK is required but unavailable."""


@dataclass(frozen=True)
class PressureChannelInfo:
    index: int
    controller_sn: int
    device_sn: int
    position: int
    instr_type: str
    pmin: float = 0.0
    pmax: float = 0.0


@dataclass
class SensorChannelInfo:
    index: int
    controller_sn: int
    device_sn: int
    position: int
    instr_type: str
    sensor_type: str
    smin: float = 0.0
    smax: float = 0.0


class FluigentSDK:
    def __init__(self, sdk_module: Any | None = None):
        self._sdk = sdk_module
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def create_simulated_instrument(
        self,
        instr_type: int,
        serial: int,
        firmware: int,
        config: list[int],
    ) -> None:
        self._module().fgt_create_simulated_instr(instr_type, serial, firmware, config)

    def remove_simulated_instrument(self, instr_type: int, serial: int) -> None:
        self._module().fgt_remove_simulated_instr(instr_type, serial)

    def init(self, instruments: list[int] | None = None) -> None:
        self._module().fgt_init(instruments)
        self._initialized = True

    def close(self) -> None:
        if self._initialized:
            self._module().fgt_close()
            self._initialized = False

    def get_controllers_info(self) -> list[dict]:
        return [
            {
                "sn": controller.SN,
                "firmware": controller.Firmware,
                "index": controller.index,
                "type": str(controller.InstrType),
            }
            for controller in self._module().fgt_get_controllersInfo()
        ]

    def get_pressure_channel_count(self) -> int:
        return self._module().fgt_get_pressureChannelCount()

    def get_sensor_channel_count(self) -> int:
        return self._module().fgt_get_sensorChannelCount()

    def get_pressure_channels_info(self) -> list[PressureChannelInfo]:
        module = self._module()
        channels = module.fgt_get_pressureChannelsInfo()
        result = []
        for channel in channels:
            pmin, pmax = module.fgt_get_pressureRange(channel.index)
            result.append(
                PressureChannelInfo(
                    index=channel.index,
                    controller_sn=channel.ControllerSN,
                    device_sn=channel.DeviceSN,
                    position=channel.position,
                    instr_type=str(channel.InstrType),
                    pmin=pmin,
                    pmax=pmax,
                )
            )
        return result

    def get_sensor_channels_info(self) -> list[SensorChannelInfo]:
        module = self._module()
        channels, sensor_types = module.fgt_get_sensorChannelsInfo()
        result = []
        for channel, sensor_type in zip(channels, sensor_types, strict=False):
            smin, smax = module.fgt_get_sensorRange(channel.index)
            result.append(
                SensorChannelInfo(
                    index=channel.index,
                    controller_sn=channel.ControllerSN,
                    device_sn=channel.DeviceSN,
                    position=channel.position,
                    instr_type=str(channel.InstrType),
                    sensor_type=str(sensor_type),
                    smin=smin,
                    smax=smax,
                )
            )
        return result

    def set_pressure(self, pressure_index: int, pressure: float) -> None:
        self._module().fgt_set_pressure(pressure_index, pressure)

    def get_pressure(self, pressure_index: int) -> float:
        return float(self._module().fgt_get_pressure(pressure_index))

    def set_sensor_regulation(
        self,
        sensor_index: int,
        pressure_index: int,
        setpoint: float,
    ) -> None:
        self._module().fgt_set_sensorRegulation(sensor_index, pressure_index, setpoint)

    def get_sensor_value(self, sensor_index: int) -> float:
        return float(self._module().fgt_get_sensorValue(sensor_index))

    def calibrate_pressure(self, pressure_index: int) -> None:
        self._module().fgt_calibratePressure(pressure_index)

    def set_sensor_calibration(self, sensor_index: int, calibration: int) -> None:
        module = self._module()
        module.fgt_set_sensorCalibration(
            sensor_index,
            module.fgt_SENSOR_CALIBRATION(calibration),
        )

    def get_sensor_calibration(self, sensor_index: int) -> int:
        return int(self._module().fgt_get_sensorCalibration(sensor_index))

    def set_sensor_regulation_response(self, sensor_index: int, response_time: int) -> None:
        self._module().fgt_set_sensorRegulationResponse(sensor_index, response_time)

    def set_sensor_custom_scale(
        self,
        sensor_index: int,
        a: float,
        b: float = 0.0,
        c: float = 0.0,
        smax: float | None = None,
    ) -> None:
        self._module().fgt_set_sensorCustomScale(sensor_index, a, b, c, smax=smax)

    def _module(self):
        if self._sdk is not None:
            return self._sdk
        try:
            self._sdk = importlib.import_module("Fluigent.SDK")
        except ImportError as exc:
            raise FluigentSDKUnavailableError(
                "Fluigent SDK is required for hardware fluidics control."
            ) from exc
        return self._sdk
