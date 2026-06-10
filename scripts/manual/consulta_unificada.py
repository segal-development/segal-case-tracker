#!/usr/bin/env python3
"""
Consulta Unificada - Buscar causa civil por ROL.
Versión que evita navegaciones problemáticas.
"""

import asyncio
import re
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def consulta_unificada(captcha_token: str, rol: str = "1234", year: str = "2024"):
    rut_clean = '16021492'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  CONSULTA UNIFICADA - CAUSA CIVIL')
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
        
        # 1. Login - stay on login page
        print("1. Haciendo login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
        # Do login and immediately fetch causas
        print("2. Autenticando y buscando causa...")
        
        result = await page.evaluate(f"""
            async () => {{
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
                
                const loginStatus = loginResp.status;
                
                // 2. Search civil case
                const searchForm = new URLSearchParams();
                searchForm.append('competencia', '3');  // Civil
                searchForm.append('conCorte', '0');     // Todas
                searchForm.append('conTribunal', '0');  // Todos
                searchForm.append('conTipoBus', '1');   // Por RIT
                searchForm.append('conTipoCausa', 'C'); // Tipo C
                searchForm.append('conRolCausa', '{rol}');
                searchForm.append('conEraCausa', '{year}');
                searchForm.append('conCaratulado', '');
                
                const searchResp = await fetch('{base_url}/ADIR_871/civil/consultaRitCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: searchForm.toString(),
                    credentials: 'include'
                }});
                
                const searchHtml = await searchResp.text();
                
                return {{
                    loginStatus: loginStatus,
                    searchStatus: searchResp.status,
                    searchHtml: searchHtml
                }};
            }}
        """)
        
        print(f"   Login status: {result['loginStatus']}")
        print(f"   Search status: {result['searchStatus']}")
        
        search_html = result['searchHtml']
        with open("screenshots/consulta_civil_result.html", "w") as f:
            f.write(search_html)
        print(f"   ✓ Respuesta guardada ({len(search_html)} chars)")
        
        # 3. Analyze
        print()
        print("3. Analizando resultados...")
        
        if '404' in search_html or 'Not Found' in search_html:
            print("   ⚠️  Error 404")
            print(f"   {search_html[:200]}")
        elif 'no existen' in search_html.lower():
            print(f"   Sin resultados para C-{rol}-{year}")
        else:
            tr_count = len(re.findall(r'<tr[^>]*>', search_html, re.IGNORECASE))
            print(f"   Filas encontradas: {tr_count}")
            
            onclick_pattern = r'onclick="([^"]*)"'
            onclicks = re.findall(onclick_pattern, search_html)
            print(f"   Onclick handlers: {len(onclicks)}")
            
            if onclicks:
                for o in onclicks[:3]:
                    print(f"     - {o[:120]}...")
            
            # Extract ROLs
            rol_pattern = r'[CVEAFI]-\d+-\d{4}'
            rols = re.findall(rol_pattern, search_html)
            if rols:
                print(f"   ROLs: {rols[:10]}")
        
        # 4. Try C-1-2024 (usually exists in every court)
        print()
        print("4. Probando C-1-2024...")
        
        result2 = await page.evaluate(f"""
            async () => {{
                const searchForm = new URLSearchParams();
                searchForm.append('competencia', '3');
                searchForm.append('conCorte', '0');
                searchForm.append('conTribunal', '0');
                searchForm.append('conTipoBus', '1');
                searchForm.append('conTipoCausa', 'C');
                searchForm.append('conRolCausa', '1');
                searchForm.append('conEraCausa', '2024');
                searchForm.append('conCaratulado', '');
                
                const searchResp = await fetch('{base_url}/ADIR_871/civil/consultaRitCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: searchForm.toString(),
                    credentials: 'include'
                }});
                
                return await searchResp.text();
            }}
        """)
        
        with open("screenshots/consulta_civil_c1_2024.html", "w") as f:
            f.write(result2)
        
        tr_count = len(re.findall(r'<tr[^>]*>', result2, re.IGNORECASE))
        print(f"   Filas: {tr_count}")
        
        if tr_count > 1:  # More than header row
            onclicks = re.findall(r'onclick="([^"]*)"', result2)
            if onclicks:
                print(f"   Handler: {onclicks[0][:150]}...")
        elif 'no existen' in result2.lower():
            print("   Sin resultados")
        else:
            print(f"   Preview: {result2[:300]}")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ COMPLETADO")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(consulta_unificada(token))
