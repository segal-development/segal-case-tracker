#!/usr/bin/env python3
"""
Test: v12 - Esperar jQuery y usar fallback fetch.
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
    print('  v12 - Detalle causa Civil')
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
        
        # 2. Go to misCausas and wait for jQuery
        print()
        print("2. Navegando a Mis Causas...")
        await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
        
        # Wait longer for scripts
        for i in range(10):
            has_jquery = await page.evaluate("typeof jQuery !== 'undefined'")
            if has_jquery:
                break
            await asyncio.sleep(1)
        
        print(f"   URL: {page.url}")
        print(f"   jQuery: {has_jquery}")
        
        # Check if page loaded correctly
        content = await page.content()
        if len(content) < 1000 or '404' in content:
            print("   ⚠️ Página no cargó correctamente")
            with open("screenshots/miscausas_debug.html", "w") as f:
                f.write(content)
            await browser.close()
            return
        
        # 3. Load civil cases - use fetch fallback if no jQuery
        print()
        print("3. Cargando causas civiles...")
        
        if has_jquery:
            civil_html = await page.evaluate(f"""
                () => {{
                    return new Promise((resolve) => {{
                        $.ajax({{
                            url: 'civil/consultaMisCausasCivil.php',
                            type: 'POST',
                            data: {{
                                rutMisCauCiv: '{rut_clean}',
                                dvMisCauCiv: '{dv}',
                                tipoMisCauCiv: '0',
                                'tipCausaMisCauCiv[]': 'M'
                            }},
                            success: function(data) {{ resolve(data); }},
                            error: function(xhr) {{ resolve('JQUERY_ERROR:' + xhr.status); }}
                        }});
                    }});
                }}
            """)
        else:
            # Use fetch
            civil_html = await page.evaluate(f"""
                async () => {{
                    const form = new URLSearchParams();
                    form.append('rutMisCauCiv', '{rut_clean}');
                    form.append('dvMisCauCiv', '{dv}');
                    form.append('tipoMisCauCiv', '0');
                    form.append('tipCausaMisCauCiv[]', 'M');
                    
                    const resp = await fetch('civil/consultaMisCausasCivil.php', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: form.toString(),
                        credentials: 'include'
                    }});
                    
                    if (!resp.ok) return 'FETCH_ERROR:' + resp.status;
                    return await resp.text();
                }}
            """)
        
        if 'ERROR' in civil_html:
            print(f"   ⚠️ {civil_html}")
            await browser.close()
            return
        
        tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
        rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
        print(f"   Causas: {len(rols)}")
        
        if not tokens:
            print("   Sin tokens - guardando HTML")
            with open("screenshots/civil_list_debug.html", "w") as f:
                f.write(civil_html)
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0]
        
        # 4. Get detail
        print()
        print(f"4. Detalle de {first_rol}...")
        
        if has_jquery:
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
                            success: function(data) {{ resolve(data); }},
                            error: function(xhr) {{ resolve('JQUERY_ERROR:' + xhr.status); }}
                        }});
                    }});
                }}
            """)
        else:
            detail_result = await page.evaluate(f"""
                async () => {{
                    const form = new URLSearchParams();
                    form.append('dtaCausa', '{first_token}');
                    form.append('token', 'df32271e9cdca2704ff289941058a253');
                    
                    const resp = await fetch('civil/modal/misCausasCivil.php', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: form.toString(),
                        credentials: 'include'
                    }});
                    
                    return {{ status: resp.status, html: await resp.text() }};
                }}
            """)
            detail_html = detail_result.get('html', '')
            if detail_result.get('status') != 200:
                detail_html = f"FETCH_ERROR:{detail_result.get('status')}"
        
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
