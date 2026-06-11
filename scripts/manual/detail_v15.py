#!/usr/bin/env python3
"""
Test: v15 - Navegar desde indexN.php a misCausas via menú/link.
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
    print('  v15 - Detalle causa Civil')
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
        
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)
        print(f"   ✓ URL: {page.url}")
        
        # 2. Look for Mis Causas link in the main page
        print()
        print("2. Buscando link a Mis Causas...")
        
        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a'))
                .filter(a => a.innerText.toLowerCase().includes('mis causas') || 
                             a.href.toLowerCase().includes('miscausas'))
                .map(a => ({ text: a.innerText.trim(), href: a.href }))
        """)
        
        print(f"   Links: {links}")
        
        # 3. Click on Mis Causas or navigate directly to the iframe content
        print()
        print("3. Navegando a Mis Causas...")
        
        # The main page might use iframes - check
        iframes = await page.evaluate("""
            () => Array.from(document.querySelectorAll('iframe'))
                .map(f => ({ name: f.name, src: f.src }))
        """)
        print(f"   Iframes: {iframes}")
        
        # Try to find the actual content frame
        frames = page.frames
        print(f"   Frames en página: {len(frames)}")
        
        for frame in frames:
            frame_url = frame.url
            if 'misCausas' in frame_url.lower():
                print(f"   ✓ Encontrado frame misCausas: {frame_url}")
                
                # Wait for jQuery in this frame
                try:
                    await frame.wait_for_function("typeof jQuery !== 'undefined'", timeout=10000)
                    has_jquery = True
                except:
                    has_jquery = False
                
                print(f"   jQuery en frame: {has_jquery}")
                
                if has_jquery:
                    # Use this frame for AJAX calls
                    civil_html = await frame.evaluate(f"""
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
                                    error: function(xhr) {{ resolve('ERROR:' + xhr.status); }}
                                }});
                            }});
                        }}
                    """)
                    
                    tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
                    rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
                    print(f"   Causas: {len(rols)}")
                    
                    if tokens:
                        first_token = tokens[0]
                        first_rol = rols[0]
                        
                        print()
                        print(f"4. Detalle de {first_rol}...")
                        
                        detail_html = await frame.evaluate(f"""
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
                                        error: function(xhr) {{ resolve('ERROR:' + xhr.status); }}
                                    }});
                                }});
                            }}
                        """)
                        
                        print(f"   Tamaño: {len(detail_html)} chars")
                        
                        if 'ERROR' not in detail_html and len(detail_html) > 100:
                            with open("screenshots/detalle_civil.html", "w") as f:
                                f.write(detail_html)
                            
                            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
                            print(f"   ✓ Fechas: {len(fechas)}")
                        else:
                            print(f"   ⚠️ {detail_html[:200]}")
                
                break
        else:
            # No misCausas frame found - try navigating to the menu option
            print("   No se encontró frame misCausas")
            
            # Check the page content for menu structure
            content = await page.content()
            with open("screenshots/indexN_debug.html", "w") as f:
                f.write(content)
            
            # Look for the navigation structure
            print("   Guardado indexN_debug.html para análisis")
        
        await browser.close()
        
        print()
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Token: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
