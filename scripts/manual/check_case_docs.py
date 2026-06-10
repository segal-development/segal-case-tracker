#!/usr/bin/env python3
"""
Check a specific case with multiple documents.
"""

import asyncio
import sys
import os

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from app.scrapper.pjud_civil import PJUDCivilScraper


async def main(captcha_token: str, target_rol: str = "C-5524-2026"):
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    scraper = PJUDCivilScraper(headless=True)
    
    try:
        print("=" * 60)
        print(f"  REVISANDO {target_rol}")
        print("=" * 60)
        
        # Login
        print("\n1. Login...")
        session = await scraper.login_with_token(rut, password, captcha_token)
        print(f"   ✓ Sesión creada")
        
        # Get cases
        print("\n2. Buscando la causa...")
        cases = await scraper.get_my_cases(session, max_pages=10)
        
        target = None
        for c in cases:
            if c.rol == target_rol:
                target = c
                break
        
        if not target:
            print(f"   ✗ No se encontró {target_rol}")
            return
        
        print(f"   ✓ Encontrada: {target.caratulado}")
        
        # Get detail
        print("\n3. Obteniendo detalle...")
        detail = await scraper.get_case_detail(session, target.case_token)
        
        print(f"   ✓ {len(detail.movements)} movimientos")
        print(f"   ✓ {len(detail.cuadernos)} cuadernos")
        
        # Show all movements with their documents
        print("\n4. Movimientos y documentos:")
        for m in detail.movements:
            docs_count = len(m.documentos)
            icon = "📄" if m.tiene_documento else "  "
            anexo_icon = "📎" if m.tiene_anexos else "  "
            print(f"   {icon}{anexo_icon} Folio {m.folio} [{m.fecha}] {m.tipo_tramite}")
            print(f"       └─ {m.descripcion[:50]}")
            if m.documentos:
                for d in m.documentos:
                    print(f"          • {d.tipo} ({d.url_type})")
        
        # Download all documents from this case
        print(f"\n5. Descargando TODOS los documentos...")
        output_dir = f"screenshots/documentos/{target_rol.replace('/', '-')}"
        os.makedirs(output_dir, exist_ok=True)
        
        total_downloaded = []
        for m in detail.movements:
            if m.documentos:
                downloaded = await scraper.download_movement_documents(
                    session=session,
                    movement=m,
                    output_dir=output_dir,
                    rol=detail.case.rol,
                )
                total_downloaded.extend(downloaded)
        
        print(f"\n   ✓ Descargados {len(total_downloaded)} archivos:")
        for path in total_downloaded:
            size = os.path.getsize(path)
            print(f"      - {os.path.basename(path)} ({size:,} bytes)")
        
        print("\n" + "=" * 60)
        
    finally:
        await scraper.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_check_case_docs.py <captcha_token> [rol]")
        sys.exit(1)
    
    token = sys.argv[1]
    rol = sys.argv[2] if len(sys.argv) > 2 else "C-5524-2026"
    
    asyncio.run(main(token, rol))
