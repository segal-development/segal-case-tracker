#!/usr/bin/env python3
"""
Test the PJUD API endpoints.

Usage:
    1. Start the server: uvicorn app.main:app --reload
    2. Run this script: python test_api.py <captcha_token>
"""

import sys
import requests
import json

BASE_URL = "http://localhost:8000/api/v1/pjud"

def main(captcha_token: str):
    print("=" * 60)
    print("  TEST PJUD API")
    print("=" * 60)
    
    # 1. Login
    print("\n1. POST /pjud/login")
    resp = requests.post(f"{BASE_URL}/login", json={
        "rut": "16021492-9",
        "password": "Gruposegal2026+",
        "captcha_token": captcha_token,
    })
    
    if resp.status_code != 200:
        print(f"   ✗ Error: {resp.status_code}")
        print(f"   {resp.text}")
        return
    
    login_data = resp.json()
    session_id = login_data["session_id"]
    print(f"   ✓ Login OK")
    print(f"   Session: {session_id[:30]}...")
    
    # 2. Get count
    print("\n2. GET /pjud/cases/count?year=2026")
    resp = requests.get(f"{BASE_URL}/cases/count", params={
        "session_id": session_id,
        "year": "2026",
    })
    
    count_data = resp.json()
    print(f"   ✓ Total 2026: {count_data['total_cases']} causas ({count_data['total_pages']} páginas)")
    
    # 3. Get cases list
    print("\n3. GET /pjud/cases?year=2026&max_pages=1")
    resp = requests.get(f"{BASE_URL}/cases", params={
        "session_id": session_id,
        "year": "2026",
        "max_pages": 1,
    })
    
    cases_data = resp.json()
    print(f"   ✓ Obtenidas: {cases_data['total']} causas")
    print("\n   Primeras 3:")
    for c in cases_data["cases"][:3]:
        print(f"      - {c['rol']}: {c['caratulado'][:35]}...")
    
    # 4. Get case detail
    if cases_data["cases"]:
        first_rol = cases_data["cases"][0]["rol"]
        print(f"\n4. GET /pjud/cases/{first_rol}")
        resp = requests.get(f"{BASE_URL}/cases/{first_rol}", params={
            "session_id": session_id,
        })
        
        detail = resp.json()
        case = detail["case"]
        print(f"   ✓ ROL: {case['rol']}")
        print(f"   ✓ Tribunal: {case['tribunal']}")
        print(f"   ✓ Caratulado: {case['caratulado']}")
        print(f"   ✓ Estado: {case['estado_procesal']}")
        print(f"   ✓ Movimientos: {len(case['movements'])}")
        
        print("\n   Movimientos:")
        for m in case["movements"][:3]:
            docs = "📄" if m["tiene_documento"] else "  "
            print(f"      {docs} Folio {m['folio']} [{m['fecha']}] {m['tipo_tramite']}: {m['descripcion'][:30]}")
            for d in m["documentos"]:
                print(f"            └─ {d['tipo']} ({d['url_type']})")
        
        # Pretty print full JSON
        print("\n   JSON completo guardado en: screenshots/api_response.json")
        with open("screenshots/api_response.json", "w") as f:
            json.dump(detail, f, indent=2, ensure_ascii=False)
    
    # 5. Logout
    print("\n5. DELETE /pjud/logout")
    resp = requests.delete(f"{BASE_URL}/logout", params={
        "session_id": session_id,
    })
    print(f"   ✓ {resp.json()['message']}")
    
    print("\n" + "=" * 60)
    print("  ✅ TEST COMPLETADO")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_api.py <captcha_token>")
        print("\nPrimero levanta el servidor:")
        print("  uvicorn app.main:app --reload")
        sys.exit(1)
    
    main(sys.argv[1])
