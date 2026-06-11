#!/usr/bin/env python3
"""
Test fetching cases from recent years.
"""

import asyncio
import sys
import logging

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

from app.scrapper.pjud_civil import PJUDCivilScraper


async def main(captcha_token: str):
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    scraper = PJUDCivilScraper(headless=True)
    
    try:
        print("=" * 60)
        print("  TEST: CAUSAS ÚLTIMOS 2 AÑOS")
        print("=" * 60)
        
        # Login
        print("\n1. Login...")
        session = await scraper.login_with_token(rut, password, captcha_token)
        print(f"   ✓ Sesión creada")
        
        # First, get counts without fetching all data
        print("\n2. Obteniendo conteos por año...")
        
        count_2026, pages_2026 = await scraper.get_cases_count(session, year="2026")
        print(f"   2026: {count_2026} causas ({pages_2026} páginas)")
        
        count_2025, pages_2025 = await scraper.get_cases_count(session, year="2025")
        print(f"   2025: {count_2025} causas ({pages_2025} páginas)")
        
        count_all, pages_all = await scraper.get_cases_count(session)
        print(f"   Total: {count_all} causas ({pages_all} páginas)")
        
        # Now fetch recent cases
        print("\n3. Obteniendo causas de últimos 2 años...")
        cases = await scraper.get_my_cases_recent(session, years=2)
        
        print(f"\n   ✓ Total obtenido: {len(cases)} causas")
        print(f"   ✓ Esperado: ~{count_2026 + count_2025} causas")
        
        # Show summary by year
        from collections import Counter
        years = Counter(c.fecha_ingreso.split('/')[-1] for c in cases if c.fecha_ingreso)
        print(f"\n   Distribución:")
        for year, count in sorted(years.items(), reverse=True):
            print(f"      {year}: {count} causas")
        
        print("\n" + "=" * 60)
        print("  ✅ TEST COMPLETADO")
        print("=" * 60)
        
    finally:
        await scraper.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_recent_years.py <captcha_token>")
        sys.exit(1)
    
    asyncio.run(main(sys.argv[1]))
