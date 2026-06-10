#!/usr/bin/env python3
"""
Test pagination with more pages to verify it scales.
"""

import asyncio
import sys
import logging

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

# Enable logging to see pagination progress
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from app.scrapper.pjud_civil import PJUDCivilScraper, LoginError


async def main(captcha_token: str, max_pages: int = 10):
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    scraper = PJUDCivilScraper(headless=True)
    
    try:
        print("=" * 60)
        print(f"  TEST PAGINACIÓN ({max_pages} páginas)")
        print("=" * 60)
        
        # 1. Login
        print("\n1. Login...")
        session = await scraper.login_with_token(rut, password, captcha_token)
        print(f"   ✓ Sesión creada: {session.rut}")
        
        # 2. Get cases with pagination
        print(f"\n2. Obteniendo causas civiles (max {max_pages} páginas)...")
        cases = await scraper.get_my_cases(session, max_pages=max_pages)
        
        print(f"\n   ✓ Total causas obtenidas: {len(cases)}")
        print(f"   ✓ Esperadas (~15/página): ~{max_pages * 15}")
        
        # Show some from middle
        if len(cases) > 30:
            print("\n   Causas #30-35:")
            for case in cases[30:35]:
                print(f"      - {case.rol}: {case.caratulado[:40]}...")
        
        print("\n" + "=" * 60)
        print("  ✅ TEST COMPLETADO")
        print("=" * 60)
        
    finally:
        await scraper.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_pagination_full.py <captcha_token> [max_pages]")
        sys.exit(1)
    
    token = sys.argv[1]
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    
    asyncio.run(main(token, pages))
