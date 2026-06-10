#!/usr/bin/env python3
"""
Test: Obtener detalle de causa Civil - v4 con espera robusta.
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
    print('  DETALLE CAUSA CIVIL v4')
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
        await page.goto(f"{base_url}/misCausas/index.php", wait_until="domcontentloaded")
        await asyncio.sleep(5)  # Wait for page scripts to load
        
        # Check if jQuery exists now
        has_jquery = await page.evaluate("() => typeof $ !== 'undefined' || typeof jQuery !== 'undefined'")
        print(f"   jQuery disponible: {has_jquery}")
        
        # 3. Load civil cases via direct AJAX call
        print()
        print("3. Cargando causas civiles via fetch...")
        
        civil_html = await page.evaluate(f"""
            async () => {{
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
        
        # Extract tokens
        tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
        rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
        
        print(f"   ✓ Encontradas {len(rols)} causas")
        
        if not tokens:
            print("   ✗ No hay tokens")
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0] if rols else "Unknown"
        
        # 4. Get detail - try via fetch with proper Referer
        print()
        print(f"4. Obteniendo detalle de {first_rol}...")
        
        # The key might be the Referer header
        detail_result = await page.evaluate(f"""
            async () => {{
                const detForm = new URLSearchParams();
                detForm.append('dtaCausa', '{first_token}');
                detForm.append('token', 'df32271e9cdca2704ff289941058a253');
                
                try {{
                    const resp = await fetch('{base_url}/misCausas/civil/modal/misCausasCivil.php', {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'X-Requested-With': 'XMLHttpRequest',
                            'Accept': 'text/html, */*; q=0.01'
                        }},
                        body: detForm.toString(),
                        credentials: 'include'
                    }});
                    
                    return {{
                        status: resp.status,
                        statusText: resp.statusText,
                        headers: Object.fromEntries(resp.headers.entries()),
                        html: await resp.text()
                    }};
                }} catch (e) {{
                    return {{ error: e.message }};
                }}
            }}
        """)
        
        print(f"   Status: {detail_result.get('status')} {detail_result.get('statusText', '')}")
        
        detail_html = detail_result.get('html', '')
        
        if detail_result.get('status') != 200:
            print(f"   Headers: {detail_result.get('headers', {})}")
            
            # Try alternative with jQuery if available
            if has_jquery:
                print()
                print("5. Intentando con jQuery...")
                
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
                                    resolve('JQUERY_ERROR: ' + status + ' - ' + error);
                                }}
                            }});
                        }});
                    }}
                """)
                print(f"   jQuery result length: {len(detail_html)}")
        
        # Save
        with open("screenshots/detalle_civil.html", "w") as f:
            f.write(detail_html)
        
        # 6. Analyze
        print()
        print("6. Analizando detalle...")
        
        if len(detail_html) < 100 or 'ERROR' in detail_html:
            print(f"   ⚠️ Respuesta: {detail_html[:300]}")
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
                print(f"   Fechas: {len(fechas)} (rango: {fechas[-1]} - {fechas[0]})")
        
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
