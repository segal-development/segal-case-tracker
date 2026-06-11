"""
Script de prueba para capturar la estructura del PJUD
"""
import asyncio
from playwright.async_api import async_playwright


async def test_pjud_access():
    """Prueba acceso al PJUD y captura estructura HTML"""
    
    async with async_playwright() as p:
        # Lanzar browser visible para debug
        browser = await p.chromium.launch(headless=False, slow_mo=1000)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()
        
        print("=" * 60)
        print("PASO 1: Navegando al portal PJUD...")
        print("=" * 60)
        
        await page.goto("https://oficinajudicialvirtual.pjud.cl")
        await page.wait_for_load_state("networkidle")
        
        # Guardar screenshot inicial
        await page.screenshot(path="screenshots/01_home.png")
        print("Screenshot guardado: 01_home.png")
        
        # Buscar opciones de login
        print("\nBuscando opciones de login...")
        
        # Buscar botón de Segunda Clave / Clave Poder Judicial
        login_buttons = await page.query_selector_all("button, a")
        for btn in login_buttons:
            text = await btn.inner_text()
            if "clave" in text.lower() or "login" in text.lower() or "ingresar" in text.lower():
                print(f"  Encontrado: {text.strip()}")
        
        print("\n" + "=" * 60)
        print("PASO 2: Buscando formulario de Segunda Clave...")
        print("=" * 60)
        
        # Intentar encontrar el formulario de login
        # Buscar por diferentes selectores comunes
        selectors_to_try = [
            "#segunda-clave-access",
            ".login-form",
            "#loginForm",
            "form[name='frmseg']",
            "#fSGN",
            "input#rut",
            "input[name='rut']",
        ]
        
        for selector in selectors_to_try:
            element = await page.query_selector(selector)
            if element:
                print(f"  ✓ Encontrado: {selector}")
            else:
                print(f"  ✗ No encontrado: {selector}")
        
        # Esperar un poco para que cargue JS
        await asyncio.sleep(3)
        
        # Guardar HTML de la página
        html_content = await page.content()
        with open("screenshots/01_home.html", "w") as f:
            f.write(html_content)
        print("\nHTML guardado: 01_home.html")
        
        print("\n" + "=" * 60)
        print("Página cargada. Revisa los screenshots y el HTML.")
        print("Presiona Enter para continuar explorando o Ctrl+C para salir...")
        print("=" * 60)
        
        # Mantener el browser abierto para inspección manual
        input()
        
        await browser.close()


if __name__ == "__main__":
    import os
    os.makedirs("screenshots", exist_ok=True)
    asyncio.run(test_pjud_access())
