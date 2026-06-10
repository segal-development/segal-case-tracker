#!/usr/bin/env python3
"""
Explorar causas asociadas a un RUT en PJUD.
Captura la estructura HTML real para ajustar selectores.
"""

import asyncio
import json
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from playwright.async_api import async_playwright


async def explore_causas(captcha_token: str):
    rut = '16021492-9'
    rut_clean = '16021492'
    password = 'Gruposegal2026+'
    
    print('=' * 60)
    print('  EXPLORAR CAUSAS EN PJUD')
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
        print("1. Navegando al PJUD...")
        await page.goto("https://oficinajudicialvirtual.pjud.cl/home/index.php", wait_until="networkidle")
        await asyncio.sleep(1)
        
        print("2. Haciendo login...")
        # Get JWT token
        jwt_token = await page.evaluate("""
            () => {
                const input = document.querySelector('input[name*="7f9d8a"]');
                return input ? input.value : '';
            }
        """)
        
        # Login POST
        login_result = await page.evaluate(f"""
            async () => {{
                const formData = new URLSearchParams();
                formData.append('7f9d8a6356360386f79afd5691435626f470dee1', '{jwt_token}');
                formData.append('g-recaptcha-response-seg-clave_hn', '{captcha_token}');
                formData.append('rut', '{rut_clean}');
                formData.append('password', '{password}');
                
                const response = await fetch('https://oficinajudicialvirtual.pjud.cl/sessionN.php', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: formData.toString(),
                    credentials: 'include'
                }});
                
                return {{
                    status: response.status,
                    text: (await response.text()).substring(0, 300)
                }};
            }}
        """)
        
        print(f"   Login response: {login_result.get('status')}")
        
        # Set localStorage
        await page.evaluate("""
            () => {
                localStorage.setItem('InitSitioOld', '0');
                localStorage.setItem('InitSitioNew', '1');
                localStorage.setItem('logged-in', 'true');
                sessionStorage.setItem('logged-in', 'true');
            }
        """)
        
        # 2. Navigate to main page
        print("3. Navegando a página principal...")
        await page.goto("https://oficinajudicialvirtual.pjud.cl/indexN.php", wait_until="networkidle")
        await asyncio.sleep(2)
        
        await page.screenshot(path="screenshots/explore_01_index.png", full_page=True)
        print("   Screenshot: explore_01_index.png")
        
        # Save HTML
        html = await page.content()
        with open("screenshots/explore_01_index.html", "w") as f:
            f.write(html)
        
        print(f"   URL actual: {page.url}")
        print(f"   Título: {await page.title()}")
        
        # 3. Look for navigation/menu
        print()
        print("4. Buscando menús y opciones...")
        
        # Find all links
        links = await page.query_selector_all("a")
        print(f"   Links encontrados: {len(links)}")
        
        interesting_links = []
        for link in links:
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            onclick = await link.get_attribute("onclick") or ""
            
            if any(word in text.lower() for word in ["causa", "civil", "consulta", "buscar", "mis causa"]):
                interesting_links.append({
                    "text": text[:50],
                    "href": href[:80],
                    "onclick": onclick[:50]
                })
        
        print()
        print("   Links interesantes:")
        for link in interesting_links[:10]:
            print(f"     - {link['text']} | href={link['href']}")
        
        # 4. Try to find "Mis Causas" or similar
        print()
        print("5. Buscando sección de causas...")
        
        # Look for frames/iframes
        frames = page.frames
        print(f"   Frames en la página: {len(frames)}")
        for i, frame in enumerate(frames):
            print(f"     Frame {i}: {frame.url[:80]}")
        
        # 5. Try navigating to civil section
        print()
        print("6. Intentando acceder a causas civiles...")
        
        civil_urls = [
            "https://oficinajudicialvirtual.pjud.cl/ADIR_871/civil/",
            "https://oficinajudicialvirtual.pjud.cl/ADIR_871/indexN.php?opc_menu=15",
            "https://oficinajudicialvirtual.pjud.cl/consultaunificadacausas.php",
        ]
        
        for url in civil_urls:
            try:
                print(f"   Probando: {url}")
                await page.goto(url, wait_until="networkidle", timeout=15000)
                await asyncio.sleep(2)
                
                current_url = page.url
                title = await page.title()
                print(f"     -> URL: {current_url}")
                print(f"     -> Título: {title}")
                
                # Save screenshot
                filename = url.split("/")[-1] or "root"
                filename = filename.replace("?", "_").replace("=", "_")[:30]
                await page.screenshot(path=f"screenshots/explore_{filename}.png", full_page=True)
                
                # Save HTML
                html = await page.content()
                with open(f"screenshots/explore_{filename}.html", "w") as f:
                    f.write(html)
                
                # Look for tables or case data
                tables = await page.query_selector_all("table")
                print(f"     -> Tablas encontradas: {len(tables)}")
                
                # Look for forms
                forms = await page.query_selector_all("form")
                print(f"     -> Formularios: {len(forms)}")
                
                print()
                
            except Exception as e:
                print(f"     Error: {str(e)[:50]}")
                print()
        
        # 7. Explore the main frame content
        print("7. Explorando contenido detallado...")
        
        # Go back to index
        await page.goto("https://oficinajudicialvirtual.pjud.cl/indexN.php", wait_until="networkidle")
        await asyncio.sleep(2)
        
        # Check for iframes
        iframe = await page.query_selector("iframe")
        if iframe:
            frame_src = await iframe.get_attribute("src")
            print(f"   Iframe encontrado: {frame_src}")
            
            # Navigate to iframe content directly
            if frame_src:
                full_url = frame_src if frame_src.startswith("http") else f"https://oficinajudicialvirtual.pjud.cl/{frame_src}"
                print(f"   Navegando a: {full_url}")
                await page.goto(full_url, wait_until="networkidle")
                await asyncio.sleep(2)
                
                await page.screenshot(path="screenshots/explore_iframe_content.png", full_page=True)
                html = await page.content()
                with open("screenshots/explore_iframe_content.html", "w") as f:
                    f.write(html)
        
        await browser.close()
        print()
        print("=" * 60)
        print("  EXPLORACIÓN COMPLETADA")
        print("=" * 60)
        print()
        print("Revisa los screenshots en /screenshots/")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python test_explore_causas.py <captcha_token>")
        print()
        token = input("O pega el token aquí: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(explore_causas(token))
