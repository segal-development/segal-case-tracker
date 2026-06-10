#!/usr/bin/env python3
"""
Consultar causas civiles directamente via AJAX endpoint.
Versión simplificada que evita navegaciones problemáticas.
"""

import asyncio
import re
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_causas_civiles(captcha_token: str):
    rut_clean = '16021492'
    dv = '9'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  CONSULTAR CAUSAS CIVILES - PJUD')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Go to login page
        print("1. Cargando página de login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        # Get JWT token
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        print(f"   JWT token: {jwt_token[:20]}...")
        
        # 2. Login via fetch
        print("2. Haciendo login...")
        login_result = await page.evaluate(f"""
            async () => {{
                const formData = new URLSearchParams();
                formData.append('7f9d8a6356360386f79afd5691435626f470dee1', '{jwt_token}');
                formData.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                formData.append('rut', '{rut_clean}');
                formData.append('password', '{password}');
                
                const response = await fetch('{base_url}/sessionN.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: formData.toString(),
                    credentials: 'include'
                }});
                
                return {{
                    status: response.status,
                    text: await response.text()
                }};
            }}
        """)
        print(f"   Login status: {login_result['status']}")
        
        # 3. Get cookies
        cookies = await context.cookies()
        session_cookies = [c for c in cookies if 'PHPSESSID' in c['name'] or 'pjud' in c['name'].lower()]
        print(f"   Cookies de sesión: {len(session_cookies)}")
        
        # 4. Query causas civiles directly - DON'T navigate, just fetch
        print("3. Consultando causas civiles...")
        
        causas_html = await page.evaluate(f"""
            async () => {{
                const formData = new URLSearchParams();
                formData.append('rutMisCauCiv', '{rut_clean}');
                formData.append('dvMisCauCiv', '{dv}');
                formData.append('tipoMisCauCiv', '0');
                formData.append('rolMisCauCiv', '');
                formData.append('anhoMisCauCiv', '');
                formData.append('tipCausaMisCauCiv', 'M');
                formData.append('estadoCausaMisCauCiv', '1');
                formData.append('nombreMisCauCiv', '');
                formData.append('apePatMisCauCiv', '');
                formData.append('apeMatMisCauCiv', '');
                
                const response = await fetch('{base_url}/misCausas/civil/consultaMisCausasCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: formData.toString(),
                    credentials: 'include'
                }});
                
                return await response.text();
            }}
        """)
        
        with open("screenshots/causas_civiles_activas.html", "w") as f:
            f.write(causas_html)
        print(f"   ✓ Respuesta guardada ({len(causas_html)} chars)")
        
        # Check if it's an error
        if '404' in causas_html or 'Not Found' in causas_html:
            print("   ⚠️  Error 404 - probando ruta alternativa...")
            
            # Maybe we need to be on indexN.php first to establish proper session
            # Let's try a different approach - use the page directly
            print("   Navegando a indexN.php...")
            await page.goto(f"{base_url}/indexN.php", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            
            # Take screenshot
            await page.screenshot(path="screenshots/indexN_page.png", full_page=True)
            print("   ✓ Screenshot guardado")
            
            # Now try calling misCausas() function if it exists
            print("   Intentando llamar a misCausas()...")
            try:
                await page.evaluate("misCausas()")
                await asyncio.sleep(2)
                await page.screenshot(path="screenshots/mis_causas_loaded.png", full_page=True)
                print("   ✓ misCausas() ejecutado")
                
                # Now get the civil causas
                causas_html = await page.evaluate(f"""
                    async () => {{
                        const formData = new URLSearchParams();
                        formData.append('rutMisCauCiv', '{rut_clean}');
                        formData.append('dvMisCauCiv', '{dv}');
                        formData.append('tipoMisCauCiv', '0');
                        formData.append('rolMisCauCiv', '');
                        formData.append('anhoMisCauCiv', '');
                        formData.append('tipCausaMisCauCiv', 'M');
                        formData.append('estadoCausaMisCauCiv', '0');
                        formData.append('nombreMisCauCiv', '');
                        formData.append('apePatMisCauCiv', '');
                        formData.append('apeMatMisCauCiv', '');
                        
                        const response = await fetch('misCausas/civil/consultaMisCausasCivil.php', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: formData.toString(),
                            credentials: 'include'
                        }});
                        
                        return await response.text();
                    }}
                """)
                
                with open("screenshots/causas_civiles_todas.html", "w") as f:
                    f.write(causas_html)
                print(f"   ✓ Causas guardadas ({len(causas_html)} chars)")
                
            except Exception as e:
                print(f"   ✗ Error: {e}")
        
        # Analyze
        print()
        print("4. Analizando resultados...")
        rit_pattern = r'[CVEAFI]-\d+-\d{4}'
        rits = re.findall(rit_pattern, causas_html)
        tr_count = len(re.findall(r'<tr[^>]*>', causas_html, re.IGNORECASE))
        
        print(f"   Filas <tr>: {tr_count}")
        print(f"   RITs encontrados: {len(rits)}")
        
        if rits:
            print(f"   Primeros 10: {rits[:10]}")
            
            # Look for detail handlers
            onclick_pattern = r'onclick="([^"]*)"'
            onclicks = re.findall(onclick_pattern, causas_html)
            print(f"   Onclick handlers: {len(onclicks)}")
            if onclicks[:3]:
                for o in onclicks[:3]:
                    print(f"     - {o[:80]}...")
        else:
            # Show first 500 chars of response for debugging
            print(f"   Preview de respuesta: {causas_html[:500]}")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ COMPLETADO")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_causas_civiles(token))
