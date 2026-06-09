import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
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


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    from app.workers.sync_scheduler import is_scheduler_running
    
    return {
        "status": "healthy",
        "scheduler": "running" if is_scheduler_running() else "disabled",
    }
