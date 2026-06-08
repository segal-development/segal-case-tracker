"""
Selector Registry for PJUD scrapers.

Loads selectors from YAML files with support for:
- Primary selectors with fallback chains
- Hot-reload via reload() method
- Validation on load
- Graceful fallback to last-known-good config on parse errors
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

import yaml


logger = logging.getLogger(__name__)


@dataclass
class SelectorConfig:
    """Configuration for a single selector with optional fallbacks."""
    primary: str
    fallbacks: List[str] = field(default_factory=list)
    description: str = ""
    
    def get_all(self) -> List[str]:
        """Return primary + all fallbacks in order."""
        return [self.primary] + self.fallbacks


class SelectorValidationError(Exception):
    """Raised when selector YAML fails validation."""
    pass


class SelectorRegistry:
    """
    Registry for managing PJUD selectors from YAML files.
    
    Usage:
        registry = SelectorRegistry()
        registry.load("civil")
        
        selector = registry.get("civil", "table_body_id")
        # Returns: "verDetalleMisCauCiv"
        
        all_selectors = registry.get_with_fallbacks("civil", "table_body_id")
        # Returns: ["verDetalleMisCauCiv", "verDetalleCiv_alt"]
    """
    
    # Default selectors directory (bundled with package)
    DEFAULT_SELECTORS_PATH = Path(__file__).parent
    
    def __init__(self, selectors_path: Optional[Path] = None):
        """
        Initialize the selector registry.
        
        Args:
            selectors_path: Path to directory containing YAML files.
                          Defaults to package directory, can be overridden
                          via PJUD_SELECTORS_PATH environment variable.
        """
        # Priority: explicit param > env var > default
        if selectors_path:
            self._selectors_path = Path(selectors_path)
        elif os.environ.get("PJUD_SELECTORS_PATH"):
            self._selectors_path = Path(os.environ["PJUD_SELECTORS_PATH"])
        else:
            self._selectors_path = self.DEFAULT_SELECTORS_PATH
        
        # Cache: competencia -> {selector_name -> SelectorConfig}
        self._cache: Dict[str, Dict[str, SelectorConfig]] = {}
        
        # Last-known-good cache for fallback on parse errors
        self._last_good: Dict[str, Dict[str, SelectorConfig]] = {}
        
        logger.info(f"SelectorRegistry initialized with path: {self._selectors_path}")
    
    @property
    def selectors_path(self) -> Path:
        """Return the current selectors directory path."""
        return self._selectors_path
    
    def load(self, competencia: str, *, raise_on_error: bool = True) -> bool:
        """
        Load selectors for a competency from its YAML file.
        
        Args:
            competencia: Competency name (e.g., "civil", "laboral")
            raise_on_error: If True, raises on validation errors.
                          If False, falls back to last-known-good.
        
        Returns:
            True if loaded successfully, False if fell back to cache.
        
        Raises:
            FileNotFoundError: If YAML file doesn't exist and no cache.
            SelectorValidationError: If YAML is invalid and raise_on_error=True.
        """
        yaml_path = self._selectors_path / f"{competencia}.yaml"
        
        try:
            if not yaml_path.exists():
                # Check if we have cached data
                if competencia in self._last_good:
                    logger.warning(
                        f"YAML file not found: {yaml_path}, using last-known-good config"
                    )
                    self._cache[competencia] = self._last_good[competencia]
                    return False
                raise FileNotFoundError(f"Selector file not found: {yaml_path}")
            
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            
            if not isinstance(data, dict):
                raise SelectorValidationError(
                    f"Invalid YAML structure in {yaml_path}: expected dict at root"
                )
            
            # Parse and validate selectors
            selectors = self._parse_selectors(data, competencia)
            
            # Validate required selectors exist
            self._validate_required_selectors(selectors, competencia)
            
            # Success - update both caches
            self._cache[competencia] = selectors
            self._last_good[competencia] = selectors.copy()
            
            logger.info(
                f"Loaded {len(selectors)} selectors for '{competencia}' from {yaml_path}"
            )
            return True
            
        except (yaml.YAMLError, SelectorValidationError) as e:
            logger.error(f"Error loading selectors for '{competencia}': {e}")
            
            if raise_on_error:
                raise SelectorValidationError(str(e)) from e
            
            # Fallback to last-known-good
            if competencia in self._last_good:
                logger.warning(f"Falling back to last-known-good config for '{competencia}'")
                self._cache[competencia] = self._last_good[competencia]
                return False
            
            # No fallback available
            raise SelectorValidationError(
                f"Failed to load '{competencia}' and no fallback available: {e}"
            ) from e
    
    def _parse_selectors(
        self, data: Dict[str, Any], competencia: str
    ) -> Dict[str, SelectorConfig]:
        """Parse raw YAML data into SelectorConfig objects."""
        selectors = {}
        
        for name, value in data.items():
            if isinstance(value, str):
                # Simple format: just the selector string
                selectors[name] = SelectorConfig(primary=value)
            elif isinstance(value, dict):
                # Full format with primary/fallbacks/description
                if "primary" not in value:
                    raise SelectorValidationError(
                        f"Selector '{name}' in '{competencia}' missing 'primary' field"
                    )
                
                selectors[name] = SelectorConfig(
                    primary=value["primary"],
                    fallbacks=value.get("fallbacks", []),
                    description=value.get("description", ""),
                )
            else:
                raise SelectorValidationError(
                    f"Invalid selector format for '{name}' in '{competencia}': "
                    f"expected str or dict, got {type(value).__name__}"
                )
        
        return selectors
    
    def _validate_required_selectors(
        self, selectors: Dict[str, SelectorConfig], competencia: str
    ) -> None:
        """
        Validate that required selectors exist for the competency.
        
        Required for all competencies:
        - table_body_id
        - modal_id  
        - detail_function_name
        """
        required = {"table_body_id", "modal_id", "detail_function_name"}
        missing = required - set(selectors.keys())
        
        if missing:
            raise SelectorValidationError(
                f"Missing required selectors for '{competencia}': {missing}"
            )
    
    def reload(self, competencia: Optional[str] = None) -> Dict[str, bool]:
        """
        Reload selectors from YAML files.
        
        Args:
            competencia: Specific competency to reload, or None for all loaded.
        
        Returns:
            Dict mapping competencia -> success status.
        """
        results = {}
        
        if competencia:
            # Reload specific competency
            try:
                results[competencia] = self.load(competencia, raise_on_error=False)
            except Exception as e:
                logger.error(f"Failed to reload '{competencia}': {e}")
                results[competencia] = False
        else:
            # Reload all currently loaded competencies
            for comp in list(self._cache.keys()):
                try:
                    results[comp] = self.load(comp, raise_on_error=False)
                except Exception as e:
                    logger.error(f"Failed to reload '{comp}': {e}")
                    results[comp] = False
        
        return results
    
    def get(self, competencia: str, selector_name: str) -> str:
        """
        Get the primary selector value.
        
        Args:
            competencia: Competency name
            selector_name: Selector key
        
        Returns:
            Primary selector string.
        
        Raises:
            KeyError: If competency not loaded or selector not found.
        """
        if competencia not in self._cache:
            # Try to load on-demand
            self.load(competencia)
        
        if selector_name not in self._cache[competencia]:
            raise KeyError(
                f"Selector '{selector_name}' not found for competency '{competencia}'"
            )
        
        return self._cache[competencia][selector_name].primary
    
    def get_with_fallbacks(self, competencia: str, selector_name: str) -> List[str]:
        """
        Get selector with all fallbacks in priority order.
        
        Args:
            competencia: Competency name
            selector_name: Selector key
        
        Returns:
            List of selectors: [primary, fallback1, fallback2, ...]
        
        Raises:
            KeyError: If competency not loaded or selector not found.
        """
        if competencia not in self._cache:
            self.load(competencia)
        
        if selector_name not in self._cache[competencia]:
            raise KeyError(
                f"Selector '{selector_name}' not found for competency '{competencia}'"
            )
        
        return self._cache[competencia][selector_name].get_all()
    
    def get_config(self, competencia: str, selector_name: str) -> SelectorConfig:
        """
        Get the full SelectorConfig object.
        
        Args:
            competencia: Competency name
            selector_name: Selector key
        
        Returns:
            SelectorConfig with primary, fallbacks, and description.
        
        Raises:
            KeyError: If competency not loaded or selector not found.
        """
        if competencia not in self._cache:
            self.load(competencia)
        
        if selector_name not in self._cache[competencia]:
            raise KeyError(
                f"Selector '{selector_name}' not found for competency '{competencia}'"
            )
        
        return self._cache[competencia][selector_name]
    
    def has_selector(self, competencia: str, selector_name: str) -> bool:
        """Check if a selector exists for the competency."""
        try:
            if competencia not in self._cache:
                self.load(competencia)
            return selector_name in self._cache.get(competencia, {})
        except Exception:
            return False
    
    def list_selectors(self, competencia: str) -> List[str]:
        """List all available selector names for a competency."""
        if competencia not in self._cache:
            self.load(competencia)
        return list(self._cache.get(competencia, {}).keys())
    
    def is_loaded(self, competencia: str) -> bool:
        """Check if a competency's selectors are currently loaded."""
        return competencia in self._cache
    
    def clear(self, competencia: Optional[str] = None) -> None:
        """
        Clear cached selectors.
        
        Args:
            competencia: Specific competency to clear, or None for all.
        """
        if competencia:
            self._cache.pop(competencia, None)
        else:
            self._cache.clear()
