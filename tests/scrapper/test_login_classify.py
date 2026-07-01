"""Tests for classify_login_failure — distinguishing wrong credentials (the
lawyer must update their clave) from a rejected token / transient failure.

This drives whether the segunda_clave_rotator tells the firm "ask this lawyer to
change their password" vs. "retry — it's the token/network, not the clave".
"""

from app.scrapper.pjud.base import classify_login_failure, detect_shape_challenge


class TestInvalidCredentialsDetected:
    """Wrong-password / wrong-user messages → is_invalid_credentials = True."""

    def test_clave_incorrecta(self):
        invalid, msg = classify_login_failure("<div class='alert'>Clave incorrecta</div>")
        assert invalid is True
        assert "clave" in msg.lower()

    def test_usuario_o_clave_invalidos(self):
        invalid, _ = classify_login_failure("<p>Usuario y/o clave inválidos</p>")
        assert invalid is True

    def test_contrasena_incorrecta(self):
        invalid, _ = classify_login_failure("La contraseña ingresada es incorrecta")
        assert invalid is True

    def test_clave_bloqueada(self):
        invalid, _ = classify_login_failure("Su clave se encuentra bloqueada")
        assert invalid is True

    def test_rut_no_valido(self):
        invalid, _ = classify_login_failure("RUT o clave no válidos")
        assert invalid is True


class TestNotCredentialFailure:
    """Token rejection / generic pages / empty → is_invalid_credentials = False."""

    def test_empty_content(self):
        assert classify_login_failure("") == (False, "")

    def test_none_safe(self):
        assert classify_login_failure(None) == (False, "")

    def test_token_rejection_message_is_not_credentials(self):
        # A captcha/token error must NOT be flagged as bad credentials (retry-able).
        invalid, _ = classify_login_failure("<div>Error de validación. Intente nuevamente.</div>")
        assert invalid is False

    def test_normal_login_page_no_error(self):
        invalid, _ = classify_login_failure("<html><body>Ingrese su RUT y clave para continuar</body></html>")
        assert invalid is False


class TestShapeChallengeDetected:
    """PJUD/F5 Shape challenge pages must be treated as a block, not bad credentials."""

    def test_tspd_image_challenge(self):
        detected, marker = detect_shape_challenge(
            """
            <script>window["bobcmn"] = ".../TSPD/..."; window["failureConfig"] = "...";</script>
            <b>What code is in the image?</b>
            Your support ID is: 11224827228940942058.
            """,
            "https://oficinajudicialvirtual.pjud.cl/indexN.php",
        )

        assert detected is True
        assert marker

    def test_normal_login_page_is_not_shape(self):
        detected, marker = detect_shape_challenge(
            "<html><body>Ingrese su RUT y clave para continuar</body></html>",
            "https://oficinajudicialvirtual.pjud.cl/home/index.php",
        )

        assert detected is False
        assert marker == ""

    def test_tspd_cookie_script_without_challenge_is_not_shape(self):
        detected, marker = detect_shape_challenge(
            """
            <html><body>
            <script>document.cookie = "TSPD_101=abc";</script>
            <main>Normal PJUD content without a challenge form.</main>
            </body></html>
            """,
            "https://oficinajudicialvirtual.pjud.cl/indexN.php",
        )

        assert detected is False
        assert marker == ""
