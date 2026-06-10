#!/usr/bin/env python3
"""
Probar Mis Causas de COBRANZA (no Civil).
Segal es firma de cobranzas - probablemente las causas están ahí.
"""

import asyncio
import re
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_mis_causas_cobranza(captcha_token: str):
    rut_clean = '16021492'
    dv = '9'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  MIS CAUSAS - COBRANZA')
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
        print("1. Haciendo login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
        # Login + search all in one
        result = await page.evaluate(f"""
            async () => {{
                // Login
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
                
                const results = {{}};
                
                // 1. Civil (para comparar)
                const civilForm = new URLSearchParams();
                civilForm.append('rutMisCauCiv', '{rut_clean}');
                civilForm.append('dvMisCauCiv', '{dv}');
                civilForm.append('tipoMisCauCiv', '0');
                civilForm.append('rolMisCauCiv', '');
                civilForm.append('anhoMisCauCiv', '');
                civilForm.append('tipCausaMisCauCiv', 'M');
                civilForm.append('estadoCausaMisCauCiv', '0');  // TODAS, no solo activas
                civilForm.append('nombreMisCauCiv', '');
                civilForm.append('apePatMisCauCiv', '');
                civilForm.append('apeMatMisCauCiv', '');
                
                const civilResp = await fetch('{base_url}/misCausas/civil/consultaMisCausasCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: civilForm.toString(),
                    credentials: 'include'
                }});
                results.civil = await civilResp.text();
                
                // 2. Cobranza
                const cobForm = new URLSearchParams();
                cobForm.append('rutMisCauCob', '{rut_clean}');
                cobForm.append('dvMisCauCob', '{dv}');
                cobForm.append('tipoMisCauCob', '0');
                cobForm.append('rolMisCauCob', '');
                cobForm.append('anhoMisCauCob', '');
                cobForm.append('tipCausaMisCauCob[]', 'M');  // Array
                cobForm.append('estadoCausaMisCauCob', '0');  // TODAS
                
                const cobResp = await fetch('{base_url}/misCausas/cobranza/consultaMisCausasCobranza.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: cobForm.toString(),
                    credentials: 'include'
                }});
                results.cobranza = await cobResp.text();
                
                // 3. Laboral
                const labForm = new URLSearchParams();
                labForm.append('rutMisCauLab', '{rut_clean}');
                labForm.append('dvMisCauLab', '{dv}');
                labForm.append('tipoMisCaulab', '0');
                labForm.append('rolMisCauLab', '');
                labForm.append('anhoMisCauLab', '');
                labForm.append('tipCausaMisCauLab', 'M');
                labForm.append('estadoCausaMisCauLab', '0');
                
                const labResp = await fetch('{base_url}/misCausas/laboral/consultaMisCausasLaboral.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: labForm.toString(),
                    credentials: 'include'
                }});
                results.laboral = await labResp.text();
                
                // 4. Familia
                const famForm = new URLSearchParams();
                famForm.append('rutMisCauFam', '{rut_clean}');
                famForm.append('dvMisCauFam', '{dv}');
                famForm.append('tipoMisCauFam', '0');
                famForm.append('rolMisCauFam', '');
                famForm.append('anhoMisCauFam', '');
                famForm.append('tipCausaMisCauFam', 'M');
                famForm.append('estadoCausaMisCauFam', '0');
                
                const famResp = await fetch('{base_url}/misCausas/familia/consultaMisCausasFamilia.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: famForm.toString(),
                    credentials: 'include'
                }});
                results.familia = await famResp.text();
                
                return results;
            }}
        """)
        
        print("   ✓ Login y consultas completadas")
        print()
        
        # Analyze results
        competencias = ['civil', 'cobranza', 'laboral', 'familia']
        
        for comp in competencias:
            html = result.get(comp, '')
            
            # Save for analysis
            with open(f"screenshots/mis_causas_{comp}.html", "w") as f:
                f.write(html)
            
            # Count cases
            # Look for onclick handlers that indicate rows
            handlers = re.findall(r'onclick="[^"]*Cuaderno[^"]*\(', html, re.IGNORECASE)
            rows = len(re.findall(r'<tr[^>]*>', html, re.IGNORECASE))
            
            # Check for "no existen"
            no_data = 'no existen' in html.lower()
            is_404 = '404' in html or 'Not Found' in html
            
            status = "❌ 404" if is_404 else ("⚠️ Sin causas" if no_data else f"✅ {len(handlers)} causas")
            
            print(f"2. {comp.upper()}: {status} ({len(html)} chars)")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ COMPLETADO")
        print("=" * 60)
        print()
        print("Archivos guardados en screenshots/mis_causas_*.html")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_mis_causas_cobranza(token))
