"""
Script simple para probar login con Segunda Clave
"""
import asyncio
from playwright.async_api import async_playwright


async def test_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print("1. Navegando al PJUD...")
        await page.goto("https://oficinajudicialvirtual.pjud.cl/home/index.php", timeout=30000)
        print(f"   URL: {page.url}")
        
        print("\n2. Abriendo modal de Segunda Clave...")
        # Buscar y hacer clic en "Clave Poder Judicial"
        await page.click("a:has-text('Clave Poder Judicial')", timeout=5000)
        await asyncio.sleep(1)
        
        # Verificar que el modal está visible
        modal = await page.query_selector("#segunda-clave-access")
        if modal:
            is_visible = await modal.is_visible()
            print(f"   Modal visible: {is_visible}")
        
        print("\n3. Llenando formulario...")
        # RUT sin dígito verificador
        await page.fill("#rut", "16021492")
        await page.fill("#password", "Gruposegal2026+")
        print("   Datos ingresados")
        
        # Screenshot antes de submit
        await page.screenshot(path="screenshots/login_form_filled.png")
        print("   Screenshot: login_form_filled.png")
        
        print("\n4. Verificando reCAPTCHA...")
        recaptcha = await page.query_selector("#g-recaptcha-response-seg-clave_hn")
        if recaptcha:
            value = await recaptcha.get_attribute("value")
            print(f"   Token reCAPTCHA: {value[:50] if value else 'VACÍO - necesita resolver'}")
        
        print("\n5. Información del formulario:")
        form = await page.query_selector("#fSGN")
        if form:
            html = await form.inner_html()
            print(f"   Formulario capturado ({len(html)} chars)")
            with open("screenshots/login_form.html", "w") as f:
                f.write(html)
        
        await browser.close()
        print("\n✅ Completado")


if __name__ == "__main__":
    import os
    os.makedirs("screenshots", exist_ok=True)
    asyncio.run(test_login())
