"""`/health` informa qué commit está corriendo.

Existe por un incidente concreto: el 28-09-2026 tres deploys corrieron fuera
de orden y el último subió un commit anterior encima del nuevo. Los tres se
reportaron en verde, porque el chequeo solo preguntaba si la app respondía —
y una app con codigo de la semana pasada responde igual de bien.

El deploy compara este campo contra el commit que quiso desplegar.
"""

import pytest

from app.config import settings


@pytest.fixture
def commit(monkeypatch):
    def _set(valor: str):
        monkeypatch.setattr(settings, "APP_COMMIT", valor)
    return _set


def test_informa_el_commit_inyectado_por_el_deploy(client, commit):
    commit("c3a11f5b0d9e4f6a8b2c1d3e5f7a9b0c2d4e6f80")

    body = client.get("/health").json()

    assert body["commit"] == "c3a11f5b0d9e4f6a8b2c1d3e5f7a9b0c2d4e6f80"
    assert body["status"] == "healthy"


def test_sin_inyectar_lo_dice_en_vez_de_mentir(client, commit):
    """Correr fuera del deploy es legítimo; afirmar un commit que no se sabe,
    no. Un string vacío se leería como 'coincide con nada'."""
    commit("")

    assert client.get("/health").json()["commit"] == "desconocido"


def test_sigue_sin_tocar_la_base(client, commit):
    """`/health` es sonda de vida: /readyz es la que verifica la base."""
    commit("abc123")

    assert client.get("/health").status_code == 200
