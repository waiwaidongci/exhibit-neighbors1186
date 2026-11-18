from fastapi import APIRouter, Depends, Query
from ..config.database import db_session
from ..schemas.accounting import EventBatch, TerminalInput
from ..services.auth import current_user, writer
from ..controllers.accounting import AccountingController as C

router = APIRouter()


@router.get('/accounting/terminals')
def terminals(db=Depends(db_session), u=Depends(current_user)):
    return C.terminals(db)


@router.post('/accounting/terminals')
def create_terminal(data: TerminalInput, db=Depends(db_session), u=Depends(writer)):
    return C.create_terminal(db, u, data)


@router.post('/accounting/terminals/{id}/events')
def add_events(id: int, data: EventBatch, db=Depends(db_session), u=Depends(writer)):
    return C.add_events(db, u, id, data)


@router.get('/accounting/status')
def status(db=Depends(db_session), u=Depends(current_user)):
    return C.status(db)


@router.get('/accounting/events')
def events(limit: int = Query(default=50, ge=1, le=200), db=Depends(db_session), u=Depends(current_user)):
    return C.events(db, limit)


@router.post('/accounting/close')
def close(db=Depends(db_session), u=Depends(writer)):
    return C.close(db, u)


@router.get('/accounting/versions')
def versions(db=Depends(db_session), u=Depends(current_user)):
    return C.versions(db)


@router.get('/accounting/versions/{id}')
def version(id: int, db=Depends(db_session), u=Depends(current_user)):
    return C.version(db, id)


@router.get('/accounting/versions/{id}/recompute')
def recompute(id: int, db=Depends(db_session), u=Depends(current_user)):
    return C.recompute(db, id)
