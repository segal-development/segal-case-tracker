#!/usr/bin/env python3
"""
Test: Obtener detalle de causa Civil - v3 navegando como usuario real.
"""

import asyncio
import re
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_detail_civil(captcha_token: str):
    rut_clean = '16021492'
    dv = '9'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  DETALLE CAUSA CIVIL v3 - Navegación real')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Login via API
        print("1. Haciendo login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
        await page.evaluate(f"""
            async () => {{
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
            }}
        """)
        print("   ✓ Login completado")
        
        # 2. Navigate to Mis Causas
        print()
        print("2. Navegando a Mis Causas...")
        await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
        await asyncio.sleep(2)
        
        # Wait for jQuery to be available
        await page.wait_for_function("typeof $ !== 'undefined'", timeout=10000)
        print("   ✓ Página cargada con jQuery")
        
        # 3. Click on Civil tab and wait for results
        print()
        print("3. Cargando causas civiles...")
        
        # Click civil tab
        await page.click('#misCausas-civ-tab')
        await asyncio.sleep(3)
        
        # Get the table HTML
        table_html = await page.evaluate("""
            () => document.querySelector('#verDetalleMisCauCiv')?.innerHTML || ''
        """)
        
        # Extract tokens
        tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", table_html)
        rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', table_html)
        
        print(f"   ✓ Encontradas {len(rols)} causas")
        
        if not tokens:
            print("   ✗ No hay tokens")
            await page.screenshot(path="screenshots/no_tokens.png")
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0] if rols else "Unknown"
        
        # 4. Use jQuery to get detail (via page context)
        print()
        print(f"4. Obteniendo detalle de {first_rol}...")
        
        detail_html = await page.evaluate(f"""
            () => {{
                return new Promise((resolve) => {{
                    $.ajax({{
                        url: 'civil/modal/misCausasCivil.php',
                        dataType: 'html',
                        type: 'POST',
                        cache: false,
                        data: {{
                            dtaCausa: '{first_token}',
                            token: 'df32271e9cdca2704ff289941058a253'
                        }},
                        success: function(data) {{
                            resolve(data);
                        }},
                        error: function(xhr, status, error) {{
                            resolve('ERROR: ' + status + ' - ' + error + ' - ' + xhr.responseText);
                        }}
                    }});
                }});
            }}
        """)
        
        print(f"   Tamaño respuesta: {len(detail_html)} chars")
        
        # Save
        with open("screenshots/detalle_civil.html", "w") as f:
            f.write(detail_html)
        
        # 5. Analyze
        print()
        print("5. Analizando detalle...")
        
        if detail_html.startswith('ERROR:'):
            print(f"   ✗ {detail_html}")
        elif len(detail_html) < 100:
            print(f"   ⚠️ Respuesta corta: {detail_html}")
        else:
            # Extract info
            rol_match = re.search(r'<b>Rit:</b>\s*([^<]+)', detail_html)
            if rol_match:
                print(f"   ROL: {rol_match.group(1).strip()}")
            
            trib_match = re.search(r'<b>Tribunal:</b>\s*([^<]+)', detail_html)
            if trib_match:
                print(f"   Tribunal: {trib_match.group(1).strip()}")
            
            cara_match = re.search(r'<b>Caratulado:</b>\s*([^<]+)', detail_html)
            if cara_match:
                print(f"   Caratulado: {cara_match.group(1).strip()}")
            
            # Count rows 
            tramites = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
            print(f"   Filas HTML: {len(tramites)}")
            
            # Movement dates
            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
            if fechas:
                print(f"   Fechas: {len(fechas)} (primera: {fechas[0]}, última: {fechas[-1] if len(fechas) > 1 else fechas[0]})")
            
            # Tabs
            tabs = re.findall(r'data-toggle="(?:tab|pill)"[^>]*>([^<]+)', detail_html)
            if tabs:
                print(f"   Tabs: {[t.strip() for t in tabs]}")
            
            # Look for Historial section
            if 'historial' in detail_html.lower() or 'tramit' in detail_html.lower():
                print("   ✓ Contiene sección de historial/tramitación")
            
            # Extract some movement descriptions
            movs = re.findall(r'<td[^>]*>([^<]{10,100})</td>', detail_html)
            unique_movs = list(set([m.strip() for m in movs if not m.strip().startswith(('C-', 'V-', '<'))]))[:5]
            if unique_movs:
                print(f"   Movimientos ejemplo: {unique_movs}")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ Ver screenshots/detalle_civil.html")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
