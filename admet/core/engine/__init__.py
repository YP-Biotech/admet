"""Engine contracts and lazy registry."""

from .base import Engine, EngineContext, EngineResult
from .registry import EngineRegistry, LazyEngineSpec

__all__ = ["Engine", "EngineContext", "EngineRegistry", "EngineResult", "LazyEngineSpec"]

