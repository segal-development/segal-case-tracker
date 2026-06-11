#!/usr/bin/env python3
"""
Test: Obtener detalle de causa Civil - v7.
Debug del login y la sesión.
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
    print('  DETALLE CAUSA CIVIL v7 - Debug sesión')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Go to home
        print("1. Cargando home...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        
        jwt_token = await page.evaluate("""
            () => document.querySelector('input[name*="7f9d8a"]')?.value || ''
        """)
        print(f"   JWT token: {jwt_token[:30]}..." if jwt_token else "   No JWT token!")
        
        # 2. Login and check response
        print()
        print("2. Login via API...")
        
        login_result = await page.evaluate(f"""
            async () => {{
                const form = new URLSearchParams();
                form.append('7f9d8a6356360386f79afd5691435626f470dee1', '{jwt_token}');
                form.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                form.append('rut', '{rut_clean}');
                form.append('password', '{password}');
                
                const resp = await fetch('{base_url}/sessionN.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: form.toString(),
                    credentials: 'include'
                }});
                
                const text = await resp.text();
                return {{
                    status: resp.status,
                    redirected: resp.redirected,
                    url: resp.url,
                    responseLength: text.length,
                    responsePreview: text.substring(0, 500)
                }};
            }}
        """)
        
        print(f"   Status: {login_result.get('status')}")
        print(f"   Redirected: {login_result.get('redirected')}")
        print(f"   URL: {login_result.get('url')}")
        print(f"   Response length: {login_result.get('responseLength')}")
        
        if login_result.get('responseLength', 0) > 0:
            print(f"   Response preview: {login_result.get('responsePreview', '')[:200]}")
        
        # 3. Check cookies
        print()
        print("3. Cookies después del login...")
        cookies = await context.cookies()
        for c in cookies:
            if 'pjud' in c.get('domain', '').lower():
                print(f"   {c['name']}: {c['value'][:30]}...")
        
        # 4. Try to access misCausas
        print()
        print("4. Accediendo a Mis Causas...")
        
        response = await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
        print(f"   Status: {response.status if response else 'None'}")
        print(f"   URL final: {page.url}")
        
        # 5. Check page content
        page_content = await page.content()
        has_user = '16021492' in page_content or 'Carla' in page_content
        has_login = 'login' in page_content.lower() or 'password' in page_content.lower()
        
        print(f"   Tiene info usuario: {has_user}")
        print(f"   Tiene form login: {has_login}")
        
        if not has_user:
            print()
            print("   ⚠️ No hay sesión - página redirigió a login")
            
            # Try to login ON the login page that we got redirected to
            print()
            print("5. Intentando login en página actual...")
            
            # Get new JWT token from current page
            new_jwt = await page.evaluate("""
                () => document.querySelector('input[name*="7f9d8a"]')?.value || ''
            """)
            
            if new_jwt:
                print(f"   Nuevo JWT: {new_jwt[:30]}...")
                
                # Do login again
                login2 = await page.evaluate(f"""
                    async () => {{
                        const form = new URLSearchParams();
                        form.append('7f9d8a6356360386f79afd5691435626f470dee1', '{new_jwt}');
                        form.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                        form.append('rut', '{rut_clean}');
                        form.append('password', '{password}');
                        
                        const resp = await fetch('{base_url}/sessionN.php', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: form.toString(),
                            credentials: 'include'
                        }});
                        
                        return {{ status: resp.status, ok: resp.ok }};
                    }}
                """)
                print(f"   Login 2: {login2}")
                
                # Navigate again
                await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
                await asyncio.sleep(2)
                
                page_content = await page.content()
                has_user = '16021492' in page_content or 'Carla' in page_content
                print(f"   Sesión ahora: {has_user}")
        
        if has_user:
            # 6. Get civil cases
            print()
            print("6. Cargando causas civiles...")
            
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
                    
                    return await resp.text();
                }}
            """)
            
            tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
            rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
            print(f"   Causas: {len(rols)}")
            
            if tokens:
                first_token = tokens[0]
                first_rol = rols[0]
                
                # 7. Get detail
                print()
                print(f"7. Detalle de {first_rol}...")
                
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
                        
                        return {{
                            status: resp.status,
                            html: await resp.text()
                        }};
                    }}
                """)
                
                print(f"   Status: {detail_result.get('status')}")
                detail_html = detail_result.get('html', '')
                print(f"   Tamaño: {len(detail_html)} chars")
                
                with open("screenshots/detalle_civil.html", "w") as f:
                    f.write(detail_html)
                
                if len(detail_html) > 100:
                    fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
                    print(f"   Fechas: {len(fechas)}")
                    
                    rows = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
                    print(f"   Filas: {len(rows)}")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ Completado")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Token: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
