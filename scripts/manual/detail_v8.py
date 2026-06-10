#!/usr/bin/env python3
"""
Test: v8 - Explorar estructura del sitio después de login.
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
    print('  v8 - Explorar estructura')
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
        
        # Login via API
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
        print("   ✓ Login ejecutado")
        
        # 2. Ver a dónde redirige el sitio después del login
        print()
        print("2. Siguiendo el redirect del login script...")
        
        # The login response contained localStorage setup - let's navigate to the main page
        await page.goto(f"{base_url}/index.php", wait_until="networkidle")
        await asyncio.sleep(2)
        
        print(f"   URL: {page.url}")
        
        # Check content
        content = await page.content()
        
        # Save for debug
        with open("screenshots/after_login.html", "w") as f:
            f.write(content)
        
        # Look for menu links
        links = await page.evaluate("""
            () => {
                const anchors = document.querySelectorAll('a[href]');
                const hrefs = [];
                for (const a of anchors) {
                    if (a.href.includes('misCausas') || a.href.includes('causas') || a.href.includes('consulta')) {
                        hrefs.push({ text: a.innerText.trim(), href: a.href });
                    }
                }
                return hrefs;
            }
        """)
        
        print(f"   Links encontrados: {len(links)}")
        for link in links[:10]:
            print(f"      - {link['text']}: {link['href']}")
        
        # 3. Try different URLs
        print()
        print("3. Probando URLs...")
        
        urls_to_try = [
            f"{base_url}/ADIR_871/misCausas/index.php",
            f"{base_url}/ADIR_871/misCausas.php",
            f"{base_url}/misCausas.php",
            f"{base_url}/index.php?opcion=misCausas",
        ]
        
        for url in urls_to_try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            status = resp.status if resp else "None"
            has_content = len(await page.content()) > 1000
            print(f"   {url}: {status} ({'OK' if has_content else 'Empty'})")
            
            if has_content and status == 200:
                # Found it!
                print("   ✓ Encontrado!")
                break
        
        # 4. Alternative: Use the API directly since we have the session
        print()
        print("4. Probando API directamente...")
        
        # Try civil search from root
        civil_html = await page.evaluate(f"""
            async () => {{
                const form = new URLSearchParams();
                form.append('rutMisCauCiv', '{rut_clean}');
                form.append('dvMisCauCiv', '{dv}');
                form.append('tipoMisCauCiv', '0');
                form.append('tipCausaMisCauCiv[]', 'M');
                
                // Try different paths
                const paths = [
                    '/misCausas/civil/consultaMisCausasCivil.php',
                    '/ADIR_871/misCausas/civil/consultaMisCausasCivil.php',
                ];
                
                for (const path of paths) {{
                    try {{
                        const resp = await fetch('{base_url}' + path, {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: form.toString(),
                            credentials: 'include'
                        }});
                        
                        if (resp.status === 200) {{
                            const text = await resp.text();
                            if (text.length > 100) {{
                                return {{ path: path, html: text }};
                            }}
                        }}
                    }} catch (e) {{}}
                }}
                return {{ error: 'No path worked' }};
            }}
        """)
        
        if 'error' in civil_html:
            print(f"   ✗ {civil_html['error']}")
        else:
            print(f"   ✓ Encontrado en: {civil_html.get('path')}")
            html = civil_html.get('html', '')
            
            tokens = re.findall(r"detalleMisCausaCivil\('([^']+)'\)", html)
            rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', html)
            print(f"   Causas: {len(rols)}")
            
            if tokens:
                # Try detail
                first_token = tokens[0]
                first_rol = rols[0]
                
                print()
                print(f"5. Detalle de {first_rol}...")
                
                # Use same base path
                base_path = civil_html.get('path', '').replace('consultaMisCausasCivil.php', '')
                
                detail = await page.evaluate(f"""
                    async () => {{
                        const form = new URLSearchParams();
                        form.append('dtaCausa', '{first_token}');
                        form.append('token', 'df32271e9cdca2704ff289941058a253');
                        
                        const resp = await fetch('{base_url}{base_path}modal/misCausasCivil.php', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: form.toString(),
                            credentials: 'include'
                        }});
                        
                        return {{ status: resp.status, html: await resp.text() }};
                    }}
                """)
                
                print(f"   Status: {detail.get('status')}")
                detail_html = detail.get('html', '')
                print(f"   Tamaño: {len(detail_html)}")
                
                with open("screenshots/detalle_civil.html", "w") as f:
                    f.write(detail_html)
                
                if len(detail_html) > 100:
                    fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
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
