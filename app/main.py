import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import get_db
from app.api.v1.router import api_router
from app.api.sysgal.causas import router as sysgal_causas_router
from app.api.sysgal.plazos import router as sysgal_plazos_router
from app.api.sysgal.novedades import router as sysgal_novedades_router
from app.api.sysgal.buscar import router as sysgal_buscar_router
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown."""
    # Startup
    if os.getenv("ENABLE_SCHEDULER", "false").lower() == "true":
        from app.workers.sync_scheduler import start_scheduler
        start_scheduler()
    
    yield
    
    # Shutdown
    if os.getenv("ENABLE_SCHEDULER", "false").lower() == "true":
        from app.workers.sync_scheduler import stop_scheduler
        stop_scheduler()


app = FastAPI(
    title="Segal Case Tracker API",
    description="API para seguimiento de causas civiles - Segal",
    version="0.1.0",
    docs_url="/docs" if settings.effective_debug else None,
    redoc_url="/redoc" if settings.effective_debug else None,
    lifespan=lifespan,
)

# CORS — explicit origins only; credentials require non-wildcard origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(api_router, prefix="/api/v1")

# External read-only API for the Sysgal CRM. Mounted SEPARATELY (own prefix,
# own auth) so it is fully isolated from the internal /api/v1 app.
app.include_router(sysgal_causas_router, prefix="/api/sysgal/v1", tags=["sysgal"])
app.include_router(sysgal_plazos_router, prefix="/api/sysgal/v1", tags=["sysgal"])
app.include_router(sysgal_novedades_router, prefix="/api/sysgal/v1", tags=["sysgal"])
app.include_router(sysgal_buscar_router, prefix="/api/sysgal/v1", tags=["sysgal"])


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    from app.workers.sync_scheduler import is_scheduler_running
    
    return {
        "status": "healthy",
        "scheduler": "running" if is_scheduler_running() else "disabled",
    }


@app.get("/readyz")
async def readiness_check(db=Depends(get_db)):
    """Readiness probe: verifies the app can reach the database. Returns 503 if
    the DB is unreachable, so a deploy (or load balancer) never treats a
    DB-broken app as healthy — /health is liveness only and never touches the DB.
    """
    from sqlalchemy import text

    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unavailable: {type(exc).__name__}")
    return {"ready": True, "db": "ok"}
