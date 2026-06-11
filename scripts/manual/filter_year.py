#!/usr/bin/env python3
"""
Test filtering by year to reduce results.
"""

import asyncio
import sys
import logging

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

logging.basicConfig(level=logging.INFO, format='%(message)s')

from app.scrapper.pjud_civil import PJUDCivilScraper


async def main(captcha_token: str, year: str = "2026"):
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    scraper = PJUDCivilScraper(headless=True)
    
    print("=" * 60)
    print(f"  TEST FILTRO POR AÑO: {year}")
    print("=" * 60)
    
    # Login
    print("\n1. Login...")
    session = await scraper.login_with_token(rut, password, captcha_token)
    print(f"   ✓ Sesión creada")
    
    # Get cases filtered by year
    print(f"\n2. Obteniendo causas civiles del {year}...")
    cases = await scraper.get_my_cases(session, year=year, max_pages=5)
    
    print(f"\n   ✓ Total causas {year}: {len(cases)}")
    
    if cases:
        print(f"\n   Primeras 10 causas:")
        for case in cases[:10]:
            print(f"      - {case.rol} ({case.fecha_ingreso}): {case.caratulado[:35]}...")
    
    print("\n" + "=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_filter_year.py <captcha_token> [year]")
        sys.exit(1)
    
    token = sys.argv[1]
    year = sys.argv[2] if len(sys.argv) > 2 else "2026"
    
    asyncio.run(main(token, year))
