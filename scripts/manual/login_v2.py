"""
Script para probar login con Segunda Clave - v2
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
        
        print("\n2. Abriendo modal de Segunda Clave directamente...")
        # En vez de hacer clic, disparamos el modal directamente con JS
        await page.evaluate("""
            $('#segunda-clave-access').modal('show');
        """)
        await asyncio.sleep(1)
        
        # Verificar que el modal está visible
        modal = await page.query_selector("#segunda-clave-access")
        if modal:
            is_visible = await modal.is_visible()
            print(f"   Modal visible: {is_visible}")
        
        await page.screenshot(path="screenshots/modal_opened.png")
        print("   Screenshot: modal_opened.png")
        
        print("\n3. Llenando formulario...")
        # RUT sin dígito verificador
        rut_input = await page.query_selector("#segunda-clave-access #rut, #fSGN #rut")
        if rut_input:
            await rut_input.fill("16021492")
            print("   RUT ingresado")
        
        pwd_input = await page.query_selector("#segunda-clave-access #password, #fSGN #password")
        if pwd_input:
            await pwd_input.fill("Gruposegal2026+")
            print("   Password ingresado")
        
        await page.screenshot(path="screenshots/form_filled.png")
        print("   Screenshot: form_filled.png")
        
        print("\n4. Estado del reCAPTCHA...")
        recaptcha = await page.query_selector("[name='g-recaptcha-response-seg-clave_hn']")
        if recaptcha:
            value = await recaptcha.get_attribute("value")
            print(f"   Token: {'HAY TOKEN' if value else 'VACÍO - reCAPTCHA v3 necesita sitekey'}")
        
        # Ver el sitekey
        print("\n5. Buscando sitekey de reCAPTCHA...")
        scripts = await page.query_selector_all("script[src*='recaptcha']")
        for script in scripts:
            src = await script.get_attribute("src")
            print(f"   Script: {src}")
        
        # Buscar en el HTML
        html = await page.content()
        if "6LelLWkUAAAAA" in html:
            print("   ✓ Sitekey encontrado: 6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt")
        
        print("\n6. Guardando HTML del formulario...")
        form_html = await page.evaluate("""
            document.querySelector('#fSGN')?.outerHTML || 'NO ENCONTRADO'
        """)
        with open("screenshots/form_fSGN.html", "w") as f:
            f.write(form_html)
        print(f"   Guardado: form_fSGN.html ({len(form_html)} chars)")
        
        await browser.close()
        print("\n✅ Completado")


if __name__ == "__main__":
    import os
    os.makedirs("screenshots", exist_ok=True)
    asyncio.run(test_login())
