#!/usr/bin/env python3
"""
Test de login real con PJUD.

Uso:
    python test_login_real.py

Te pedirá el token de captcha que obtuviste del HTML de prueba.
"""

import asyncio
import sys
import json
from datetime import datetime

# Add project to path
sys.path.insert(0, '/Users/marcelo/Projects/segal-case-tracker')

from app.scrapper.pjud_civil import PJUDCivilScraper, LoginError
from app.scrapper.session_manager import SessionManager, PJUDSession


async def test_login():
    print("=" * 60)
    print("  TEST DE LOGIN PJUD - Segal Case Tracker")
    print("=" * 60)
    print()
    
    # Credenciales (ya las tenemos)
    rut = "16021492-9"
    password = "Gruposegal2026+"
    
    print(f"RUT: {rut}")
    print(f"Password: {'*' * len(password)}")
    print()
    
    # Pedir token
    print("📋 Abrí el archivo test_captcha.html en tu browser")
    print("   y generá un token de reCAPTCHA.")
    print()
    captcha_token = input("🔑 Pegá el token aquí: ").strip()
    
    if not captcha_token:
        print("❌ Token vacío, abortando.")
        return
    
    print()
    print(f"Token recibido: {captcha_token[:50]}...")
    print()
    print("-" * 60)
    print("🚀 Iniciando login en PJUD...")
    print("-" * 60)
    print()
    
    # Crear scraper (sin captcha solver, usamos el token del frontend)
    scraper = PJUDCivilScraper(
        session_manager=SessionManager(),
        headless=True,
    )
    
    try:
        await scraper.start()
        print("✓ Browser iniciado")
        
        # Intentar login
        session = await scraper.login_with_token(
            rut=rut,
            password=password,
            captcha_token=captcha_token,
        )
        
        print()
        print("=" * 60)
        print("  ✅ LOGIN EXITOSO!")
        print("=" * 60)
        print()
        print(f"RUT: {session.rut}")
        print(f"Creado: {session.created_at}")
        print(f"Expira: {session.expires_at}")
        print(f"Cookies: {len(session.cookies)} cookies guardadas")
        print()
        
        # Guardar sesión para debug
        with open("screenshots/session_data.json", "w") as f:
            json.dump({
                "rut": session.rut,
                "created_at": session.created_at.isoformat(),
                "expires_at": session.expires_at.isoformat(),
                "cookies": session.cookies,
            }, f, indent=2)
        print("📁 Sesión guardada en screenshots/session_data.json")
        
        # Intentar navegar para verificar
        print()
        print("-" * 60)
        print("🔍 Verificando acceso a causas civiles...")
        print("-" * 60)
        
        # Probar búsqueda simple
        # Por ahora solo verificamos que podemos acceder
        
        return session
        
    except LoginError as e:
        print()
        print("=" * 60)
        print("  ❌ LOGIN FALLIDO")
        print("=" * 60)
        print()
        print(f"Error: {e}")
        print()
        print("Posibles causas:")
        print("  - Token de captcha expirado (duran ~2 min)")
        print("  - Credenciales incorrectas")
        print("  - PJUD no disponible")
        print()
        
    except Exception as e:
        print()
        print(f"❌ Error inesperado: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        await scraper.stop()
        print()
        print("Browser cerrado.")


if __name__ == "__main__":
    asyncio.run(test_login())
