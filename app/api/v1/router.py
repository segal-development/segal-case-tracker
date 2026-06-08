from fastapi import APIRouter

from app.api.v1 import auth, cases, movements, courts, scrapper, webhooks, pjud, sync

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(cases.router, prefix="/cases", tags=["cases"])
api_router.include_router(courts.router, prefix="/courts", tags=["courts"])
api_router.include_router(scrapper.router, prefix="/scrapper", tags=["scrapper"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(pjud.router, prefix="/pjud", tags=["pjud"])
api_router.include_router(sync.router, prefix="/sync", tags=["sync"])
