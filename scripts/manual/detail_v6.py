#!/usr/bin/env python3
"""
Test: Obtener detalle de causa Civil - v6.
Usa API login + navega a misCausas + usa relative URL para modal.
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
    print('  DETALLE CAUSA CIVIL v6')
    print('=' * 60)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        # 1. Go to home, get JWT, login via API
        print("1. Login...")
        await page.goto(f"{base_url}/home/index.php", wait_until="networkidle")
        
        jwt_token = await page.evaluate("""
            () => document.querySelector('input[name*="7f9d8a"]')?.value || ''
        """)
        
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
                
                return {{ status: resp.status, ok: resp.ok }};
            }}
        """)
        print(f"   Login: {login_result}")
        
        # 2. Navigate to misCausas page (to establish page context)
        print()
        print("2. Navegando a Mis Causas...")
        await page.goto(f"{base_url}/misCausas/index.php", wait_until="networkidle")
        await asyncio.sleep(3)
        
        # Verify we're logged in
        page_html = await page.content()
        logged_in = '16021492' in page_html or 'Carla' in page_html
        print(f"   Sesión verificada: {logged_in}")
        
        if not logged_in:
            print("   ⚠️ No hay sesión - guardando página")
            with open("screenshots/no_session.html", "w") as f:
                f.write(page_html)
            await browser.close()
            return
        
        # 3. Get civil cases - using relative URL since we're on misCausas/index.php
        print()
        print("3. Cargando causas civiles...")
        
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
        
        if not tokens:
            print("   Sin tokens")
            await browser.close()
            return
        
        first_token = tokens[0]
        first_rol = rols[0]
        
        # 4. Get detail - using RELATIVE URL
        print()
        print(f"4. Obteniendo detalle de {first_rol}...")
        print(f"   Token (primeros 50): {first_token[:50]}...")
        
        # The key: use relative URL from misCausas context
        detail_result = await page.evaluate(f"""
            async () => {{
                const form = new URLSearchParams();
                form.append('dtaCausa', '{first_token}');
                form.append('token', 'df32271e9cdca2704ff289941058a253');
                
                const resp = await fetch('civil/modal/misCausasCivil.php', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/x-www-form-urlencoded'
                    }},
                    body: form.toString(),
                    credentials: 'include'
                }});
                
                const text = await resp.text();
                return {{
                    status: resp.status,
                    statusText: resp.statusText,
                    length: text.length,
                    html: text
                }};
            }}
        """)
        
        print(f"   Status: {detail_result.get('status')} {detail_result.get('statusText')}")
        print(f"   Tamaño: {detail_result.get('length')} chars")
        
        detail_html = detail_result.get('html', '')
        
        # Save
        with open("screenshots/detalle_civil.html", "w") as f:
            f.write(detail_html)
        
        # 5. Analyze
        print()
        print("5. Analizando...")
        
        if len(detail_html) < 100:
            print(f"   ⚠️ Respuesta muy corta")
            print(f"   Contenido: {detail_html[:500]}")
        else:
            # Info básica
            rit_match = re.search(r'Rit[:\s]*</?\w*>?\s*([^<\n]+)', detail_html, re.IGNORECASE)
            if rit_match:
                print(f"   Rit: {rit_match.group(1).strip()}")
            
            tribunal_match = re.search(r'Tribunal[:\s]*</?\w*>?\s*([^<\n]+)', detail_html, re.IGNORECASE)
            if tribunal_match:
                print(f"   Tribunal: {tribunal_match.group(1).strip()}")
            
            # Fechas
            fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', detail_html)
            if fechas:
                print(f"   Fechas: {len(fechas)} (desde {fechas[-1]} hasta {fechas[0]})")
            
            # Filas
            rows = re.findall(r'<tr[^>]*>.*?</tr>', detail_html, re.DOTALL)
            print(f"   Filas HTML: {len(rows)}")
            
            # Tabs/secciones
            tabs = re.findall(r'>(Historial|Demandante|Demandado|Litigantes|Tramitación)<', detail_html, re.IGNORECASE)
            if tabs:
                print(f"   Secciones: {list(set(tabs))}")
        
        await browser.close()
        
        print()
        print("=" * 60)
        print("  ✅ Ver screenshots/detalle_civil.html")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Token: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(get_detail_civil(token))
