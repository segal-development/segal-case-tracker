#!/usr/bin/env python3
"""
Test: Obtener detalle de causa Civil - v2 con headers AJAX correctos.
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
    print('  DETALLE CAUSA CIVIL v2')
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
        
        # 2. Get detail with AJAX headers
        first_token = tokens[0]
        first_rol = rols[0] if rols else "Unknown"
        
        print()
        print(f"2. Obteniendo detalle de {first_rol}...")
        
        # Try with jQuery AJAX style headers
        detail = await page.evaluate(f"""
            async () => {{
                const detForm = new URLSearchParams();
                detForm.append('dtaCausa', '{first_token}');
                detForm.append('token', 'df32271e9cdca2704ff289941058a253');
                
                // Use same headers as jQuery AJAX
                const detResp = await fetch('{base_url}/misCausas/civil/modal/misCausasCivil.php', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'text/html, */*; q=0.01'
                    }},
                    body: detForm.toString(),
                    credentials: 'include'
                }});
                
                return {{
                    status: detResp.status,
                    statusText: detResp.statusText,
                    html: await detResp.text()
                }};
            }}
        """)
        
        detail_html = detail.get('html', '')
        
        print(f"   Status: {detail.get('status')} {detail.get('statusText')}")
        print(f"   Tamaño: {len(detail_html)} chars")
        
        if detail.get('status') != 200 or len(detail_html) < 100:
            # Try alternative: use jQuery from page context
            print()
            print("3. Intentando con jQuery desde contexto de página...")
            
            # Navigate to mis causas page first
            await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
            await asyncio.sleep(2)
            
            # Use jQuery
            detail2 = await page.evaluate(f"""
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
                                resolve({{ status: 200, html: data }});
                            }},
                            error: function(xhr) {{
                                resolve({{ status: xhr.status, html: xhr.responseText || '' }});
                            }}
                        }});
                    }});
                }}
            """)
            
            detail_html = detail2.get('html', '')
            print(f"   Status: {detail2.get('status')}")
            print(f"   Tamaño: {len(detail_html)} chars")
        
        # Save
        with open("screenshots/detalle_civil.html", "w") as f:
            f.write(detail_html)
        
        # 4. Analyze
        print()
        print("4. Analizando detalle...")
        
        if len(detail_html) < 100:
            print(f"   ⚠️ Respuesta muy corta: {detail_html[:200]}")
        else:
            # Extract info
            rol_match = re.search(r'Rit[:\s]*</?\w*>\s*([^<\n]+)', detail_html, re.IGNORECASE)
            if rol_match:
                print(f"   ROL: {rol_match.group(1).strip()}")
            
            trib_match = re.search(r'Tribunal[:\s]*</?\w*>\s*([^<\n]+)', detail_html, re.IGNORECASE)
            if trib_match:
                print(f"   Tribunal: {trib_match.group(1).strip()}")
            
            cara_match = re.search(r'Caratulado[:\s]*</?\w*>\s*([^<\n]+)', detail_html, re.IGNORECASE)
            if cara_match:
                print(f"   Caratulado: {cara_match.group(1).strip()}")
            
            # Count rows (tramites)
            tramites = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
            print(f"   Filas HTML: {len(tramites)}")
            
            # Movement dates
            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
            if fechas:
                print(f"   Fechas: {len(fechas)} (primera: {fechas[0]}, última: {fechas[-1]})")
            
            # Tabs
            tabs = re.findall(r'data-toggle="tab"[^>]*>([^<]+)', detail_html)
            if tabs:
                print(f"   Tabs: {tabs}")
        
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
