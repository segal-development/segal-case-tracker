#!/usr/bin/env python3
"""
Acceder a "Mis Causas" en PJUD y capturar la estructura.
"""

import asyncio
import json
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_mis_causas(captcha_token: str):
    rut = '16021492-9'
    rut_clean = '16021492'
    password = 'Gruposegal2026+'
    
    print('=' * 60)
    print('  ACCEDER A MIS CAUSAS - PJUD')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Login
        print("1. Haciendo login...")
        await page.goto("https://oficinajudicialvirtual.pjud.cl/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
        login_result = await page.evaluate(f"""
            async () => {{
                const formData = new URLSearchParams();
                formData.append('7f9d8a6356360386f79afd5691435626f470dee1', '{jwt_token}');
                formData.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                formData.append('rut', '{rut_clean}');
                formData.append('password', '{password}');
                
                const response = await fetch('https://oficinajudicialvirtual.pjud.cl/sessionN.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: formData.toString(),
                    credentials: 'include'
                }});
                
                return await response.text();
            }}
        """)
        
        await page.evaluate("""
            () => {
                localStorage.setItem('InitSitioOld', '0');
                localStorage.setItem('InitSitioNew', '1');
                localStorage.setItem('logged-in', 'true');
                sessionStorage.setItem('logged-in', 'true');
            }
        """)
        
        print("   ✓ Login completado")
        
        # 2. Navigate to index
        print("2. Navegando a página principal...")
        await page.goto("https://oficinajudicialvirtual.pjud.cl/indexN.php", wait_until="networkidle")
        await asyncio.sleep(2)
        
        # 3. Click on "Mis Causas" - call the JS function
        print("3. Cargando 'Mis Causas'...")
        
        # Load misCausas.php via AJAX like the menu does
        causas_html = await page.evaluate("""
            async () => {
                const response = await fetch('misCausas.php', {
                    credentials: 'include'
                });
                return await response.text();
            }
        """)
        
        # Save the raw response
        with open("screenshots/mis_causas_raw.html", "w") as f:
            f.write(causas_html)
        print(f"   ✓ HTML guardado: mis_causas_raw.html ({len(causas_html)} chars)")
        
        # Also inject it into the page to render
        await page.evaluate(f"""
            () => {{
                document.getElementById('contMain').innerHTML = `{causas_html.replace('`', '\\`').replace('${', '\\${')}`;
            }}
        """)
        await asyncio.sleep(1)
        
        # Take screenshot
        await page.screenshot(path="screenshots/mis_causas.png", full_page=True)
        print("   ✓ Screenshot: mis_causas.png")
        
        # Save full page HTML
        full_html = await page.content()
        with open("screenshots/mis_causas_full.html", "w") as f:
            f.write(full_html)
        
        # 4. Analyze the content
        print()
        print("4. Analizando contenido...")
        
        # Look for tables
        tables = await page.query_selector_all("table")
        print(f"   Tablas encontradas: {len(tables)}")
        
        for i, table in enumerate(tables):
            table_id = await table.get_attribute("id") or "sin-id"
            table_class = await table.get_attribute("class") or "sin-clase"
            rows = await table.query_selector_all("tr")
            print(f"     Tabla {i}: id='{table_id}', class='{table_class[:50]}', filas={len(rows)}")
        
        # Look for specific elements
        print()
        print("5. Buscando elementos de causas...")
        
        # Common patterns for case tables
        selectors_to_try = [
            "#tablaCausas",
            "#gridCausas", 
            ".tabla-causas",
            "[id*='causa']",
            "[class*='causa']",
            "table tbody tr",
        ]
        
        for selector in selectors_to_try:
            elements = await page.query_selector_all(selector)
            if elements:
                print(f"   ✓ '{selector}': {len(elements)} elementos")
        
        # 5. Try consultaUnificada too
        print()
        print("6. Probando Consulta Unificada...")
        
        consulta_html = await page.evaluate("""
            async () => {
                const response = await fetch('consultaUnificada.php', {
                    credentials: 'include'
                });
                return await response.text();
            }
        """)
        
        with open("screenshots/consulta_unificada_raw.html", "w") as f:
            f.write(consulta_html)
        print(f"   ✓ HTML guardado: consulta_unificada_raw.html ({len(consulta_html)} chars)")
        
        # Inject and screenshot
        await page.evaluate(f"""
            () => {{
                document.getElementById('contMain').innerHTML = `{consulta_html.replace('`', '\\`').replace('${', '\\${')}`;
            }}
        """)
        await asyncio.sleep(1)
        
        await page.screenshot(path="screenshots/consulta_unificada.png", full_page=True)
        print("   ✓ Screenshot: consulta_unificada.png")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ COMPLETADO")
        print("=" * 60)
        print()
        print("Revisa:")
        print("  - screenshots/mis_causas.png")
        print("  - screenshots/mis_causas_raw.html")
        print("  - screenshots/consulta_unificada.png")
        print("  - screenshots/consulta_unificada_raw.html")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_mis_causas(token))
