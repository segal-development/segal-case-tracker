#!/usr/bin/env python3
"""
Mis Causas CIVIL - CORREGIDO.
El problema era que tipCausaMisCauCiv[] y estadoCausaMisCauCiv[] son ARRAYS.
"""

import asyncio
import re
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def get_mis_causas_civil(captcha_token: str):
    rut_clean = '16021492'
    dv = '9'
    password = 'Gruposegal2026+'
    base_url = 'https://oficinajudicialvirtual.pjud.cl'
    
    print('=' * 60)
    print('  MIS CAUSAS - CIVIL (CORREGIDO)')
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
        
        # Login + search with CORRECT array notation
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
                
                // Civil with ARRAY notation for tipCausaMisCauCiv[] and estadoCausaMisCauCiv[]
                const civForm = new URLSearchParams();
                civForm.append('rutMisCauCiv', '{rut_clean}');
                civForm.append('dvMisCauCiv', '{dv}');
                civForm.append('tipoMisCauCiv', '0');           // Tipo RIT (0=todos)
                civForm.append('rolMisCauCiv', '');
                civForm.append('anhoMisCauCiv', '');
                civForm.append('tipCausaMisCauCiv[]', 'M');     // ARRAY! Causas Propias
                // No enviar estadoCausaMisCauCiv[] = sin filtro de estado
                civForm.append('nombreMisCauCiv', '');
                civForm.append('apePatMisCauCiv', '');
                civForm.append('apeMatMisCauCiv', '');
                
                const civResp = await fetch('{base_url}/misCausas/civil/consultaMisCausasCivil.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: civForm.toString(),
                    credentials: 'include'
                }});
                
                return await civResp.text();
            }}
        """)
        
        print("   ✓ Login y consulta completados")
        print()
        
        # Save
        with open("screenshots/mis_causas_civil_fixed.html", "w") as f:
            f.write(result)
        
        # Analyze
        print("2. Analizando resultados...")
        print(f"   Tamaño respuesta: {len(result)} chars")
        
        # Check for "no existen"
        if 'no existen' in result.lower():
            print("   ⚠️ Sin causas (mismo problema)")
            print()
            print("   Primeros 500 chars:")
            print(f"   {result[:500]}")
        else:
            # Count rows
            rows = re.findall(r'<tr[^>]*>.*?</tr>', result, re.DOTALL | re.IGNORECASE)
            print(f"   Filas encontradas: {len(rows)}")
            
            # Extract tokens
            tokens = re.findall(r"historiaCausaCuaderno\('([^']+)'\)", result)
            print(f"   Tokens de causas: {len(tokens)}")
            
            # Extract ROLs
            rols = re.findall(r'>([CVEAFI]-\d+-\d{4})<', result)
            print(f"   ROLs: {len(rols)}")
            if rols[:5]:
                print(f"   Primeros 5: {rols[:5]}")
            
            if tokens:
                # Get detail of first case
                print()
                print("3. Obteniendo detalle de primera causa...")
                
                first_token = tokens[0]
                
                detail = await page.evaluate(f"""
                    async () => {{
                        const detForm = new URLSearchParams();
                        detForm.append('dtaCausa', '{first_token}');
                        detForm.append('token', 'df32271e9cdca2704ff289941058a253');
                        
                        const detResp = await fetch('{base_url}/misCausas/civil/modal/misCausasCivil.php', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: detForm.toString(),
                            credentials: 'include'
                        }});
                        
                        return {{
                            status: detResp.status,
                            html: await detResp.text()
                        }};
                    }}
                """)
                
                detail_html = detail.get('html', '')
                with open("screenshots/detalle_civil.html", "w") as f:
                    f.write(detail_html)
                
                print(f"   Status: {detail.get('status')}")
                print(f"   Tamaño detalle: {len(detail_html)} chars")
                
                if len(detail_html) > 500:
                    # Look for movements
                    movs = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
                    print(f"   Filas en detalle: {len(movs)}")
        
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
    
    asyncio.run(get_mis_causas_civil(token))
