#!/usr/bin/env python3
"""
Test: v10 - Login con redirect natural y luego navegar.
El problema es que sessionN.php redirige internamente pero fetch no lo sigue.
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
    print('  v10 - Login con navegación natural')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Go to home and submit login form via form submit (not fetch)
        print("1. Cargando home...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        
        jwt_token = await page.evaluate("""
            () => document.querySelector('input[name*="7f9d8a"]')?.value || ''
        """)
        print(f"   JWT: {jwt_token[:30]}...")
        
        # 2. Login using form submission that follows redirect
        print()
        print("2. Login via form submit...")
        
        # Create and submit a form dynamically
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
        
        # Wait for navigation to complete
        await asyncio.sleep(5)
        
        print(f"   URL después de login: {page.url}")
        
        # 3. Check where we ended up
        content = await page.content()
        has_user = '16021492' in content or 'Carla' in content
        print(f"   Tiene usuario: {has_user}")
        
        # Check for menu/navigation
        has_menu = 'misCausas' in content.lower() or 'mis causas' in content.lower()
        print(f"   Tiene menú: {has_menu}")
        
        # 4. Navigate to Mis Causas if we're logged in
        if has_user or has_menu:
            print()
            print("3. Navegando a Mis Causas...")
            
            # Try clicking on the menu link if it exists
            links = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a'))
                    .filter(a => a.href.toLowerCase().includes('miscausas') || 
                                 a.innerText.toLowerCase().includes('mis causas'))
                    .map(a => ({ text: a.innerText, href: a.href }))
            """)
            
            if links:
                print(f"   Encontrados {len(links)} links de Mis Causas")
                # Click first link
                await page.click(f'a[href*="misCausas"], a[href*="miscausas"]')
                await asyncio.sleep(3)
            else:
                # Navigate directly
                await page.goto(f"{base_url}/misCausas.php", wait_until="networkidle")
                await asyncio.sleep(3)
            
            print(f"   URL: {page.url}")
            
            # Check jQuery
            has_jquery = await page.evaluate("typeof jQuery !== 'undefined'")
            print(f"   jQuery: {has_jquery}")
            
            # 5. Load civil cases
            print()
            print("4. Cargando causas civiles...")
            
            # Try to find the Civil tab and click it
            civil_tab = await page.query_selector('#misCausas-civ-tab')
            if civil_tab:
                await civil_tab.click()
                await asyncio.sleep(3)
                print("   ✓ Tab Civil clickeado")
            
            # Get the data
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
                                error: function(xhr) {{ resolve('ERROR:' + xhr.status); }}
                            }});
                        }});
                    }}
                """)
            else:
                # Use fetch as fallback
                civil_html = await page.evaluate(f"""
                    async () => {{
                        const form = new URLSearchParams();
                        form.append('rutMisCauCiv', '{rut_clean}');
                        form.append('dvMisCauCiv', '{dv}');
                        form.append('tipoMisCauCiv', '0');
                        form.append('tipCausaMisCauCiv[]', 'M');
                        
                        const resp = await fetch('{base_url}/misCausas/civil/consultaMisCausasCivil.php', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: form.toString(),
                            credentials: 'include'
                        }});
                        
                        return await resp.text();
                    }}
                """)
            
            tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
            rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
            print(f"   Causas: {len(rols)}")
            
            if tokens:
                first_token = tokens[0]
                first_rol = rols[0]
                
                # 6. Get detail
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
                                    error: function(xhr) {{ resolve('ERROR:' + xhr.status); }}
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
                            
                            const resp = await fetch('{base_url}/misCausas/civil/modal/misCausasCivil.php', {{
                                method: 'POST',
                                headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                                body: form.toString(),
                                credentials: 'include'
                            }});
                            
                            return {{ status: resp.status, html: await resp.text() }};
                        }}
                    """)
                    detail_html = f"STATUS:{detail_result.get('status')}|{detail_result.get('html', '')}"
                
                print(f"   Respuesta: {len(detail_html)} chars")
                
                if 'ERROR' in detail_html or len(detail_html) < 100:
                    print(f"   ⚠️ {detail_html[:300]}")
                else:
                    with open("screenshots/detalle_civil.html", "w") as f:
                        f.write(detail_html)
                    
                    fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
                    print(f"   ✓ Fechas: {len(fechas)}")
        else:
            print("   ⚠️ No se pudo establecer sesión")
            with open("screenshots/after_login_v10.html", "w") as f:
                f.write(content)
        
        await browser.close()
        
        print()
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Token: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
