#!/usr/bin/env python3
"""
Probar Mis Causas de CORTE SUPREMA.
Las causas de Carla Lavín están ahí!
"""

import asyncio
import re
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_mis_causas_suprema(captcha_token: str):
    rut_clean = '16021492'
    dv = '9'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  MIS CAUSAS - CORTE SUPREMA')
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
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
        # Login + search Suprema
        result = await page.evaluate(f"""
            async () => {{
                // Login
                const loginForm = new URLSearchParams();
                loginForm.append('7f9d8a6356360386f79afd5691435626f470dee1', '{jwt_token}');
                loginForm.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                loginForm.append('rut', '{rut_clean}');
                loginForm.append('password', '{password}');
                
                await fetch('{base_url}/sessionN.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: loginForm.toString(),
                    credentials: 'include'
                }});
                
                // Suprema - estado 0 = todas las causas
                const supForm = new URLSearchParams();
                supForm.append('rutMisCauSup', '{rut_clean}');
                supForm.append('dvMisCauSup', '{dv}');
                supForm.append('tipoMisCauSup', '0');
                supForm.append('rolMisCauSup', '');
                supForm.append('anhoMisCauSup', '');
                supForm.append('tipCausaMisCauSup', 'M');
                supForm.append('estadoCausaMisCauSup', '0');  // 0 = todas
                
                const supResp = await fetch('{base_url}/misCausas/suprema/consultaMisCausasSuprema.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: supForm.toString(),
                    credentials: 'include'
                }});
                
                return await supResp.text();
            }}
        """)
        
        print("   ✓ Login y consulta completados")
        print()
        
        # Save
        with open("screenshots/mis_causas_suprema.html", "w") as f:
            f.write(result)
        
        # Analyze
        print("2. Analizando resultados...")
        
        # Count rows with onclick handlers
        handlers = re.findall(r'onclick="[^"]*\(\'([^\']+)\'\)"', result, re.IGNORECASE)
        rows = re.findall(r'<tr[^>]*>.*?</tr>', result, re.DOTALL | re.IGNORECASE)
        
        # Extract ROLs
        rols = re.findall(r'>(\d+-\d{4})<', result)
        
        print(f"   Handlers encontrados: {len(handlers)}")
        print(f"   Filas: {len(rows)}")
        print(f"   ROLs: {rols}")
        
        if handlers:
            print()
            print("3. Tokens de causas:")
            for i, token in enumerate(handlers[:5]):
                print(f"   {i+1}. {token[:80]}...")
            
            # Get detail of first case
            print()
            print("4. Obteniendo detalle de primera causa...")
            
            first_token = handlers[0]
            
            detail = await page.evaluate(f"""
                async () => {{
                    const detForm = new URLSearchParams();
                    detForm.append('dtaCausa', '{first_token}');
                    detForm.append('token', 'df32271e9cdca2704ff289941058a253');
                    
                    const detResp = await fetch('{base_url}/misCausas/suprema/modal/misCausasSuprema.php', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: detForm.toString(),
                        credentials: 'include'
                    }});
                    
                    return {{
                        status: detResp.status,
                        html: await detResp.text()
                    }};
                }}
            """)
            
            detail_html = detail.get('html', '')
            with open("screenshots/detalle_suprema.html", "w") as f:
                f.write(detail_html)
            
            print(f"   Status: {detail.get('status')}")
            print(f"   Tamaño: {len(detail_html)} chars")
            
            # Check content
            if len(detail_html) > 500:
                # Look for movements/tramites
                movs = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
                print(f"   Filas en detalle: {len(movs)}")
                
                # Keywords
                keywords = ['tramit', 'movimiento', 'resolución', 'escrito', 'fallo']
                found = [k for k in keywords if k in detail_html.lower()]
                print(f"   Keywords: {found}")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ COMPLETADO")
        print("=" * 60)
        print()
        print("Archivos:")
        print("  - screenshots/mis_causas_suprema.html")
        print("  - screenshots/detalle_suprema.html")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_mis_causas_suprema(token))
