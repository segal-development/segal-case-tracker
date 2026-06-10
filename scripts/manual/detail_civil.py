#!/usr/bin/env python3
"""
Test: Obtener detalle de causa Civil con token JWT.
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
    print('  DETALLE CAUSA CIVIL')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Login + Get cases
        print("1. Login y obtener lista de causas...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
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
                
                // Get Civil cases
                const civForm = new URLSearchParams();
                civForm.append('rutMisCauCiv', '{rut_clean}');
                civForm.append('dvMisCauCiv', '{dv}');
                civForm.append('tipoMisCauCiv', '0');
                civForm.append('rolMisCauCiv', '');
                civForm.append('anhoMisCauCiv', '');
                civForm.append('tipCausaMisCauCiv[]', 'M');
                civForm.append('nombreMisCauCiv', '');
                civForm.append('apePatMisCauCiv', '');
                civForm.append('apeMatMisCauCiv', '');
                
                const civResp = await fetch('{base_url}/misCausas/civil/consultaMisCausasCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: civForm.toString(),
                    credentials: 'include'
                }});
                
                return await civResp.text();
            }}
        """)
        
        # Extract first JWT token
        tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", result)
        rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', result)
        
        print(f"   ✓ Encontradas {len(rols)} causas")
        
        if not tokens:
            print("   ✗ No hay tokens para consultar detalle")
            await browser.close()
            return
        
        # 2. Get detail of first case
        first_token = tokens[0]
        first_rol = rols[0] if rols else "Unknown"
        
        print()
        print(f"2. Obteniendo detalle de {first_rol}...")
        print(f"   Token (primeros 50 chars): {first_token[:50]}...")
        
        detail = await page.evaluate(f"""
            async () => {{
                const detForm = new URLSearchParams();
                detForm.append('dtaCausa', '{first_token}');
                detForm.append('token', 'df32271e9cdca2704ff289941058a253');
                
                const detResp = await fetch('{base_url}/misCausas/civil/modal/misCausasCivil.php', {{
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
        
        # Save
        with open("screenshots/detalle_civil.html", "w") as f:
            f.write(detail_html)
        
        print(f"   Status: {detail.get('status')}")
        print(f"   Tamaño: {len(detail_html)} chars")
        
        # 3. Parse detail
        print()
        print("3. Analizando detalle...")
        
        # Check for error
        if 'error' in detail_html.lower() or len(detail_html) < 500:
            print(f"   ⚠️ Posible error. Primeros 500 chars:")
            print(f"   {detail_html[:500]}")
        else:
            # Extract case info
            # ROL
            rol_match = re.search(r'<b>Rit:</b>\s*([^<]+)', detail_html)
            if rol_match:
                print(f"   ROL: {rol_match.group(1).strip()}")
            
            # Tribunal
            trib_match = re.search(r'<b>Tribunal:</b>\s*([^<]+)', detail_html)
            if trib_match:
                print(f"   Tribunal: {trib_match.group(1).strip()}")
            
            # Caratulado
            cara_match = re.search(r'<b>Caratulado:</b>\s*([^<]+)', detail_html)
            if cara_match:
                print(f"   Caratulado: {cara_match.group(1).strip()}")
            
            # Count tramites/movements
            tramites = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
            print(f"   Filas en detalle: {len(tramites)}")
            
            # Look for tabs (Historial, Demandante, etc)
            tabs = re.findall(r'<a[^>]*data-toggle="tab"[^>]*>([^<]+)</a>', detail_html)
            if tabs:
                print(f"   Tabs: {tabs}")
            
            # Look for movement dates
            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
            if fechas:
                print(f"   Fechas encontradas: {len(fechas)} (primera: {fechas[0]}, última: {fechas[-1]})")
        
        # 4. Also try to get Historial (tramites)
        print()
        print("4. Buscando endpoint de historial/tramites...")
        
        # Check if there's a separate historial endpoint
        historial_match = re.search(r"(historial|tramite|historia)[^'\"]*\.php", detail_html, re.IGNORECASE)
        if historial_match:
            print(f"   Encontrado endpoint: {historial_match.group(0)}")
        else:
            print("   No hay endpoint separado - tramites incluidos en el modal")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ COMPLETADO - Ver screenshots/detalle_civil.html")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
