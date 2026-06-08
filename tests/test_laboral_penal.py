"""
Tests for PJUD Laboral and Penal scrapers.

Tests:
1. LaboralScraper and PenalScraper can be instantiated
2. Both implement all abstract methods from PJUDBaseScraper
3. Competency configs are correct
4. Selector YAMLs load correctly
5. API endpoints for laboral/penal are defined
"""

import pytest
from abc import ABC

from app.scrapper.pjud import (
    PJUDBaseScraper,
    CivilScraper,
    LaboralScraper,
    PenalScraper,
    PJUDCase,
    PJUDCaseDetail,
    CompetencyConfig,
    SelectorRegistry,
)


class TestLaboralScraper:
    """Test the LaboralScraper concrete implementation."""
    
    def test_laboral_scraper_can_be_instantiated(self):
        """LaboralScraper should instantiate without errors."""
        scraper = LaboralScraper(headless=True)
        assert scraper is not None
    
    def test_laboral_scraper_inherits_from_base(self):
        """LaboralScraper should inherit from PJUDBaseScraper."""
        assert issubclass(LaboralScraper, PJUDBaseScraper)
    
    def test_laboral_scraper_implements_abstract_methods(self):
        """LaboralScraper should implement all abstract methods."""
        scraper = LaboralScraper(headless=True)
        
        # These should not raise
        config = scraper._get_competency_config()
        assert isinstance(config, CompetencyConfig)
        
        # Parsing methods exist and are callable
        assert callable(scraper._parse_cases_html)
        assert callable(scraper._parse_case_detail_html)
        assert callable(scraper._fetch_cases_page)
    
    def test_laboral_scraper_config_values(self):
        """LaboralScraper should return correct config."""
        scraper = LaboralScraper(headless=True)
        config = scraper.config
        
        assert config.name == "laboral"
        assert config.display_name == "Laboral"
        assert config.competencia_code == "4"
        assert "detalleMisCausaLaboral" in config.detail_function_name
        assert "modalDetalleMisCauLaboral" in config.modal_id
        assert "laboral" in config.cases_endpoint
    
    def test_laboral_scraper_has_public_api_methods(self):
        """LaboralScraper should have the expected public API."""
        scraper = LaboralScraper(headless=True)
        
        # Core methods from base class
        assert hasattr(scraper, 'start')
        assert hasattr(scraper, 'stop')
        assert hasattr(scraper, 'close')
        assert hasattr(scraper, 'login_with_token')
        assert hasattr(scraper, 'get_my_cases')
        assert hasattr(scraper, 'get_case_detail')
        assert hasattr(scraper, 'get_cases_count')
        
        # Laboral-specific methods
        assert hasattr(scraper, 'download_document')
    
    def test_laboral_scraper_uses_selector_registry(self):
        """LaboralScraper should use SelectorRegistry."""
        scraper = LaboralScraper(headless=True)
        
        assert hasattr(scraper, 'selectors')
        assert isinstance(scraper.selectors, SelectorRegistry)
        
        # Should be able to get laboral selectors
        table_id = scraper.get_selector("table_body_id")
        assert "Lab" in table_id


class TestPenalScraper:
    """Test the PenalScraper concrete implementation."""
    
    def test_penal_scraper_can_be_instantiated(self):
        """PenalScraper should instantiate without errors."""
        scraper = PenalScraper(headless=True)
        assert scraper is not None
    
    def test_penal_scraper_inherits_from_base(self):
        """PenalScraper should inherit from PJUDBaseScraper."""
        assert issubclass(PenalScraper, PJUDBaseScraper)
    
    def test_penal_scraper_implements_abstract_methods(self):
        """PenalScraper should implement all abstract methods."""
        scraper = PenalScraper(headless=True)
        
        # These should not raise
        config = scraper._get_competency_config()
        assert isinstance(config, CompetencyConfig)
        
        # Parsing methods exist and are callable
        assert callable(scraper._parse_cases_html)
        assert callable(scraper._parse_case_detail_html)
        assert callable(scraper._fetch_cases_page)
    
    def test_penal_scraper_config_values(self):
        """PenalScraper should return correct config."""
        scraper = PenalScraper(headless=True)
        config = scraper.config
        
        assert config.name == "penal"
        assert config.display_name == "Penal"
        assert config.competencia_code == "5"
        assert "detalleMisCausaPenal" in config.detail_function_name
        assert "modalDetalleMisCauPenal" in config.modal_id
        assert "penal" in config.cases_endpoint
    
    def test_penal_scraper_has_public_api_methods(self):
        """PenalScraper should have the expected public API."""
        scraper = PenalScraper(headless=True)
        
        # Core methods from base class
        assert hasattr(scraper, 'start')
        assert hasattr(scraper, 'stop')
        assert hasattr(scraper, 'close')
        assert hasattr(scraper, 'login_with_token')
        assert hasattr(scraper, 'get_my_cases')
        assert hasattr(scraper, 'get_case_detail')
        assert hasattr(scraper, 'get_cases_count')
        
        # Penal-specific methods
        assert hasattr(scraper, 'download_document')
    
    def test_penal_scraper_uses_selector_registry(self):
        """PenalScraper should use SelectorRegistry."""
        scraper = PenalScraper(headless=True)
        
        assert hasattr(scraper, 'selectors')
        assert isinstance(scraper.selectors, SelectorRegistry)
        
        # Should be able to get penal selectors
        table_id = scraper.get_selector("table_body_id")
        assert "Pen" in table_id


class TestSelectorYAMLFiles:
    """Test that selector YAML files load correctly."""
    
    def test_laboral_yaml_loads(self):
        """laboral.yaml should load successfully."""
        registry = SelectorRegistry()
        success = registry.load("laboral")
        
        assert success is True
        assert registry.is_loaded("laboral")
    
    def test_penal_yaml_loads(self):
        """penal.yaml should load successfully."""
        registry = SelectorRegistry()
        success = registry.load("penal")
        
        assert success is True
        assert registry.is_loaded("penal")
    
    def test_laboral_yaml_has_required_selectors(self):
        """laboral.yaml should have all required selectors."""
        registry = SelectorRegistry()
        registry.load("laboral")
        
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
            value = registry.get("laboral", selector)
            assert value is not None and value != "", f"Missing or empty: {selector}"
    
    def test_penal_yaml_has_required_selectors(self):
        """penal.yaml should have all required selectors."""
        registry = SelectorRegistry()
        registry.load("penal")
        
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
            value = registry.get("penal", selector)
            assert value is not None and value != "", f"Missing or empty: {selector}"
    
    def test_laboral_competency_code_is_4(self):
        """Laboral competency code should be '4'."""
        registry = SelectorRegistry()
        registry.load("laboral")
        
        code = registry.get("laboral", "competencia_code")
        assert code == "4"
    
    def test_penal_competency_code_is_5(self):
        """Penal competency code should be '5'."""
        registry = SelectorRegistry()
        registry.load("penal")
        
        code = registry.get("penal", "competencia_code")
        assert code == "5"


class TestMultiCompetencyScrapers:
    """Test that multiple scrapers can coexist."""
    
    def test_all_scrapers_have_different_configs(self):
        """Each scraper should have a distinct config."""
        civil = CivilScraper(headless=True)
        laboral = LaboralScraper(headless=True)
        penal = PenalScraper(headless=True)
        
        # Names should be different
        assert civil.config.name == "civil"
        assert laboral.config.name == "laboral"
        assert penal.config.name == "penal"
        
        # Competency codes should be different
        codes = {
            civil.config.competencia_code,
            laboral.config.competencia_code,
            penal.config.competencia_code,
        }
        assert len(codes) == 3, "All competency codes should be unique"
    
    def test_all_scrapers_have_different_endpoints(self):
        """Each scraper should have distinct endpoints."""
        civil = CivilScraper(headless=True)
        laboral = LaboralScraper(headless=True)
        penal = PenalScraper(headless=True)
        
        endpoints = {
            civil.config.cases_endpoint,
            laboral.config.cases_endpoint,
            penal.config.cases_endpoint,
        }
        assert len(endpoints) == 3, "All cases_endpoint should be unique"
    
    def test_scrapers_share_base_class_methods(self):
        """All scrapers should have the same base class methods."""
        civil = CivilScraper(headless=True)
        laboral = LaboralScraper(headless=True)
        penal = PenalScraper(headless=True)
        
        base_methods = ['start', 'stop', 'close', 'login_with_token', 
                       'get_my_cases', 'get_case_detail', 'get_cases_count']
        
        for method in base_methods:
            assert hasattr(civil, method)
            assert hasattr(laboral, method)
            assert hasattr(penal, method)


class TestPackageExports:
    """Test that new scrapers are exported from the package."""
    
    def test_laboral_scraper_exported(self):
        """LaboralScraper should be exported from app.scrapper.pjud."""
        from app.scrapper.pjud import LaboralScraper
        assert LaboralScraper is not None
    
    def test_penal_scraper_exported(self):
        """PenalScraper should be exported from app.scrapper.pjud."""
        from app.scrapper.pjud import PenalScraper
        assert PenalScraper is not None
    
    def test_all_scrapers_in_all_list(self):
        """All scrapers should be in __all__."""
        import app.scrapper.pjud as pjud
        
        assert "CivilScraper" in pjud.__all__
        assert "LaboralScraper" in pjud.__all__
        assert "PenalScraper" in pjud.__all__


class TestLaboralCasesParsing:
    """Test laboral-specific parsing logic."""
    
    def test_laboral_case_with_7_columns(self):
        """Laboral cases should handle 7 columns (no Institucion)."""
        scraper = LaboralScraper(headless=True)
        
        # Simulate HTML with 7 columns (typical laboral format)
        html = """
        <tr>
            <td><a onclick="detalleMisCausaLaboral('token123')">Ver</a></td>
            <td>T-123-2026</td>
            <td>1er Juzgado del Trabajo</td>
            <td>EMPRESA/TRABAJADOR</td>
            <td>01/06/2026</td>
            <td>Tramitacion</td>
            <td>Principal</td>
        </tr>
        """
        
        cases = scraper._parse_cases_html(html)
        
        assert len(cases) == 1
        assert cases[0].rol == "T-123-2026"
        assert cases[0].tribunal == "1er Juzgado del Trabajo"
        assert cases[0].caratulado == "EMPRESA/TRABAJADOR"
        assert cases[0].institucion is None  # Laboral doesn't have this


class TestPenalCasesParsing:
    """Test penal-specific parsing logic."""
    
    def test_penal_case_with_8_columns(self):
        """Penal cases should handle 8 columns (with Institucion)."""
        scraper = PenalScraper(headless=True)
        
        # Simulate HTML with 8 columns (typical penal format)
        html = """
        <tr>
            <td><a onclick="detalleMisCausaPenal('token456')">Ver</a></td>
            <td>O-456-2026</td>
            <td>8vo Juzgado de Garantia</td>
            <td>MINISTERIO PUBLICO/IMPUTADO</td>
            <td>15/05/2026</td>
            <td>En Tramite</td>
            <td>Principal</td>
            <td>Fiscalia Centro Norte</td>
        </tr>
        """
        
        cases = scraper._parse_cases_html(html)
        
        assert len(cases) == 1
        assert cases[0].rol == "O-456-2026"
        assert cases[0].tribunal == "8vo Juzgado de Garantia"
        assert cases[0].institucion == "Fiscalia Centro Norte"
