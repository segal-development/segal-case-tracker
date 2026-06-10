#!/usr/bin/env python3
"""
Test final del scraper PJUD Civil.
Prueba el flujo completo: Login -> Lista -> Detalle
"""

import asyncio
import sys
import json

sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from app.scrapper.pjud_civil import PJUDCivilScraper, LoginError


async def main(captcha_token: str):
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    scraper = PJUDCivilScraper(headless=True)
    
    try:
        print("=" * 60)
        print("  TEST SCRAPER PJUD CIVIL")
        print("=" * 60)
        print()
        
        # 1. Login
        print("1. Login...")
        session = await scraper.login_with_token(rut, password, captcha_token)
        print(f"   ✓ Sesión creada: {session.rut}")
        print(f"   ✓ Cookies: {len(session.cookies)}")
        
        # 2. Get cases (limit to 3 pages for quick test)
        print()
        print("2. Obteniendo causas civiles (max 3 páginas)...")
        cases = await scraper.get_my_cases(session, max_pages=3)
        print(f"   ✓ Causas encontradas: {len(cases)}")
        
        if cases:
            print()
            print("   Primeras 5 causas:")
            for case in cases[:5]:
                print(f"      - {case.rol}: {case.caratulado[:30]}... ({case.tribunal[:20]}...)")
            
            # 3. Get detail of first case
            print()
            print(f"3. Obteniendo detalle de {cases[0].rol}...")
            detail = await scraper.get_case_detail(session, cases[0].case_token)
            
            print(f"   ✓ ROL: {detail.case.rol}")
            print(f"   ✓ Tribunal: {detail.case.tribunal}")
            print(f"   ✓ Caratulado: {detail.case.caratulado}")
            print(f"   ✓ Fecha Ingreso: {detail.case.fecha_ingreso}")
            print(f"   ✓ Estado Procesal: {detail.estado_procesal}")
            print(f"   ✓ Procedimiento: {detail.procedimiento}")
            print(f"   ✓ Cuadernos: {len(detail.cuadernos)}")
            print(f"   ✓ Movimientos: {len(detail.movements)}")
            
            if detail.movements:
                print()
                print("   Movimientos:")
                for mov in detail.movements[:5]:
                    desc = mov.descripcion[:30] if mov.descripcion else "(sin desc)"
                    docs_icon = "📄" if mov.tiene_documento else "  "
                    anexos_icon = "📎" if mov.tiene_anexos else "  "
                    print(f"      {docs_icon}{anexos_icon} Folio {mov.folio} [{mov.fecha}] {mov.tipo_tramite}: {desc}")
                    if mov.documentos:
                        for doc in mov.documentos:
                            print(f"            └─ {doc.tipo}: {doc.url_type} ({doc.token[:20]}...)")
            
            # Save detail to JSON
            detail_dict = {
                "rol": detail.case.rol,
                "tribunal": detail.case.tribunal,
                "caratulado": detail.case.caratulado,
                "fecha_ingreso": detail.case.fecha_ingreso,
                "estado_procesal": detail.estado_procesal,
                "procedimiento": detail.procedimiento,
                "ubicacion": detail.ubicacion,
                "etapa": detail.etapa,
                "cuadernos": detail.cuadernos,
                "movements": [
                    {
                        "folio": m.folio,
                        "fecha": m.fecha,
                        "tipo": m.tipo_tramite,
                        "descripcion": m.descripcion,
                        "etapa": m.etapa,
                        "tiene_documento": m.tiene_documento,
                        "tiene_anexos": m.tiene_anexos,
                        "documentos": [
                            {
                                "token": d.token,
                                "tipo": d.tipo,
                                "url_type": d.url_type,
                            }
                            for d in m.documentos
                        ]
                    }
                    for m in detail.movements
                ]
            }
            
            with open("screenshots/detail_final.json", "w") as f:
                json.dump(detail_dict, f, indent=2, ensure_ascii=False)
            
            print()
            print("   ✓ Detalle guardado en screenshots/detail_final.json")
        
        print()
        print("=" * 60)
        print("  ✅ TEST COMPLETADO EXITOSAMENTE")
        print("=" * 60)
        
    except LoginError as e:
        print(f"   ✗ Error de login: {e}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await scraper.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        token = input("Pega el token de captcha: ").strip()
    else:
        token = sys.argv[1]
    
    asyncio.run(main(token))
