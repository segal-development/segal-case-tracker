"""
PJUD Selector Registry package.

Provides YAML-based selector configuration with hot-reload capability.
"""

from app.scrapper.pjud.selectors.registry import SelectorRegistry, SelectorConfig


__all__ = [
    "SelectorRegistry",
    "SelectorConfig",
]
