"""Deterministic, offline tests for ``_BUILD_LOGIN_POST_JS`` — the parser that
reproduces PJUD's daily-rotating login POST field mapping.

PJUD rotates the login field names every day, so this logic has no stable
production fixture to assert against. These tests run the REAL parser JS against
static fixture HTML (no network, no live PJUD), so a regression in the parser —
or a PJUD rotation shape it can't handle — is caught deterministically instead of
only surfacing as a silent empty-credential POST in production.
"""
import pytest
from playwright.async_api import async_playwright

from app.scrapper.pjud.base import _BUILD_LOGIN_POST_JS

# Non-secret placeholders; the JWT keeps the 'eyJ...'.<p>.<s> shape.
ARGS = {"rut": "16021492", "clave": "s3cr3t-p4ss", "token": "TOK123", "jwt": "eyJ.aaa.bbb"}

# Mirrors the real handler's postData block (obfuscated rotative keys).
HAPPY_POSTDATA = """
        postData['k_jwt_rot'] = acceso_paso;
        postData['g-recaptcha-response-seg-clave_hn'] = g_recaptcha_clave_paso;
        postData['f_6b5373856e8c'] = rut;
        postData['f_c46982d87605'] = clave;
        postData['f_7e12b55ac448'] = 'CONST_ABC_DAY';
        postData['f_7589242b39d7'] = $('#f_7589242b39d7').val();
"""


def _fixture(postdata_lines: str, *, include_block: bool = True) -> str:
    block = ""
    if include_block:
        block = (
            "var postData = {};\n"
            + postdata_lines
            + '\n$("#formSSGGNN").load("../sessionN.php", postData);'
        )
    return f"""<!doctype html><html><body>
      <form name="frm">
        <input type="hidden" name="ACCESO" value="eyJ.aaa.bbb">
        <input type="hidden" name="action" value="ACT">
      </form>
      <input type="hidden" id="f_7589242b39d7" value="">
      <div id="formSSGGNN"></div>
      <script>{block}</script>
    </body></html>"""


async def _run(html: str, args: dict = ARGS) -> dict:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # chromium binary not installed in this env
            pytest.skip(f"playwright chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            # Never navigate on submit — keep the test hermetic and offline.
            await page.add_init_script("HTMLFormElement.prototype.submit = function(){};")
            await page.route(
                "**/*",
                lambda route: route.fulfill(content_type="text/html", body=html),
            )
            await page.goto("https://oficinajudicialvirtual.pjud.cl/home/index.php")
            return await page.evaluate(_BUILD_LOGIN_POST_JS, args)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_happy_path_maps_every_field_under_rotative_keys():
    r = await _run(_fixture(HAPPY_POSTDATA))
    assert r["ok"] is True
    assert set(r["fields"]) == {
        "k_jwt_rot",
        "g-recaptcha-response-seg-clave_hn",
        "f_6b5373856e8c",
        "f_c46982d87605",
        "f_7e12b55ac448",
        "f_7589242b39d7",
    }
    # Relative endpoint resolves against the login page URL.
    assert r["endpoint"] == "https://oficinajudicialvirtual.pjud.cl/sessionN.php"
    # Each key is attributed to the right source category.
    assert r["roleMap"]["f_6b5373856e8c"] == "rut"
    assert r["roleMap"]["f_c46982d87605"] == "clave"
    assert r["roleMap"]["k_jwt_rot"] == "jwt"
    assert r["roleMap"]["g-recaptcha-response-seg-clave_hn"] == "recaptcha"
    assert r["roleMap"]["f_7e12b55ac448"] == "literal"
    assert r["roleMap"]["f_7589242b39d7"] == "dom"  # honeypot, read empty from DOM


@pytest.mark.asyncio
async def test_renamed_credential_source_fails_loud_not_silent_empty():
    # PJUD renames the password source variable: `clave` -> `pass_paso`.
    renamed = HAPPY_POSTDATA.replace("= clave;", "= pass_paso;")
    r = await _run(_fixture(renamed))
    assert r["ok"] is False, "must refuse to POST an empty password, not submit it"
    assert "clave" in r["reason"]
    assert "pass_paso" in r["reason"]  # the unrecognized source is surfaced for diagnosis


@pytest.mark.asyncio
async def test_missing_postdata_block_fails_loud():
    r = await _run(_fixture("", include_block=False))
    assert r["ok"] is False
    assert "postData block not found" in r["reason"]
