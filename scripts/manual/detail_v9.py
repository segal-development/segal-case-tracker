#!/usr/bin/env python3
"""
Test: v9 - Navegar a /misCausas.php y usar jQuery para el detalle.
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
    print('  v9 - Navegar a misCausas.php')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Home + Login
        print("1. Login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        
        jwt_token = await page.evaluate("""
            () => document.querySelector('input[name*="7f9d8a"]')?.value || ''
        """)
        
        await page.evaluate(f"""
            async () => {{
                const form = new URLSearchParams();
                form.append('7f9d8a6356360386f79afd5691435626f470dee1', '{jwt_token}');
                form.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                form.append('rut', '{rut_clean}');
                form.append('password', '{password}');
                
                await fetch('{base_url}/sessionN.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: form.toString(),
                    credentials: 'include'
                }});
            }}
        """)
        print("   ✓ Login OK")
        
        # 2. Navigate to misCausas.php (not index.php!)
        print()
        print("2. Navegando a /misCausas.php...")
        await page.goto(f"{base_url}/misCausas.php", wait_until="networkidle")
        await asyncio.sleep(3)
        
        print(f"   URL: {page.url}")
        
        # Check if jQuery is available
        has_jquery = await page.evaluate("typeof jQuery !== 'undefined'")
        print(f"   jQuery: {has_jquery}")
        
        # Check if logged in
        has_user = await page.evaluate("""
            () => document.body.innerText.includes('16021492') || 
                  document.body.innerText.includes('Carla')
        """)
        print(f"   Sesión: {has_user}")
        
        if not has_user:
            print("   ⚠️ No hay sesión")
            await page.screenshot(path="screenshots/no_session_v9.png")
            await browser.close()
            return
        
        # 3. Click on Civil tab
        print()
        print("3. Cargando Civil tab...")
        
        # Wait for tab to be available and click it
        try:
            await page.wait_for_selector('#misCausas-civ-tab', timeout=5000)
            await page.click('#misCausas-civ-tab')
            await asyncio.sleep(3)
            print("   ✓ Tab clickeado")
        except Exception as e:
            print(f"   Tab no encontrado: {e}")
        
        # 4. Get civil cases
        print()
        print("4. Cargando causas...")
        
        # Use the page's jQuery to call the function
        if has_jquery:
            civil_html = await page.evaluate(f"""
                () => {{
                    return new Promise((resolve) => {{
                        $.ajax({{
                            url: 'misCausas/civil/consultaMisCausasCivil.php',
                            type: 'POST',
                            data: {{
                                rutMisCauCiv: '{rut_clean}',
                                dvMisCauCiv: '{dv}',
                                tipoMisCauCiv: '0',
                                'tipCausaMisCauCiv[]': 'M'
                            }},
                            success: function(data) {{ resolve(data); }},
                            error: function(xhr, status, error) {{ 
                                resolve('ERROR:' + status + ':' + error); 
                            }}
                        }});
                    }});
                }}
            """)
        else:
            civil_html = await page.evaluate(f"""
                async () => {{
                    const form = new URLSearchParams();
                    form.append('rutMisCauCiv', '{rut_clean}');
                    form.append('dvMisCauCiv', '{dv}');
                    form.append('tipoMisCauCiv', '0');
                    form.append('tipCausaMisCauCiv[]', 'M');
                    
                    const resp = await fetch('misCausas/civil/consultaMisCausasCivil.php', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: form.toString(),
                        credentials: 'include'
                    }});
                    
                    return await resp.text();
                }}
            """)
        
        if civil_html.startswith('ERROR:'):
            print(f"   ✗ {civil_html}")
            await browser.close()
            return
        
        tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
        rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
        print(f"   Causas: {len(rols)}")
        
        if not tokens:
            print("   Sin tokens")
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0]
        
        # 5. Get detail using jQuery
        print()
        print(f"5. Detalle de {first_rol}...")
        
        if has_jquery:
            detail_html = await page.evaluate(f"""
                () => {{
                    return new Promise((resolve) => {{
                        $.ajax({{
                            url: 'misCausas/civil/modal/misCausasCivil.php',
                            dataType: 'html',
                            type: 'POST',
                            cache: false,
                            data: {{
                                dtaCausa: '{first_token}',
                                token: 'df32271e9cdca2704ff289941058a253'
                            }},
                            success: function(data) {{ resolve(data); }},
                            error: function(xhr, status, error) {{ 
                                resolve('JQUERY_ERROR:' + xhr.status + ':' + status + ':' + error); 
                            }}
                        }});
                    }});
                }}
            """)
        else:
            detail_html = await page.evaluate(f"""
                async () => {{
                    const form = new URLSearchParams();
                    form.append('dtaCausa', '{first_token}');
                    form.append('token', 'df32271e9cdca2704ff289941058a253');
                    
                    const resp = await fetch('misCausas/civil/modal/misCausasCivil.php', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: form.toString(),
                        credentials: 'include'
                    }});
                    
                    return 'STATUS:' + resp.status + '|' + await resp.text();
                }}
            """)
        
        print(f"   Tamaño: {len(detail_html)} chars")
        
        if 'ERROR' in detail_html or len(detail_html) < 100:
            print(f"   ⚠️ {detail_html[:200]}")
        else:
            with open("screenshots/detalle_civil.html", "w") as f:
                f.write(detail_html)
            
            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
            print(f"   ✓ Fechas: {len(fechas)}")
            
            rows = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
            print(f"   ✓ Filas: {len(rows)}")
            
            # Parse some info
            rit = re.search(r'Rit[:\s]*</?\w*>?\s*([^<\n]+)', detail_html, re.IGNORECASE)
            if rit:
                print(f"   ✓ Rit: {rit.group(1).strip()}")
        
        await browser.close()
        
        print()
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Token: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
