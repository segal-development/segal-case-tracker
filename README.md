# Segal Case Tracker API

API para seguimiento de causas civiles del Poder Judicial de Chile.

## Requisitos

- Python 3.11+
- PostgreSQL 16
- Redis 7
- Docker (para desarrollo local)

## Instalacion

### Desarrollo local

1. Clonar repositorio
```bash
git clone <repo>
cd segal-case-tracker
```

2. Iniciar servicios
```bash
docker-compose up -d
```

3. Instalar dependencias
```bash
poetry install
```

4. Configurar variables de entorno
```bash
cp .env.example .env
# Editar .env con tus valores
```

5. Ejecutar migraciones
```bash
alembic upgrade head
```

6. Instalar Playwright
```bash
playwright install chromium
```

7. Iniciar servidor
```bash
uvicorn app.main:app --reload
```

## API Docs

- Swagger: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Endpoints principales

| Metodo | Endpoint | Descripcion |
|--------|----------|-------------|
| POST | /api/v1/auth/login | Login con RUT + clave PJUD |
| GET | /api/v1/cases | Listar causas |
| POST | /api/v1/cases | Crear/vincular causa |
| GET | /api/v1/cases/{id}/movements | Movimientos de causa |
| GET | /api/v1/scrapper/search | Buscar en PJUD |
| GET | /api/v1/scrapper/poll/{jid} | Polling de busqueda |

## Arquitectura

```
+-------------+     +-------------+     +-------------+
|   FastAPI   |---->|  PostgreSQL |     |    Redis    |
|   (API)     |     |   (datos)   |     |  (sesiones) |
+------+------+     +-------------+     +-------------+
       |
       v
+-------------+     +-------------+
|   Pub/Sub   |---->|   Workers   |------> PJUD Civil
|   (queue)   |     |  (scraping) |
+-------------+     +-------------+
```

## Security / Environment

`ENVIRONMENT` defaults to `production` (fail-closed). Production requires a non-default `SECRET_KEY`, a non-default `ENCRYPTION_KEY` (valid Fernet key), and a non-empty `CORS_ORIGINS` — or the app refuses to boot. Local dev, tests, and Alembic migrations must set `ENVIRONMENT=development` (or provide real production secrets).

## Licencia

Privado - Segal 2024
