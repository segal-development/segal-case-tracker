"""
Tests for PJUD base scraper and Civil refactor.

Tests:
1. PJUDBaseScraper rejects direct instantiation (abstract)
2. CivilScraper can be instantiated
3. CivilScraper implements all abstract methods
4. Backward compatibility: old imports still work
5. Data classes work correctly
"""

import pytest
from abc import ABC
from typing import List

from app.scrapper.pjud import (
    PJUDBaseScraper,
    CivilScraper,
    PJUDCase,
    PJUDDocument,
    PJUDMovement,
    PJUDCaseDetail,
    CompetencyConfig,
    LoginError,
    PJUDError,
    ScrapingError,
)
from app.scrapper.pjud.base import (
    PJUDLitigante,
    PJUDNotificacion,
    PJUDEscrito,
    PJUDExhorto,
)


class TestPJUDBaseScraper:
    """Test the abstract base class behavior."""
    
    def test_base_scraper_is_abstract(self):
        """PJUDBaseScraper should be an abstract class."""
        assert issubclass(PJUDBaseScraper, ABC)
    
    def test_base_scraper_cannot_be_instantiated(self):
        """Direct instantiation of PJUDBaseScraper should fail."""
        with pytest.raises(TypeError) as exc_info:
            PJUDBaseScraper()
        
        # Should mention abstract methods
        error_msg = str(exc_info.value)
        assert "abstract" in error_msg.lower() or "instantiate" in error_msg.lower()
    
    def test_base_scraper_has_abstract_methods(self):
        """PJUDBaseScraper should have required abstract methods."""
        # Check __abstractmethods__ set
        abstract_methods = getattr(PJUDBaseScraper, '__abstractmethods__', set())
        
        expected_methods = {
            '_get_competency_config',
            '_parse_cases_html',
            '_parse_case_detail_html',
            '_fetch_cases_page',
        }
        
        assert expected_methods.issubset(abstract_methods), \
            f"Missing abstract methods: {expected_methods - abstract_methods}"


class TestCivilScraper:
    """Test the CivilScraper concrete implementation."""
    
    def test_civil_scraper_can_be_instantiated(self):
        """CivilScraper should instantiate without errors."""
        scraper = CivilScraper(headless=True)
        assert scraper is not None
    
    def test_civil_scraper_inherits_from_base(self):
        """CivilScraper should inherit from PJUDBaseScraper."""
        assert issubclass(CivilScraper, PJUDBaseScraper)
    
    def test_civil_scraper_implements_abstract_methods(self):
        """CivilScraper should implement all abstract methods."""
        scraper = CivilScraper(headless=True)
        
        # These should not raise
        config = scraper._get_competency_config()
        assert isinstance(config, CompetencyConfig)
        
        # Parsing methods exist and are callable
        assert callable(scraper._parse_cases_html)
        assert callable(scraper._parse_case_detail_html)
        assert callable(scraper._fetch_cases_page)
    
    def test_civil_scraper_config_values(self):
        """CivilScraper should return correct config."""
        scraper = CivilScraper(headless=True)
        config = scraper.config
        
        assert config.name == "civil"
        assert config.display_name == "Civil"
        assert config.competencia_code == "3"
        assert "detalleMisCausaCivil" in config.detail_function_name
        assert "modalDetalleMisCauCivil" in config.modal_id
    
    def test_civil_scraper_has_public_api_methods(self):
        """CivilScraper should have the expected public API."""
        scraper = CivilScraper(headless=True)
        
        # Core methods from base class
        assert hasattr(scraper, 'start')
        assert hasattr(scraper, 'stop')
        assert hasattr(scraper, 'close')  # Alias
        assert hasattr(scraper, 'login_with_token')
        assert hasattr(scraper, 'get_my_cases')
        assert hasattr(scraper, 'get_case_detail')
        assert hasattr(scraper, 'get_cases_count')
        assert hasattr(scraper, 'get_my_cases_recent')
        
        # Civil-specific methods
        assert hasattr(scraper, 'download_document')
        assert hasattr(scraper, 'download_movement_documents')
        assert hasattr(scraper, 'search_cases')


class TestBackwardCompatibility:
    """Test that old imports still work."""
    
    def test_import_from_old_location(self):
        """Imports from app.scrapper.pjud_civil should work."""
        from app.scrapper.pjud_civil import (
            PJUDCivilScraper,
            PJUDCase,
            PJUDDocument,
            PJUDMovement,
            PJUDCaseDetail,
            LoginError,
        )
        
        # Should be the same classes
        assert PJUDCivilScraper is CivilScraper
    
    def test_old_scraper_alias_works(self):
        """PJUDCivilScraper should work as an alias."""
        from app.scrapper.pjud_civil import PJUDCivilScraper
        
        scraper = PJUDCivilScraper(headless=True)
        assert isinstance(scraper, CivilScraper)
        assert isinstance(scraper, PJUDBaseScraper)
    
    def test_constants_available(self):
        """Old constants should still be accessible."""
        from app.scrapper.pjud_civil import (
            PJUD_BASE_URL,
            PJUD_HOME_URL,
            COMPETENCIA_CIVIL,
        )
        
        assert "pjud.cl" in PJUD_BASE_URL
        assert COMPETENCIA_CIVIL == "3"


class TestDataClasses:
    """Test data classes work correctly."""
    
    def test_pjud_case_creation(self):
        """PJUDCase should be created with required fields."""
        case = PJUDCase(
            rol="C-1234-2026",
            tribunal="1er Juzgado Civil de Santiago",
            caratulado="BANCO/DEUDOR",
            fecha_ingreso="01/06/2026",
        )
        
        assert case.rol == "C-1234-2026"
        assert case.tipo == "C"
        assert case.numero == "1234"
        assert case.anio == "2026"
    
    def test_pjud_case_properties(self):
        """PJUDCase properties should extract ROL parts correctly."""
        case = PJUDCase(
            rol="V-5678-2025",
            tribunal="Test",
            caratulado="Test",
            fecha_ingreso="01/01/2025",
        )
        
        assert case.tipo == "V"
        assert case.numero == "5678"
        assert case.anio == "2025"
    
    def test_pjud_document_creation(self):
        """PJUDDocument should be created correctly."""
        doc = PJUDDocument(
            token="abc123",
            tipo="principal",
            url_type="docuS",
        )
        
        assert doc.token == "abc123"
        assert doc.tipo == "principal"
        assert doc.url_type == "docuS"
    
    def test_pjud_movement_creation(self):
        """PJUDMovement should be created correctly."""
        movement = PJUDMovement(
            folio="1",
            fecha="01/06/2026",
            tipo_tramite="Resolucion",
            descripcion="Test movement",
        )
        
        assert movement.folio == "1"
        assert movement.documentos == []
        assert movement.tiene_documento is False
    
    def test_pjud_case_detail_creation(self):
        """PJUDCaseDetail should be created correctly."""
        case = PJUDCase(
            rol="C-1234-2026",
            tribunal="Test",
            caratulado="Test",
            fecha_ingreso="01/06/2026",
        )
        
        detail = PJUDCaseDetail(case=case)
        
        assert detail.case.rol == "C-1234-2026"
        assert detail.movements == []
        assert detail.cuadernos == []


class TestExceptions:
    """Test exception hierarchy."""
    
    def test_login_error_is_pjud_error(self):
        """LoginError should inherit from PJUDError."""
        assert issubclass(LoginError, PJUDError)
    
    def test_scraping_error_is_pjud_error(self):
        """ScrapingError should inherit from PJUDError."""
        assert issubclass(ScrapingError, PJUDError)
    
    def test_pjud_error_stores_cause(self):
        """PJUDError should store the cause exception."""
        original = ValueError("original error")
        error = PJUDError("wrapped error", cause=original)
        
        assert error.message == "wrapped error"
        assert error.cause is original


class TestCompetencyConfig:
    """Test CompetencyConfig dataclass."""
    
    def test_config_has_required_fields(self):
        """CompetencyConfig should have all required fields."""
        config = CompetencyConfig(
            name="test",
            display_name="Test",
            competencia_code="99",
            table_body_id="testTable",
            modal_id="testModal",
            detail_function_name="testDetail",
            cases_endpoint="test/cases.php",
            detail_endpoint="test/detail.php",
            search_endpoint="test/search.php",
        )
        
        assert config.name == "test"
        assert config.competencia_code == "99"


class TestNewEntityDataclasses:
    """Test the four new entity dataclasses added for case detail parsing (S1-T02)."""

    def test_pjud_litigante_exists_and_fields(self):
        """PJUDLitigante should have participante, rut, persona_type, nombre."""
        lit = PJUDLitigante(
            participante="DTE.",
            rut="76543210-K",
            persona_type="JURIDICA",
            nombre="CAJA FICTICIA DE PREVISION SOCIAL SA",
        )
        assert lit.participante == "DTE."
        assert lit.rut == "76543210-K"
        assert lit.persona_type == "JURIDICA"
        assert lit.nombre == "CAJA FICTICIA DE PREVISION SOCIAL SA"

    def test_pjud_notificacion_exists_and_fields(self):
        """PJUDNotificacion should have all 8 fields."""
        notif = PJUDNotificacion(
            rol="C-9999-2099",
            estado_notif="Notificada",
            tipo_notif="Personal",
            fecha_tramite="15/03/2026",
            tipo_participante="DTE.",
            nombre="CAJA FICTICIA DE PREVISION SOCIAL SA",
            tramite="Auto acordado",
            obs_fallida="",
        )
        assert notif.rol == "C-9999-2099"
        assert notif.obs_fallida == ""

    def test_pjud_escrito_exists_and_boolean_fields(self):
        """PJUDEscrito should have boolean tiene_documento and tiene_anexo."""
        escrito = PJUDEscrito(
            fecha_ingreso="10/01/2026",
            tipo_escrito="Demanda",
            solicitante="CAJA FICTICIA DE PREVISION SOCIAL SA",
            tiene_documento=True,
            tiene_anexo=False,
            doc_token="fake-token",
        )
        assert isinstance(escrito.tiene_documento, bool)
        assert isinstance(escrito.tiene_anexo, bool)
        assert escrito.doc_token == "fake-token"

    def test_pjud_exhorto_exists_and_optional_token(self):
        """PJUDExhorto should have optional detalle_token defaulting to None."""
        exhorto = PJUDExhorto(
            rol_origen="C-9999-2099",
            tipo_exhorto="Exhorto",
            rol_destino="E-888-2099",
            fecha_ordena="05/05/2026",
            fecha_ingreso="05/05/2026",
            tribunal_destino="8º Juzgado Civil de Santiago",
            estado="Generado",
        )
        assert exhorto.rol_destino == "E-888-2099"
        assert exhorto.detalle_token is None

    def test_pjud_case_detail_has_four_new_list_fields(self):
        """PJUDCaseDetail should have litigantes, notificaciones, escritos, exhortos lists."""
        case = PJUDCase(
            rol="C-9999-2099",
            tribunal="Test",
            caratulado="Test",
            fecha_ingreso="01/06/2026",
        )
        detail = PJUDCaseDetail(case=case)

        # All four new fields must exist and default to empty lists
        assert hasattr(detail, "litigantes")
        assert hasattr(detail, "notificaciones")
        assert hasattr(detail, "escritos")
        assert hasattr(detail, "exhortos")
        assert detail.litigantes == []
        assert detail.notificaciones == []
        assert detail.escritos == []
        assert detail.exhortos == []

        # partes must still exist (backward compat)
        assert hasattr(detail, "partes")
