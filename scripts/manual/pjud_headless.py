"""
Script de prueba HEADLESS para capturar la estructura del PJUD
"""
import asyncio
from playwright.async_api import async_playwright


async def test_pjud_access():
    """Prueba acceso al PJUD y captura estructura HTML"""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        print("=" * 60)
        print("PASO 1: Navegando al portal PJUD...")
        print("=" * 60)
        
        try:
            await page.goto("https://oficinajudicialvirtual.pjud.cl", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            print(f"Error cargando página: {e}")
        
        # Guardar screenshot inicial
        await page.screenshot(path="screenshots/01_home.png", full_page=True)
        print("Screenshot guardado: 01_home.png")
        
        # Guardar HTML
        html_content = await page.content()
        with open("screenshots/01_home.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        print("HTML guardado: 01_home.html")
        
        # Analizar estructura
        print("\n" + "=" * 60)
        print("ANÁLISIS DE LA PÁGINA")
        print("=" * 60)
        
        # Título
        title = await page.title()
        print(f"\nTítulo: {title}")
        
        # URL actual
        print(f"URL: {page.url}")
        
        # Buscar formularios
        print("\n--- FORMULARIOS ---")
        forms = await page.query_selector_all("form")
        for i, form in enumerate(forms):
            form_id = await form.get_attribute("id") or "sin-id"
            form_name = await form.get_attribute("name") or "sin-nombre"
            form_action = await form.get_attribute("action") or "sin-action"
            print(f"  Form {i+1}: id={form_id}, name={form_name}, action={form_action}")
        
        # Buscar inputs
        print("\n--- INPUTS ---")
        inputs = await page.query_selector_all("input")
        for inp in inputs[:15]:  # Primeros 15
            inp_id = await inp.get_attribute("id") or ""
            inp_name = await inp.get_attribute("name") or ""
            inp_type = await inp.get_attribute("type") or "text"
            inp_placeholder = await inp.get_attribute("placeholder") or ""
            if inp_id or inp_name:
                print(f"  <input id='{inp_id}' name='{inp_name}' type='{inp_type}' placeholder='{inp_placeholder}'>")
        
        # Buscar botones
        print("\n--- BOTONES ---")
        buttons = await page.query_selector_all("button")
        for btn in buttons[:10]:
            btn_id = await btn.get_attribute("id") or ""
            btn_class = await btn.get_attribute("class") or ""
            btn_text = (await btn.inner_text()).strip()[:50]
            print(f"  <button id='{btn_id}' class='{btn_class[:30]}'>{btn_text}</button>")
        
        # Buscar links importantes
        print("\n--- LINKS DE LOGIN ---")
        links = await page.query_selector_all("a")
        for link in links:
            text = (await link.inner_text()).strip().lower()
            href = await link.get_attribute("href") or ""
            if any(word in text for word in ["clave", "login", "ingresar", "acceso", "entrar"]):
                print(f"  <a href='{href}'>{text[:50]}</a>")
        
        # Buscar divs/modals de login
        print("\n--- POSIBLES MODALS DE LOGIN ---")
        modals = await page.query_selector_all("[id*='login'], [id*='clave'], [class*='login'], [class*='modal']")
        for modal in modals[:10]:
            m_id = await modal.get_attribute("id") or ""
            m_class = await modal.get_attribute("class") or ""
            print(f"  <div id='{m_id}' class='{m_class[:50]}'>")
        
        print("\n" + "=" * 60)
        print("INTENTANDO ACCEDER A CONSULTA DE CAUSAS...")
        print("=" * 60)
        
        # Intentar acceso como invitado para consulta
        try:
            # Método 1: POST directo a sesion-invitado
            response = await page.evaluate("""
                async () => {
                    const response = await fetch('/includes/sesion-invitado.php', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'nombreAcceso=CC'
                    });
                    return await response.text();
                }
            """)
            print(f"Respuesta sesion-invitado: {response[:200] if response else 'vacía'}")
            
            # Navegar a indexN.php
            await page.goto("https://oficinajudicialvirtual.pjud.cl/indexN.php", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            
            await page.screenshot(path="screenshots/02_index.png", full_page=True)
            print("\nScreenshot guardado: 02_index.png")
            
            html2 = await page.content()
            with open("screenshots/02_index.html", "w", encoding="utf-8") as f:
                f.write(html2)
            print("HTML guardado: 02_index.html")
            
            print(f"\nURL actual: {page.url}")
            print(f"Título: {await page.title()}")
            
        except Exception as e:
            print(f"Error: {e}")
        
        await browser.close()
        print("\n✅ Proceso completado. Revisa los archivos en /screenshots/")


if __name__ == "__main__":
    import os
    os.makedirs("screenshots", exist_ok=True)
    asyncio.run(test_pjud_access())
