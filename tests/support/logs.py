"""Ayudas para afirmar sobre logs sin depender del estado global.

``caplog`` captura a través del logger raíz, así que su resultado depende de la
configuración de logging que hayan dejado los tests anteriores: los mismos
tests pasan solos y fallan en el suite completo. Afirmar sobre el logger del
módulo —parcheado— deja la verificación aislada.

El patrón ya existía en ``tests/scrapper/pjud/test_detail_modal_and_login_diagnostics``;
esto lo comparte en vez de repetirlo en cada archivo.
"""

from unittest.mock import MagicMock


def _formatear(call) -> str:
    """``logger.warning("hay %s", n)`` -> ``"hay 3"``."""
    fmt, *args = call.args
    return fmt % tuple(args) if args else str(fmt)


def warning_messages(mock_logger: MagicMock) -> list[str]:
    """Mensajes ya formateados de cada ``logger.warning`` sobre el logger parcheado."""
    return [_formatear(c) for c in mock_logger.warning.call_args_list]


def warnings_text(mock_logger: MagicMock) -> str:
    """Todos los warnings en un solo texto, para buscar un fragmento."""
    return " ".join(warning_messages(mock_logger))
