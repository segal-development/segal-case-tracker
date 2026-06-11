#!/usr/bin/env python3
"""
Test: v17 - Usar URL absoluta para el modal.
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
    print('  v17 - Detalle causa Civil (URL absoluta)')
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
        
        # 2. Execute misCausas()
        print()
        print("2. Ejecutando misCausas()...")
        await page.evaluate("misCausas()")
        await asyncio.sleep(5)
        print("   ✓ Contenido cargado")
        
        # 3. Load civil cases
        print()
        print("3. Cargando causas civiles...")
        
        civil_html = await page.evaluate(f"""
            () => {{
                return new Promise((resolve) => {{
                    $.ajax({{
                        url: '{base_url}/misCausas/civil/consultaMisCausasCivil.php',
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
        print(f"   ✓ Causas: {len(rols)}")
        
        if not tokens:
            print("   Sin tokens")
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0]
        
        # 4. Get detail - try MULTIPLE URL patterns
        print()
        print(f"4. Detalle de {first_rol}...")
        print(f"   Token: {first_token[:50]}...")
        
        urls_to_try = [
            f'{base_url}/misCausas/civil/modal/misCausasCivil.php',
            'misCausas/civil/modal/misCausasCivil.php',
            '../misCausas/civil/modal/misCausasCivil.php',
            '/misCausas/civil/modal/misCausasCivil.php',
        ]
        
        for url in urls_to_try:
            print(f"   Probando: {url[:50]}...")
            
            detail_result = await page.evaluate(f"""
                () => {{
                    return new Promise((resolve) => {{
                        $.ajax({{
                            url: '{url}',
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
            
            status = detail_result.get('status')
            html = detail_result.get('html', '')
            
            print(f"      Status: {status}, Tamaño: {len(html)}")
            
            if status == 200 and len(html) > 100:
                print(f"   ✓ Funcionó con: {url}")
                
                with open("screenshots/detalle_civil.html", "w") as f:
                    f.write(html)
                
                fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', html)
                print(f"   ✓ Fechas: {len(fechas)}")
                
                break
        else:
            print("   ⚠️ Ninguna URL funcionó")
            
            # Try to call the actual function from the page
            print()
            print("5. Intentando llamar detalleMisCausaCivil() directamente...")
            
            # Check if function exists
            fn_exists = await page.evaluate("typeof detalleMisCausaCivil === 'function'")
            print(f"   Función existe: {fn_exists}")
            
            if fn_exists:
                # Call it and capture the modal content
                await page.evaluate(f"detalleMisCausaCivil('{first_token}')")
                await asyncio.sleep(3)
                
                # Get modal content
                modal_html = await page.evaluate("""
                    () => {
                        const modal = document.querySelector('#modalDetalleMisCauCivil');
                        return modal ? modal.innerHTML : '';
                    }
                """)
                
                print(f"   Modal content: {len(modal_html)} chars")
                
                if len(modal_html) > 100:
                    with open("screenshots/detalle_civil.html", "w") as f:
                        f.write(modal_html)
                    
                    fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', modal_html)
                    print(f"   ✓ Fechas: {len(fechas)}")
        
        await browser.close()
        
        print()
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Token: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
