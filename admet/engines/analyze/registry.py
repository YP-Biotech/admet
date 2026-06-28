from __future__ import annotations

from admet.core.engine import EngineRegistry, LazyEngineSpec
from admet.engines.dummy import create_engine as create_dummy_engine


def create_analyze_registry() -> EngineRegistry:
    registry = EngineRegistry()
    registry.register("dummy", create_dummy_engine)
    registry.register_lazy(LazyEngineSpec("opencv", "admet.engines.analyze.opencv"))
    registry.register_lazy(LazyEngineSpec("cellpose", "admet.engines.analyze.cellpose"))
    return registry
