from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResultRecord:
    sample_id: str
    engine: str
    values: dict[str, Any]


@dataclass(frozen=True)
class SummaryStat:
    name: str
    value: float | int | str
    unit: str = ""


@dataclass(frozen=True)
class ResultSet:
    records: tuple[ResultRecord, ...] = field(default_factory=tuple)
    stats: tuple[SummaryStat, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

