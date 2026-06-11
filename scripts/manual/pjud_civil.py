"""
Script para explorar la sección de consulta de causas civiles
"""
import asyncio
from playwright.async_api import async_playwright


async def test_consulta_civil():
    """Explora la consulta de causas civiles"""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        print("=" * 60)
        print("EXPLORANDO CONSULTA DE CAUSAS CIVILES")
        print("=" * 60)
        
        # 1. Ir al home
        await page.goto("https://oficinajudicialvirtual.pjud.cl/home/index.php")
        await page.wait_for_load_state("networkidle")
        print("✓ Página principal cargada")
        
        # 2. Establecer sesión de invitado con JS
        await page.evaluate("""
            localStorage.setItem('InitSitioOld', '0');
            localStorage.setItem('InitSitioNew', '1');
            localStorage.setItem('logged-in', 'true');
            sessionStorage.setItem('logged-in', 'true');
        """)
        print("✓ Sesión de invitado configurada")
        
        # 3. Hacer POST para sesión de consulta
        await page.evaluate("""
            fetch('../includes/sesion-invitado.php', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'nombreAcceso=CC'
            });
        """)
        await asyncio.sleep(1)
        print("✓ POST sesion-invitado enviado")
        
        # 4. Navegar a la consulta unificada
        print("\nNavegando a consulta unificada...")
        await page.goto("https://oficinajudicialvirtual.pjud.cl/consultaunificadacausas.php")
        await asyncio.sleep(2)
        await page.screenshot(path="screenshots/03_consulta_unificada.png", full_page=True)
        
        html = await page.content()
        with open("screenshots/03_consulta_unificada.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"URL: {page.url}")
        print("Screenshot: 03_consulta_unificada.png")
        
        # 5. Probar otras URLs de consulta
        urls_to_try = [
            "https://oficinajudicialvirtual.pjud.cl/ADIR_871/civil/",
            "https://oficinajudicialvirtual.pjud.cl/frameInv.php",
            "https://oficinajudicialvirtual.pjud.cl/indexN.php",
            "https://civil.pjud.cl",
        ]
        
        for i, url in enumerate(urls_to_try, start=4):
            try:
                print(f"\nProbando: {url}")
                await page.goto(url, timeout=15000)
                await asyncio.sleep(2)
                await page.screenshot(path=f"screenshots/{i:02d}_{url.split('/')[-1] or 'page'}.png", full_page=True)
                print(f"  URL final: {page.url}")
                print(f"  Título: {await page.title()}")
                
                # Guardar HTML
                html = await page.content()
                with open(f"screenshots/{i:02d}_{url.split('/')[-1] or 'page'}.html", "w", encoding="utf-8") as f:
                    f.write(html)
                    
            except Exception as e:
                print(f"  Error: {str(e)[:100]}")
        
        # 6. Buscar en la página actual los elementos de búsqueda
        print("\n" + "=" * 60)
        print("BUSCANDO FORMULARIOS DE CONSULTA")
        print("=" * 60)
        
        await page.goto("https://oficinajudicialvirtual.pjud.cl/home/index.php")
        await page.wait_for_load_state("networkidle")
        
        # Hacer clic en Consulta causas
        try:
            btn = await page.query_selector("button:has-text('Consulta causas')")
            if btn:
                await btn.click()
                await asyncio.sleep(2)
                print("✓ Click en 'Consulta causas'")
                
                await page.screenshot(path="screenshots/10_after_click_consulta.png", full_page=True)
                
                # Ver qué opciones aparecen
                dropdown = await page.query_selector_all(".dropdown-content a, .dropdown-menu a")
                print("\nOpciones del menú:")
                for item in dropdown[:15]:
                    text = await item.inner_text()
                    href = await item.get_attribute("href") or ""
                    onclick = await item.get_attribute("onclick") or ""
                    if text.strip():
                        print(f"  - {text.strip()[:40]} | href={href[:30]} | onclick={onclick[:50]}")
        except Exception as e:
            print(f"Error: {e}")
        
        await browser.close()
        print("\n✅ Exploración completada")


if __name__ == "__main__":
    import os
    os.makedirs("screenshots", exist_ok=True)
    asyncio.run(test_consulta_civil())
