#!/usr/bin/env python3
"""
Test: v13 - Usar /misCausas.php (no /misCausas/index.php)
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
    print('  v13 - Detalle causa Civil')
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
        print("1. Login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        
        jwt_token = await page.evaluate("() => document.querySelector('input[name*=\"7f9d8a\"]')?.value || ''")
        
        await page.evaluate(f"""
            () => {{
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = '{base_url}/sessionN.php';
                
                const fields = {{
                    '7f9d8a6356360386f79afd5691435626f470dee1': '{jwt_token}',
                    'g-recaptcha-response-seg-clave_hn': '{captcha_token}',
                    'rut': '{rut_clean}',
                    'password': '{password}'
                }};
                
                for (const [name, value] of Object.entries(fields)) {{
                    const input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = name;
                    input.value = value;
                    form.appendChild(input);
                }}
                
                document.body.appendChild(form);
                form.submit();
            }}
        """)
        
        await asyncio.sleep(5)
        print(f"   ✓ URL: {page.url}")
        
        # 2. Go to misCausas.php (NOT misCausas/index.php!)
        print()
        print("2. Navegando a /misCausas.php...")
        await page.goto(f"{base_url}/misCausas.php", wait_until="networkidle")
        
        # Wait for jQuery
        for i in range(10):
            has_jquery = await page.evaluate("typeof jQuery !== 'undefined'")
            if has_jquery:
                break
            await asyncio.sleep(1)
        
        print(f"   URL: {page.url}")
        print(f"   jQuery: {has_jquery}")
        
        # Check page content
        content = await page.content()
        has_user = '16021492' in content or 'Carla' in content
        print(f"   Sesión: {has_user}")
        
        if not has_jquery:
            print("   ⚠️ Sin jQuery")
            with open("screenshots/miscausas_debug.html", "w") as f:
                f.write(content)
            await browser.close()
            return
        
        # 3. Load civil cases - URLs are relative to misCausas.php
        print()
        print("3. Cargando causas civiles...")
        
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
                        error: function(xhr) {{ resolve('ERROR:' + xhr.status); }}
                    }});
                }});
            }}
        """)
        
        if 'ERROR' in civil_html:
            print(f"   ⚠️ {civil_html}")
            await browser.close()
            return
        
        tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
        rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
        print(f"   ✓ Causas: {len(rols)}")
        
        if not tokens:
            print("   Sin tokens")
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0]
        
        # 4. Get detail
        print()
        print(f"4. Detalle de {first_rol}...")
        
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
                        error: function(xhr) {{ resolve('ERROR:' + xhr.status); }}
                    }});
                }});
            }}
        """)
        
        print(f"   Tamaño: {len(detail_html)} chars")
        
        if 'ERROR' in detail_html or len(detail_html) < 100:
            print(f"   ⚠️ {detail_html[:300]}")
        else:
            with open("screenshots/detalle_civil.html", "w") as f:
                f.write(detail_html)
            
            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
            print(f"   ✓ Fechas: {len(fechas)}")
            if fechas:
                print(f"   ✓ Rango: {fechas[-1]} → {fechas[0]}")
            
            rows = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
            print(f"   ✓ Filas: {len(rows)}")
            
            # Parse some info
            rit = re.search(r'Rit[:\s]*</?\w*>?\s*([^<\n]+)', detail_html, re.I)
            if rit:
                print(f"   ✓ Rit: {rit.group(1).strip()}")
            
            trib = re.search(r'Tribunal[:\s]*</?\w*>?\s*([^<\n]+)', detail_html, re.I)
            if trib:
                print(f"   ✓ Tribunal: {trib.group(1).strip()}")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ Ver screenshots/detalle_civil.html")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Token: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
