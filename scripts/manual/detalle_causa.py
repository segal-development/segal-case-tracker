#!/usr/bin/env python3
"""
Obtener detalle completo de una causa civil incluyendo movimientos.
TODO via fetch desde la página de login (evita navegaciones).
"""

import asyncio
import re
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_detalle_causa(captcha_token: str, rol: str = "1234", year: str = "2024"):
    rut_clean = '16021492'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  DETALLE DE CAUSA CIVIL')
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
        
        # 1. Go to login page and do everything from there
        print("1. Cargando página de login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
        # 2. Do login + search + detail ALL in one evaluate to avoid context issues
        print("2. Login + búsqueda + detalle (todo junto)...")
        
        result = await page.evaluate(f"""
            async () => {{
                const results = {{}};
                
                // 1. Login
                const loginForm = new URLSearchParams();
                loginForm.append('7f9d8a6356360386f79afd5691435626f470dee1', '{jwt_token}');
                loginForm.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                loginForm.append('rut', '{rut_clean}');
                loginForm.append('password', '{password}');
                
                const loginResp = await fetch('{base_url}/sessionN.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: loginForm.toString(),
                    credentials: 'include'
                }});
                results.loginStatus = loginResp.status;
                
                // 2. Search for cases
                const searchForm = new URLSearchParams();
                searchForm.append('competencia', '3');
                searchForm.append('conCorte', '0');
                searchForm.append('conTribunal', '0');
                searchForm.append('conTipoBus', '1');
                searchForm.append('conTipoCausa', 'C');
                searchForm.append('conRolCausa', '{rol}');
                searchForm.append('conEraCausa', '{year}');
                searchForm.append('conCaratulado', '');
                
                const searchResp = await fetch('{base_url}/ADIR_871/civil/consultaRitCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: searchForm.toString(),
                    credentials: 'include'
                }});
                results.searchHtml = await searchResp.text();
                
                // 3. Extract first case token
                const tokenMatch = results.searchHtml.match(/detalleCausaCivil\\('([^']+)'\\)/);
                if (!tokenMatch) {{
                    results.error = 'No case tokens found';
                    return results;
                }}
                results.caseToken = tokenMatch[1];
                
                // 4. Get case detail
                const detailForm = new URLSearchParams();
                detailForm.append('dtaCausa', results.caseToken);
                detailForm.append('token', '917cfa057160fbb6de2eb86da2348e42');
                detailForm.append('tokenCaptcha', 'CONTENEDORSII');
                
                const detailResp = await fetch('{base_url}/ADIR_871/civil/modal/causaCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: detailForm.toString(),
                    credentials: 'include'
                }});
                results.detailHtml = await detailResp.text();
                results.detailStatus = detailResp.status;
                
                return results;
            }}
        """)
        
        print(f"   Login: {result.get('loginStatus')}")
        
        if result.get('error'):
            print(f"   Error: {result['error']}")
            await browser.close()
            return
        
        search_html = result.get('searchHtml', '')
        print(f"   Búsqueda: {len(search_html)} chars")
        
        # Count cases
        case_count = len(re.findall(r"detalleCausaCivil\('", search_html))
        print(f"   Causas encontradas: {case_count}")
        
        detail_html = result.get('detailHtml', '')
        print(f"   Detalle: {len(detail_html)} chars, status: {result.get('detailStatus')}")
        
        # Save detail
        with open("screenshots/detalle_causa.html", "w") as f:
            f.write(detail_html)
        
        # 3. Analyze detail
        print()
        print("3. Analizando detalle...")
        
        if '404' in detail_html or 'Not Found' in detail_html:
            print("   ⚠️ Error 404")
        elif len(detail_html) < 100:
            print(f"   ⚠️ Respuesta muy corta: {detail_html}")
        else:
            # Look for important elements
            tables = len(re.findall(r'<table[^>]*>', detail_html, re.IGNORECASE))
            print(f"   Tablas: {tables}")
            
            # Look for sections/headers
            sections = re.findall(r'<h[1-6][^>]*>([^<]+)</h[1-6]>', detail_html, re.IGNORECASE)
            if sections:
                print(f"   Secciones: {sections[:10]}")
            
            # Look for tabs
            tabs = re.findall(r'(?:id|class)="([^"]*(?:tab|cuaderno|movimiento|tramit)[^"]*)"', detail_html, re.IGNORECASE)
            if tabs:
                print(f"   Tabs/elementos: {tabs[:10]}")
            
            # Look for onclick handlers
            onclick_handlers = re.findall(r'onclick="([^"]+)"', detail_html)
            if onclick_handlers:
                print(f"   Onclick handlers: {len(onclick_handlers)}")
                for h in onclick_handlers[:5]:
                    print(f"     - {h[:80]}...")
            
            # Look for AJAX endpoints
            ajax_urls = re.findall(r"url\s*:\s*['\"]([^'\"]+)['\"]", detail_html)
            if ajax_urls:
                print(f"   AJAX endpoints:")
                for url in ajax_urls[:10]:
                    print(f"     - {url}")
            
            # Check for specific keywords
            keywords = ['movimiento', 'tramitación', 'cuaderno', 'resolución', 'escrito', 'notificación']
            found_keywords = [k for k in keywords if k in detail_html.lower()]
            if found_keywords:
                print(f"   Keywords: {found_keywords}")
            
            # Extract first 1000 chars to see structure
            print()
            print("4. Preview del contenido:")
            preview = detail_html[:1500].replace('\n', ' ').replace('  ', ' ')
            print(f"   {preview[:500]}...")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ COMPLETADO")
        print("=" * 60)
        print()
        print("Archivo guardado: screenshots/detalle_causa.html")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detalle_causa(token))
