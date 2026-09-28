"""poda de causas — marcar causas sin cobertura comercial para dejar de scrapearlas

Muchas causas del portafolio pertenecen a clientes que ya no tienen contrato
vigente con el estudio (cobertura ``caducado`` en Sysgal para todas sus
partes). Seguir scrapeándolas quema presupuesto de scraping — horas de
browser, riesgo de bloqueo/challenge de PJUD — sobre causas que ya no le
generan valor al estudio. La poda NO borra la causa: la marca (``poda_at``)
para que la rotación de detalle (``_select_cases_for_detail_rotation``) deje
de visitarla, sin perder el historial ya scrapeado.

Se agregan tres columnas nullable a ``cases``: ``poda_at`` (cuándo se podó,
NULL = no podada), ``poda_motivo`` (por qué) y ``poda_por_rut`` (quién lo
ejecutó, para auditoría — espejo de ``assigned_by_rut`` en la asignación por
nivel).

Revision ID: 062
Revises: 061
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "062"
down_revision: Union[str, None] = "061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Sin índice deliberadamente: la consulta de rotación
    # (_select_cases_for_detail_rotation) ya filtra por lawyer_id +
    # competencia, que es lo selectivo — poda_at solo se agrega como filtro
    # adicional (poda_at IS NULL) sobre ese resultado ya acotado, así que no
    # aporta selectividad propia que justifique el costo de mantenimiento de
    # un índice nuevo sobre una tabla grande.
    op.add_column("cases", sa.Column("poda_at", sa.DateTime(), nullable=True))
    op.add_column("cases", sa.Column("poda_motivo", sa.String(length=255), nullable=True))
    op.add_column("cases", sa.Column("poda_por_rut", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "poda_por_rut")
    op.drop_column("cases", "poda_motivo")
    op.drop_column("cases", "poda_at")
