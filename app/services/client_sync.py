"""Per-client Clave Única sync — decrypts the client's CU credential,
logs in as the client, fetches their PJUD cases, and attributes them to
the assigned firm lawyer.

Key attribution rule:
  - lawyer_id = client.assigned_lawyer_id  (firm lawyer, for case ownership)
  - client_id = client.id                  (client link, set after sync)
"""
import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


async def sync_one_cu_client(
    sc, db: Session, client, *, batch: int = 200
) -> tuple[int, int, int]:
    """Log in AS `client` via their stored Clave Única (CU), scrape their
    PJUD Mis Causas, and persist via sync_cases + detect_and_sync_movements
    with lawyer_id = client.assigned_lawyer_id.

    After syncing, sets case.client_id = client.id on all Case rows that
    match the scraped rols for this lawyer_id and have no client_id yet.

    Returns (movements_created, movements_updated, errors).
    Does NOT commit — the caller owns the transaction.

    Raises ValueError if:
      - client.assigned_lawyer_id is None (no firm lawyer assigned)
      - client.encrypted_clave_unica_password is None (no CU stored)
    """
    if client.assigned_lawyer_id is None:
        raise ValueError(
            f"Client {client.rut!r} has no assigned_lawyer_id — cannot sync"
        )
    if client.encrypted_clave_unica_password is None:
        raise ValueError(
            f"Client {client.rut!r} has no encrypted_clave_unica_password — cannot sync"
        )

    from app.core.security import decrypt_pjud_password
    from app.models.case import Case
    from app.scrapper.pjud.clave_unica import ClaveUnicaAuth, ClaveUnicaCredentials
    from app.services.sync_service import (
        ScrapedCase,
        SyncService,
        _select_cases_for_detail_rotation,
        detect_and_sync_movements,
    )

    cu_password = decrypt_pjud_password(client.encrypted_clave_unica_password)
    clave_rut: str = client.clave_unica_rut or client.rut
    lawyer_id = int(client.assigned_lawyer_id)
    client_id = int(client.id)

    # Track the active session so the reauth callback can refresh it.
    held: dict = {"session": None}

    async def cu_login():
        """Perform a fresh Clave Única login for THIS client."""
        if not sc._browser:
            await sc.start()
        ctx = await sc._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        session = await ClaveUnicaAuth().login(
            page,
            ClaveUnicaCredentials(rut=clave_rut, password=cu_password),
            lawyer_id,
        )
        await ctx.close()
        sc._page = None
        held["session"] = session
        return session

    # Initial CU login — raises on failure (caller handles it).
    session = await cu_login()

    # Fetch full Mis Causas (max_pages=0 → all pages).
    api_cases = await sc.get_my_cases(session, max_pages=0)

    # Build ScrapedCase list from PJUDCase dataclass attributes.
    scraped = [
        ScrapedCase(
            rol=c.rol,
            tribunal=c.tribunal,
            caratulado=c.caratulado,
            fecha_ingreso=c.fecha_ingreso,
            estado_cuaderno=c.estado_cuaderno or "",
            cuaderno=c.cuaderno or "",
            institucion=c.institucion,
        )
        for c in api_cases
    ]
    SyncService(db).sync_cases(lawyer_id, scraped, competencia="civil")

    # Tag touched cases with client_id (set only where not yet assigned).
    touched_rols = {c.rol for c in api_cases}
    if touched_rols:
        (
            db.query(Case)
            .filter(
                Case.lawyer_id == lawyer_id,
                Case.rol.in_(touched_rols),
                Case.client_id.is_(None),
            )
            .update({"client_id": client_id}, synchronize_session="fetch")
        )

    # Detail / movement rotation — reserved cases first.
    selected = _select_cases_for_detail_rotation(
        db, lawyer_id, "civil", api_cases, batch, reserved_first=True
    )

    created, updated, errors = await detect_and_sync_movements(
        db=db,
        scraper=sc,
        pjud_session=session,
        lawyer_id=lawyer_id,
        api_cases=api_cases,
        selected_cases=selected,
        delay_between_fetches=2.0,
        reauth_callback=cu_login,
    )

    return created, updated, len(errors)
