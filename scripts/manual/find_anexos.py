#!/usr/bin/env python3
"""
Find a case with anexos to test document download.
"""

import asyncio
import sys

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from app.scrapper.pjud_civil import PJUDCivilScraper


async def main(captcha_token: str):
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    scraper = PJUDCivilScraper(headless=True)
    
    try:
        print("=" * 60)
        print("  BUSCANDO CAUSAS CON ANEXOS")
        print("=" * 60)
        
        # Login
        print("\n1. Login...")
        session = await scraper.login_with_token(rut, password, captcha_token)
        print(f"   ✓ Sesión creada")
        
        # Get cases
        print("\n2. Obteniendo causas (5 páginas)...")
        cases = await scraper.get_my_cases(session, max_pages=5)
        print(f"   ✓ {len(cases)} causas")
        
        # Check each case for anexos
        print("\n3. Buscando causas con anexos...")
        
        found_with_anexos = []
        
        for i, case in enumerate(cases[:20]):  # Check first 20
            print(f"   Revisando {case.rol}...", end=" ")
            
            try:
                detail = await scraper.get_case_detail(session, case.case_token)
                
                # Check for anexos
                movs_with_anexos = [m for m in detail.movements if m.tiene_anexos]
                movs_with_docs = [m for m in detail.movements if m.tiene_documento]
                total_docs = sum(len(m.documentos) for m in detail.movements)
                
                if movs_with_anexos:
                    print(f"✓ {len(movs_with_anexos)} mov con anexos, {total_docs} docs total")
                    found_with_anexos.append({
                        'case': case,
                        'detail': detail,
                        'movs_with_anexos': movs_with_anexos,
                        'total_docs': total_docs,
                    })
                    
                    # Show details
                    for m in movs_with_anexos[:2]:
                        print(f"      └─ Folio {m.folio}: {len(m.documentos)} docs")
                        for d in m.documentos:
                            print(f"          - {d.tipo} ({d.url_type})")
                    
                    if len(found_with_anexos) >= 3:
                        break
                else:
                    print(f"- {len(movs_with_docs)} docs, sin anexos")
                    
            except Exception as e:
                print(f"✗ Error: {e}")
        
        if found_with_anexos:
            print(f"\n✓ Encontradas {len(found_with_anexos)} causas con anexos!")
        else:
            print("\n⚠ No se encontraron causas con anexos en las primeras 20")
            print("   Esto puede ser normal - no todas las causas tienen anexos")
        
        print("\n" + "=" * 60)
        
    finally:
        await scraper.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_find_anexos.py <captcha_token>")
        sys.exit(1)
    
    asyncio.run(main(sys.argv[1]))
