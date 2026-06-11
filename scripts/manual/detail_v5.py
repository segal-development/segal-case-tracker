#!/usr/bin/env python3
"""
Test: Obtener detalle de causa Civil - v5 con sesión de misCausas establecida.
El problema es que el endpoint modal requiere estar en la página correcta.
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
    print('  DETALLE CAUSA CIVIL v5 - Con interceptor')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # Capture network requests to see what the page does
        detail_responses = []
        
        async def handle_response(response):
            if 'modal' in response.url and 'Civil' in response.url:
                try:
                    body = await response.text()
                    detail_responses.append({
                        'url': response.url,
                        'status': response.status,
                        'body': body
                    })
                except:
                    pass
        
        page.on('response', handle_response)
        
        # 1. Go to home and login
        print("1. Haciendo login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        # Fill login form
        await page.fill('input[name="rut"]', rut_clean)
        await page.fill('input[name="password"]', password)
        
        # Inject captcha token
        await page.evaluate(f"""
            () => {{
                const input = document.querySelector('textarea[name*="g-recaptcha-response"]');
                if (input) input.value = '{captcha_token}';
                
                // Also try hidden input
                const hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = 'g-recaptcha-response-seg-clave_hn';
                hidden.value = '{captcha_token}';
                document.querySelector('form')?.appendChild(hidden);
            }}
        """)
        
        # Click login button
        await page.click('button[type="submit"], input[type="submit"], .btn-primary')
        await asyncio.sleep(3)
        
        # Check if logged in
        current_url = page.url
        print(f"   URL actual: {current_url}")
        
        # 2. Navigate to Mis Causas
        print()
        print("2. Navegando a Mis Causas...")
        await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
        await asyncio.sleep(3)
        
        # Check session status
        session_check = await page.evaluate("""
            () => {
                // Check if there's user info on the page
                const userInfo = document.body.innerText;
                return {
                    hasSession: userInfo.includes('16021492') || userInfo.includes('Carla'),
                    pageTitle: document.title,
                    bodyPreview: userInfo.substring(0, 500)
                };
            }
        """)
        print(f"   Sesión activa: {session_check.get('hasSession')}")
        print(f"   Título: {session_check.get('pageTitle')}")
        
        # 3. Wait for page to fully load with jQuery
        print()
        print("3. Esperando carga completa...")
        await asyncio.sleep(5)
        
        # Check jQuery
        has_jquery = await page.evaluate("() => typeof jQuery !== 'undefined'")
        print(f"   jQuery: {has_jquery}")
        
        if not has_jquery:
            print("   ⚠️ jQuery no cargado - la página puede no estar autenticada")
            await page.screenshot(path="screenshots/no_session.png")
            
            # Try direct API login
            print()
            print("4. Intentando login directo via API...")
            
            jwt_token = await page.evaluate("""
                () => {
                    const input = document.querySelector('input[name*="7f9d8a"]');
                    return input ? input.value : '';
                }
            """)
            
            if jwt_token:
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
                        
                        return {{ status: resp.status, url: resp.url }};
                    }}
                """)
                print(f"   Login API: {login_result}")
                
                # Reload mis causas
                await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
                await asyncio.sleep(5)
        
        # 5. Now try to load civil tab
        print()
        print("5. Cargando tab Civil...")
        
        # Click the civil tab
        try:
            await page.click('#misCausas-civ-tab', timeout=5000)
            await asyncio.sleep(3)
        except:
            print("   Tab no encontrado - intentando cargar directamente")
        
        # Get civil cases via jQuery
        civil_html = await page.evaluate(f"""
            async () => {{
                if (typeof jQuery === 'undefined') {{
                    // Use fetch fallback
                    const civForm = new URLSearchParams();
                    civForm.append('rutMisCauCiv', '{rut_clean}');
                    civForm.append('dvMisCauCiv', '{dv}');
                    civForm.append('tipoMisCauCiv', '0');
                    civForm.append('tipCausaMisCauCiv[]', 'M');
                    
                    const resp = await fetch('civil/consultaMisCausasCivil.php', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: civForm.toString(),
                        credentials: 'include'
                    }});
                    return await resp.text();
                }} else {{
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
                            error: function() {{ resolve('JQUERY_ERROR'); }}
                        }});
                    }});
                }}
            }}
        """)
        
        tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", civil_html)
        rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', civil_html)
        print(f"   Causas encontradas: {len(rols)}")
        
        if not tokens:
            print("   Sin tokens - guardando HTML para debug")
            with open("screenshots/civil_list_debug.html", "w") as f:
                f.write(civil_html)
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0]
        
        # 6. Get detail using jQuery from page context
        print()
        print(f"6. Obteniendo detalle de {first_rol}...")
        
        detail_html = await page.evaluate(f"""
            async () => {{
                if (typeof jQuery === 'undefined') {{
                    const detForm = new URLSearchParams();
                    detForm.append('dtaCausa', '{first_token}');
                    detForm.append('token', 'df32271e9cdca2704ff289941058a253');
                    
                    const resp = await fetch('civil/modal/misCausasCivil.php', {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'X-Requested-With': 'XMLHttpRequest'
                        }},
                        body: detForm.toString(),
                        credentials: 'include'
                    }});
                    return 'FETCH_STATUS:' + resp.status + '|' + await resp.text();
                }} else {{
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
                            error: function(xhr) {{ 
                                resolve('JQUERY_ERROR:' + xhr.status + '|' + xhr.responseText); 
                            }}
                        }});
                    }});
                }}
            }}
        """)
        
        print(f"   Respuesta: {len(detail_html)} chars")
        
        # Save
        with open("screenshots/detalle_civil.html", "w") as f:
            f.write(detail_html)
        
        # Check intercepted responses
        if detail_responses:
            print(f"   Respuestas interceptadas: {len(detail_responses)}")
            for r in detail_responses:
                print(f"     - {r['url']}: {r['status']} ({len(r['body'])} chars)")
        
        # Analyze
        print()
        print("7. Analizando...")
        
        if 'ERROR' in detail_html or len(detail_html) < 100:
            print(f"   ⚠️ {detail_html[:300]}")
        else:
            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
            print(f"   Fechas encontradas: {len(fechas)}")
            if fechas:
                print(f"   Rango: {fechas[-1]} → {fechas[0]}")
            
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
