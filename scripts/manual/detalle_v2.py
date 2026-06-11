#!/usr/bin/env python3
"""
Obtener detalle de causa usando requests directos con las cookies.
"""

import asyncio
import re
import sys
import httpx

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_detalle_causa(captcha_token: str, rol: str = "1234", year: str = "2024"):
    rut_clean = '16021492'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  DETALLE DE CAUSA CIVIL (v2 - httpx)')
    print('=' * 60)
    print(f"  Buscando: C-{rol}-{year}")
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Get JWT token from login page
        print("1. Obteniendo JWT token...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        print(f"   JWT: {jwt_token[:30]}...")
        
        # 2. Login via fetch to establish session
        print("2. Haciendo login...")
        await page.evaluate(f"""
            async () => {{
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
            }}
        """)
        print("   ✓ Login completado")
        
        # 3. Get cookies
        cookies = await context.cookies()
        cookie_dict = {c['name']: c['value'] for c in cookies}
        print(f"   Cookies: {list(cookie_dict.keys())}")
        
        await browser.close()
    
    # 4. Now use httpx with the cookies
    print()
    print("3. Buscando causa con httpx...")
    
    async with httpx.AsyncClient(cookies=cookie_dict, follow_redirects=True) as client:
        # Set proper headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Origin': base_url,
            'Referer': f'{base_url}/indexN.php',
            'X-Requested-With': 'XMLHttpRequest',
        }
        
        # Search for case
        search_data = {
            'competencia': '3',
            'conCorte': '0',
            'conTribunal': '0',
            'conTipoBus': '1',
            'conTipoCausa': 'C',
            'conRolCausa': rol,
            'conEraCausa': year,
            'conCaratulado': '',
        }
        
        search_resp = await client.post(
            f'{base_url}/ADIR_871/civil/consultaRitCivil.php',
            data=search_data,
            headers=headers
        )
        search_html = search_resp.text
        print(f"   Búsqueda: {len(search_html)} chars, status: {search_resp.status_code}")
        
        # Extract case tokens
        tokens = re.findall(r"detalleCausaCivil\('([^']+)'\)", search_html)
        print(f"   Causas: {len(tokens)}")
        
        if not tokens:
            print("   ⚠️ No se encontraron causas")
            return
        
        # Get first case detail
        print()
        print("4. Obteniendo detalle...")
        
        detail_data = {
            'dtaCausa': tokens[0],
            'token': '917cfa057160fbb6de2eb86da2348e42',
            'tokenCaptcha': 'CONTENEDORSII',
        }
        
        detail_resp = await client.post(
            f'{base_url}/ADIR_871/civil/modal/causaCivil.php',
            data=detail_data,
            headers=headers
        )
        detail_html = detail_resp.text
        print(f"   Detalle: {len(detail_html)} chars, status: {detail_resp.status_code}")
        
        with open("screenshots/detalle_causa.html", "w") as f:
            f.write(detail_html)
        
        # 5. Analyze
        print()
        print("5. Analizando...")
        
        if detail_resp.status_code != 200:
            print(f"   ⚠️ Error HTTP {detail_resp.status_code}")
            print(f"   Response: {detail_html[:500]}")
        elif len(detail_html) < 100:
            print(f"   ⚠️ Respuesta muy corta: {detail_html}")
        else:
            # Tables
            tables = len(re.findall(r'<table[^>]*>', detail_html, re.IGNORECASE))
            print(f"   Tablas: {tables}")
            
            # Keywords
            keywords = ['movimiento', 'tramitación', 'cuaderno', 'resolución', 'escrito']
            found = [k for k in keywords if k in detail_html.lower()]
            if found:
                print(f"   Keywords: {found}")
            
            # Onclick handlers  
            handlers = re.findall(r'onclick="([^"]+)"', detail_html)
            if handlers:
                print(f"   Handlers ({len(handlers)}):")
                for h in handlers[:5]:
                    print(f"     - {h[:80]}...")
            
            # AJAX URLs
            urls = re.findall(r"url\s*:\s*['\"]([^'\"]+)['\"]", detail_html)
            if urls:
                print(f"   AJAX URLs:")
                for u in urls[:5]:
                    print(f"     - {u}")
    
    print()
    print("=" * 60)
    print("  ✅ COMPLETADO")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detalle_causa(token))
