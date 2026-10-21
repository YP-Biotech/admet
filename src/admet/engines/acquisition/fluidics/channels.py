from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .sdk import FluigentSDK

log = logging.getLogger(__name__)


@dataclass
class ChannelState:
    sensor_index: int
    pressure_index: int
    base_setpoint: float = 0.0
    active_setpoint: float = 0.0
    owner: str = "user"
    regulation_active: bool = False
    mode: str = "off"
    pressure_setpoint: float = 0.0
    # When the channel was last told what to do, by hand or by a protocol.
    command_started: float = 0.0


class ChannelManager:
    """Who controls each channel, and its flow regulation.

    On real hardware ADMET feeds the regulation itself: every poll it hands the
    SDK the reading and the setpoint (fgt_set_customSensorRegulation). The unit's
    own regulation is never started, so its reaction to a negative flow, dropping
    the pressure to zero, never happens. ADMET decides instead: while the unit
    reads a negative flow, the regulation is told it is on target, so it holds the
    pressure where it is. The simulator does not move its flow with pressure, so
    there the unit's own regulation is used.
    """

    def __init__(self, sdk: FluigentSDK):
        self._sdk = sdk
        self._lock = threading.Lock()
        self._channels: list[ChannelState] = []
        self._pipeline_paused = False
        self.admet_regulation = False
        self._ranges: dict[int, float] = {}
        self._flows: dict[int, float] = {}

    def set_regulation(self, *, admet: bool, ranges: dict[int, float]) -> None:
        """Whether ADMET feeds the regulation, and each sensor's range it regulates within."""
        with self._lock:
            self.admet_regulation = admet
            self._ranges = dict(ranges)

    def feed_regulation(self, flows: list[float]) -> None:
        """One regulation step for every flow-regulated channel, from this poll's readings."""
        with self._lock:
            self._flows = dict(enumerate(flows))
            if not self.admet_regulation:
                return
            for channel in self._channels:
                if channel.mode == "flow" and channel.regulation_active:
                    self._feed(channel)

    def _feed(self, channel: ChannelState) -> None:
        flow = self._flows.get(channel.sensor_index)
        if flow is None:
            flow = self._flows[channel.sensor_index] = float(self._sdk.get_sensor_value(channel.sensor_index))
        measure = flow if flow >= 0 else channel.active_setpoint
        self._sdk.set_custom_regulation(measure, channel.active_setpoint,
                                        self._ranges.get(channel.sensor_index, 0.0), channel.pressure_index)

    def _regulate(self, channel: ChannelState, setpoint: float) -> None:
        """Regulate the channel's flow to setpoint: fed by ADMET, or by the unit itself in simulation."""
        if self.admet_regulation:
            self._feed(channel)
        else:
            self._sdk.set_sensor_regulation(channel.sensor_index, channel.pressure_index, setpoint)

    @property
    def channels(self) -> list[ChannelState]:
        with self._lock:
            return list(self._channels)

    def configure_channels(self, sensor_pressure_pairs: list[tuple[int, int]]) -> None:
        with self._lock:
            self._channels = [
                ChannelState(sensor_index=sensor_index, pressure_index=pressure_index)
                for sensor_index, pressure_index in sensor_pressure_pairs
            ]

    def user_set_flow_regulation(
        self,
        channel_idx: int,
        setpoint: float,
        *,
        activate: bool = True,
    ) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)
            channel.base_setpoint = setpoint
            channel.pressure_setpoint = 0.0

            channel.active_setpoint = setpoint
            channel.mode = "flow"
            if activate:
                channel.regulation_active = True
                self._regulate(channel, setpoint)
                channel.command_started = time.monotonic()

    def user_set_pressure(self, channel_idx: int, pressure_mbar: float) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)

            channel.mode = "pressure"
            channel.pressure_setpoint = pressure_mbar
            channel.base_setpoint = 0.0
            channel.active_setpoint = 0.0
            channel.regulation_active = False
            self._sdk.set_pressure(channel.pressure_index, pressure_mbar)
            channel.command_started = time.monotonic()

    def user_zero(self, channel_idx: int) -> None:
        """Stop a channel by what controls it: flow regulated to 0, or pressure to 0."""
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)
            channel.base_setpoint = channel.active_setpoint = 0.0
            if channel.mode == "flow":
                channel.regulation_active = True
                self._regulate(channel, 0.0)
                channel.command_started = time.monotonic()
            else:
                channel.pressure_setpoint = 0.0
                self._sdk.set_pressure(channel.pressure_index, 0.0)
                channel.command_started = time.monotonic()

    def user_stop_regulation(self, channel_idx: int) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)

            channel.regulation_active = False
            channel.base_setpoint = 0.0
            channel.active_setpoint = 0.0
            channel.mode = "off"
            channel.pressure_setpoint = 0.0
            self._sdk.set_pressure(channel.pressure_index, 0.0)
            channel.command_started = time.monotonic()

    def _require_user_control(self, channel):
        if channel.owner != "user" and not self._pipeline_paused:
            raise RuntimeError("Channel is controlled by the protocol; pause or finish it first")

    def pipeline_set_setpoint(self, channel_idx: int, setpoint: float) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            channel.owner = "pipeline"
            channel.active_setpoint = setpoint
            channel.regulation_active = True
            channel.mode = "flow"
            channel.pressure_setpoint = 0.0
            self._regulate(channel, setpoint)
            channel.command_started = time.monotonic()

    def pipeline_set_pressure(self, channel_idx: int, pressure_mbar: float) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            channel.owner = "pipeline"
            channel.active_setpoint = 0.0
            channel.regulation_active = False
            channel.mode = "pressure"
            channel.pressure_setpoint = pressure_mbar
            self._sdk.set_pressure(channel.pressure_index, pressure_mbar)
            channel.command_started = time.monotonic()

    def pipeline_release_channel(self, channel_idx: int) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            if channel.owner != "pipeline":
                return

            if channel.mode == "pressure":
                self._sdk.set_pressure(channel.pressure_index, 0.0)
                channel.command_started = time.monotonic()
            channel.owner = "user"
            channel.active_setpoint = channel.base_setpoint
            if channel.base_setpoint > 0 or channel.regulation_active:
                channel.mode = "flow"
                self._regulate(channel, channel.base_setpoint)
                channel.command_started = time.monotonic()
            else:
                channel.regulation_active = False
                channel.mode = "off"
                channel.pressure_setpoint = 0.0

    def pipeline_zero_all(self) -> None:
        with self._lock:
            self._pipeline_paused = True
            for channel in self._channels:
                if channel.owner == "pipeline":
                    channel.active_setpoint = 0.0
                    if channel.mode == "pressure":
                        channel.pressure_setpoint = 0.0
                        self._sdk.set_pressure(channel.pressure_index, 0.0)
                        channel.command_started = time.monotonic()
                    else:
                        self._regulate(channel, 0.0)
                        channel.command_started = time.monotonic()

    def pipeline_resume_all(self) -> None:
        with self._lock:
            self._pipeline_paused = False

    def pipeline_release_all(self) -> None:
        with self._lock:
            self._pipeline_paused = False
            for channel in self._channels:
                if channel.owner == "pipeline":
                    if channel.mode == "pressure":
                        self._sdk.set_pressure(channel.pressure_index, 0.0)
                        channel.command_started = time.monotonic()
                    else:
                        channel.active_setpoint = 0.0
                        self._regulate(channel, 0.0)
                        channel.command_started = time.monotonic()
                    channel.owner = "user"
                    channel.active_setpoint = 0.0
                    channel.pressure_setpoint = 0.0
                    # The unit's own regulation keeps holding 0 by itself; ADMET's must keep being fed.
                    keep = self.admet_regulation and channel.mode == "flow"
                    channel.regulation_active = keep
                    channel.mode = "flow" if keep else "off"

    def emergency_stop_all(self) -> None:
        with self._lock:
            for channel in self._channels:
                self._sdk.set_pressure(channel.pressure_index, 0.0)
                channel.command_started = time.monotonic()
                channel.active_setpoint = 0.0
                channel.regulation_active = False
                channel.owner = "user"
                channel.mode = "off"
                channel.pressure_setpoint = 0.0
                channel.base_setpoint = 0.0
