#!/usr/bin/env python3
"""
Test downloading documents from a case.
"""

import asyncio
import sys
import os

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from app.scrapper.pjud_civil import PJUDCivilScraper


async def main(captcha_token: str):
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    scraper = PJUDCivilScraper(headless=True)
    
    try:
        print("=" * 60)
        print("  TEST: DESCARGAR DOCUMENTOS")
        print("=" * 60)
        
        # Login
        print("\n1. Login...")
        session = await scraper.login_with_token(rut, password, captcha_token)
        print(f"   ✓ Sesión creada")
        
        # Get first case
        print("\n2. Obteniendo primera causa...")
        cases = await scraper.get_my_cases(session, max_pages=1)
        if not cases:
            print("   ✗ No se encontraron causas")
            return
        
        case = cases[0]
        print(f"   ✓ {case.rol}: {case.caratulado}")
        
        # Get detail
        print("\n3. Obteniendo detalle...")
        detail = await scraper.get_case_detail(session, case.case_token)
        print(f"   ✓ {len(detail.movements)} movimientos")
        
        # Find movements with documents
        movs_with_docs = [m for m in detail.movements if m.tiene_documento]
        print(f"   ✓ {len(movs_with_docs)} movimientos con documentos")
        
        if not movs_with_docs:
            print("   ⚠ No hay documentos para descargar")
            return
        
        # Create output directory
        output_dir = "screenshots/documentos"
        os.makedirs(output_dir, exist_ok=True)
        
        # Download documents from first movement
        print(f"\n4. Descargando documentos del movimiento folio {movs_with_docs[0].folio}...")
        
        downloaded = await scraper.download_movement_documents(
            session=session,
            movement=movs_with_docs[0],
            output_dir=output_dir,
            rol=detail.case.rol,
        )
        
        print(f"\n   ✓ Descargados {len(downloaded)} archivos:")
        for path in downloaded:
            size = os.path.getsize(path)
            print(f"      - {os.path.basename(path)} ({size:,} bytes)")
        
        print("\n" + "=" * 60)
        print("  ✅ TEST COMPLETADO")
        print("=" * 60)
        
    finally:
        await scraper.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_download_docs.py <captcha_token>")
        sys.exit(1)
    
    asyncio.run(main(sys.argv[1]))
