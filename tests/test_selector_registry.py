"""
Tests for PJUD Selector Registry.

Tests:
1. YAML loading and selector retrieval
2. Fallback when primary selector missing
3. reload() updates in-memory selectors
4. Invalid YAML retains previous config
5. Validation errors for missing required selectors
6. CivilScraper integration with registry
"""

import pytest
import tempfile
import os
from pathlib import Path

from app.scrapper.pjud.selectors import SelectorRegistry, SelectorConfig
from app.scrapper.pjud.selectors.registry import SelectorValidationError
from app.scrapper.pjud import CivilScraper, get_selector_registry, reload_selectors


class TestSelectorConfig:
    """Test the SelectorConfig dataclass."""
    
    def test_simple_selector(self):
        """SelectorConfig with just primary selector."""
        config = SelectorConfig(primary="#test")
        
        assert config.primary == "#test"
        assert config.fallbacks == []
        assert config.description == ""
    
    def test_selector_with_fallbacks(self):
        """SelectorConfig with fallback chain."""
        config = SelectorConfig(
            primary="#main",
            fallbacks=["#alt1", "#alt2"],
            description="Test selector",
        )
        
        assert config.primary == "#main"
        assert len(config.fallbacks) == 2
        assert config.description == "Test selector"
    
    def test_get_all_returns_priority_order(self):
        """get_all() returns primary first, then fallbacks."""
        config = SelectorConfig(
            primary="#main",
            fallbacks=["#alt1", "#alt2"],
        )
        
        all_selectors = config.get_all()
        
        assert all_selectors == ["#main", "#alt1", "#alt2"]


class TestSelectorRegistryLoad:
    """Test YAML loading functionality."""
    
    def test_load_valid_yaml(self, tmp_path):
        """Registry loads valid YAML file."""
        yaml_content = """
table_body_id:
  primary: "#testTable"
  fallbacks:
    - "#altTable"
  description: "Test table"

modal_id:
  primary: "#testModal"

detail_function_name:
  primary: "testDetail"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        result = registry.load("test")
        
        assert result is True
        assert registry.get("test", "table_body_id") == "#testTable"
        assert registry.get("test", "modal_id") == "#testModal"
    
    def test_load_simple_format(self, tmp_path):
        """Registry loads simple string format."""
        yaml_content = """
table_body_id: "#simpleTable"
modal_id: "#simpleModal"
detail_function_name: "simpleDetail"
"""
        yaml_file = tmp_path / "simple.yaml"
        yaml_file.write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        registry.load("simple")
        
        assert registry.get("simple", "table_body_id") == "#simpleTable"
    
    def test_load_file_not_found_raises(self, tmp_path):
        """Registry raises FileNotFoundError for missing file."""
        registry = SelectorRegistry(selectors_path=tmp_path)
        
        with pytest.raises(FileNotFoundError):
            registry.load("nonexistent")
    
    def test_load_invalid_yaml_raises(self, tmp_path):
        """Registry raises error for invalid YAML structure."""
        yaml_content = """
table_body_id:
  - invalid
  - list
  - format
modal_id: "#valid"
detail_function_name: "valid"
"""
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        
        with pytest.raises(SelectorValidationError):
            registry.load("invalid")
    
    def test_load_missing_required_raises(self, tmp_path):
        """Registry raises error for missing required selectors."""
        yaml_content = """
table_body_id:
  primary: "#table"
# Missing modal_id and detail_function_name
"""
        yaml_file = tmp_path / "incomplete.yaml"
        yaml_file.write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        
        with pytest.raises(SelectorValidationError) as exc_info:
            registry.load("incomplete")
        
        assert "Missing required selectors" in str(exc_info.value)


class TestSelectorRegistryFallback:
    """Test fallback behavior on errors."""
    
    def test_fallback_to_last_good_on_error(self, tmp_path):
        """Registry falls back to last-known-good on parse error."""
        # First, load valid config
        valid_yaml = """
table_body_id:
  primary: "#goodTable"
modal_id:
  primary: "#goodModal"
detail_function_name:
  primary: "goodDetail"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(valid_yaml)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        registry.load("test")
        
        # Now corrupt the file
        yaml_file.write_text("invalid: [yaml: structure")
        
        # Reload with raise_on_error=False should fallback
        result = registry.load("test", raise_on_error=False)
        
        assert result is False  # Indicates fallback was used
        assert registry.get("test", "table_body_id") == "#goodTable"
    
    def test_fallback_when_file_missing_but_cached(self, tmp_path):
        """Registry uses cache when file is deleted."""
        valid_yaml = """
table_body_id:
  primary: "#cachedTable"
modal_id:
  primary: "#cachedModal"
detail_function_name:
  primary: "cachedDetail"
"""
        yaml_file = tmp_path / "cached.yaml"
        yaml_file.write_text(valid_yaml)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        registry.load("cached")
        
        # Delete the file
        yaml_file.unlink()
        
        # Reload should use cache
        result = registry.load("cached", raise_on_error=False)
        
        assert result is False
        assert registry.get("cached", "table_body_id") == "#cachedTable"


class TestSelectorRegistryReload:
    """Test hot-reload functionality."""
    
    def test_reload_updates_selectors(self, tmp_path):
        """reload() picks up changes from file."""
        original_yaml = """
table_body_id:
  primary: "#original"
modal_id:
  primary: "#modal"
detail_function_name:
  primary: "detail"
"""
        yaml_file = tmp_path / "update.yaml"
        yaml_file.write_text(original_yaml)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        registry.load("update")
        assert registry.get("update", "table_body_id") == "#original"
        
        # Update the file
        updated_yaml = """
table_body_id:
  primary: "#updated"
modal_id:
  primary: "#modal"
detail_function_name:
  primary: "detail"
"""
        yaml_file.write_text(updated_yaml)
        
        # Reload
        results = registry.reload("update")
        
        assert results["update"] is True
        assert registry.get("update", "table_body_id") == "#updated"
    
    def test_reload_all_loaded_competencies(self, tmp_path):
        """reload() without args reloads all loaded competencies."""
        for comp in ["comp1", "comp2"]:
            yaml_content = f"""
table_body_id:
  primary: "#{comp}_table"
modal_id:
  primary: "#{comp}_modal"
detail_function_name:
  primary: "{comp}_detail"
"""
            (tmp_path / f"{comp}.yaml").write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        registry.load("comp1")
        registry.load("comp2")
        
        results = registry.reload()
        
        assert "comp1" in results
        assert "comp2" in results
        assert all(results.values())


class TestSelectorRegistryGetters:
    """Test selector retrieval methods."""
    
    @pytest.fixture
    def loaded_registry(self, tmp_path):
        """Create registry with loaded test selectors."""
        yaml_content = """
table_body_id:
  primary: "#mainTable"
  fallbacks:
    - "#altTable"
    - "#legacyTable"
  description: "Case listing table"

modal_id:
  primary: "#detailModal"

detail_function_name:
  primary: "showDetail"
"""
        (tmp_path / "test.yaml").write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        registry.load("test")
        return registry
    
    def test_get_returns_primary(self, loaded_registry):
        """get() returns primary selector."""
        assert loaded_registry.get("test", "table_body_id") == "#mainTable"
    
    def test_get_with_fallbacks(self, loaded_registry):
        """get_with_fallbacks() returns all selectors in order."""
        selectors = loaded_registry.get_with_fallbacks("test", "table_body_id")
        assert selectors == ["#mainTable", "#altTable", "#legacyTable"]
    
    def test_get_config_returns_full_object(self, loaded_registry):
        """get_config() returns SelectorConfig object."""
        config = loaded_registry.get_config("test", "table_body_id")
        
        assert isinstance(config, SelectorConfig)
        assert config.primary == "#mainTable"
        assert config.description == "Case listing table"
    
    def test_get_raises_for_unknown_selector(self, loaded_registry):
        """get() raises KeyError for unknown selector."""
        with pytest.raises(KeyError) as exc_info:
            loaded_registry.get("test", "unknown_selector")
        
        assert "unknown_selector" in str(exc_info.value)
    
    def test_get_loads_on_demand(self, tmp_path):
        """get() auto-loads competency if not loaded."""
        yaml_content = """
table_body_id:
  primary: "#onDemand"
modal_id:
  primary: "#modal"
detail_function_name:
  primary: "detail"
"""
        (tmp_path / "ondemand.yaml").write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=tmp_path)
        # Don't explicitly load
        
        # Should auto-load on first get
        result = registry.get("ondemand", "table_body_id")
        assert result == "#onDemand"
    
    def test_has_selector_returns_true(self, loaded_registry):
        """has_selector() returns True for existing selector."""
        assert loaded_registry.has_selector("test", "table_body_id") is True
    
    def test_has_selector_returns_false(self, loaded_registry):
        """has_selector() returns False for missing selector."""
        assert loaded_registry.has_selector("test", "nonexistent") is False
    
    def test_list_selectors(self, loaded_registry):
        """list_selectors() returns all selector names."""
        names = loaded_registry.list_selectors("test")
        
        assert "table_body_id" in names
        assert "modal_id" in names
        assert "detail_function_name" in names
    
    def test_is_loaded(self, loaded_registry):
        """is_loaded() returns correct status."""
        assert loaded_registry.is_loaded("test") is True
        assert loaded_registry.is_loaded("unknown") is False


class TestSelectorRegistryEnvConfig:
    """Test environment variable configuration."""
    
    def test_uses_env_var_path(self, tmp_path, monkeypatch):
        """Registry uses PJUD_SELECTORS_PATH env var."""
        monkeypatch.setenv("PJUD_SELECTORS_PATH", str(tmp_path))
        
        yaml_content = """
table_body_id:
  primary: "#envTable"
modal_id:
  primary: "#modal"
detail_function_name:
  primary: "detail"
"""
        (tmp_path / "env.yaml").write_text(yaml_content)
        
        registry = SelectorRegistry()  # No explicit path
        registry.load("env")
        
        assert registry.get("env", "table_body_id") == "#envTable"
    
    def test_explicit_path_overrides_env(self, tmp_path, monkeypatch):
        """Explicit path parameter overrides env var."""
        other_path = tmp_path / "other"
        other_path.mkdir()
        
        monkeypatch.setenv("PJUD_SELECTORS_PATH", "/wrong/path")
        
        yaml_content = """
table_body_id:
  primary: "#explicitTable"
modal_id:
  primary: "#modal"
detail_function_name:
  primary: "detail"
"""
        (other_path / "explicit.yaml").write_text(yaml_content)
        
        registry = SelectorRegistry(selectors_path=other_path)
        registry.load("explicit")
        
        assert registry.get("explicit", "table_body_id") == "#explicitTable"


class TestCivilSelectorIntegration:
    """Test CivilScraper integration with selector registry."""
    
    def test_civil_scraper_uses_registry(self):
        """CivilScraper should use selector registry for config."""
        scraper = CivilScraper(headless=True)
        
        # Config should come from registry
        config = scraper.config
        
        # These values should match civil.yaml
        assert config.name == "civil"
        assert config.table_body_id == "verDetalleMisCauCiv"
        assert config.modal_id == "modalDetalleMisCauCivil"
    
    def test_civil_scraper_get_selector(self):
        """CivilScraper.get_selector() retrieves from registry."""
        scraper = CivilScraper(headless=True)
        
        selector = scraper.get_selector("competencia_code")
        assert selector == "3"
    
    def test_civil_scraper_get_selector_with_fallbacks(self):
        """CivilScraper.get_selector_with_fallbacks() returns fallback chain."""
        scraper = CivilScraper(headless=True)
        
        selectors = scraper.get_selector_with_fallbacks("table_body_id")
        
        assert "verDetalleMisCauCiv" in selectors
        assert len(selectors) >= 1  # At least primary
    
    def test_civil_scraper_custom_registry(self, tmp_path):
        """CivilScraper accepts custom registry."""
        yaml_content = """
table_body_id:
  primary: "#customTable"
modal_id:
  primary: "#customModal"
detail_function_name:
  primary: "customDetail"
cases_endpoint:
  primary: "custom/cases.php"
detail_endpoint:
  primary: "custom/detail.php"
search_endpoint:
  primary: "custom/search.php"
competencia_code:
  primary: "99"
"""
        (tmp_path / "civil.yaml").write_text(yaml_content)
        
        custom_registry = SelectorRegistry(selectors_path=tmp_path)
        scraper = CivilScraper(headless=True, selector_registry=custom_registry)
        
        config = scraper.config
        assert config.table_body_id == "#customTable"
        assert config.competencia_code == "99"


class TestCivilYamlFile:
    """Test the bundled civil.yaml file."""
    
    def test_civil_yaml_loads_successfully(self):
        """civil.yaml should load without errors."""
        registry = SelectorRegistry()
        result = registry.load("civil")
        
        assert result is True
    
    def test_civil_yaml_has_required_selectors(self):
        """civil.yaml should have all required selectors."""
        registry = SelectorRegistry()
        registry.load("civil")
        
        required = [
            "table_body_id",
            "modal_id",
            "detail_function_name",
            "cases_endpoint",
            "detail_endpoint",
            "search_endpoint",
            "competencia_code",
        ]
        
        for selector in required:
            assert registry.has_selector("civil", selector), f"Missing: {selector}"
    
    def test_civil_yaml_competencia_code(self):
        """civil.yaml competencia_code should be '3'."""
        registry = SelectorRegistry()
        registry.load("civil")
        
        assert registry.get("civil", "competencia_code") == "3"
    
    def test_civil_yaml_detail_function(self):
        """civil.yaml detail function should match expected pattern."""
        registry = SelectorRegistry()
        registry.load("civil")
        
        fn = registry.get("civil", "detail_function_name")
        assert "Civil" in fn or "civil" in fn.lower()


class TestGlobalRegistryFunctions:
    """Test module-level registry functions."""
    
    def test_get_selector_registry_returns_singleton(self):
        """get_selector_registry() returns same instance."""
        reg1 = get_selector_registry()
        reg2 = get_selector_registry()
        
        assert reg1 is reg2
    
    def test_reload_selectors_function(self):
        """reload_selectors() delegates to registry.reload()."""
        # Pre-load civil
        registry = get_selector_registry()
        if not registry.is_loaded("civil"):
            registry.load("civil")
        
        results = reload_selectors("civil")
        
        assert "civil" in results
        assert results["civil"] is True
