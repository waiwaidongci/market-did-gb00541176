from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.reconciliation import APPLIED, REJECTED
from src.models.identity import User
from src.models.trucks import CorrectionBatch, TruckEvent
from src.schemas.requests import CorrectionRequest, TruckEventImport
from src.services import reconciliation as service
from src.services.audit import record
from src.utils.time import iso

router = APIRouter(prefix="/reconciliation")


def _chain_brief(chain):
    return {key: chain[key] for key in (
        "truck_id", "state", "excluded", "reasons", "market_day", "first_market",
        "transfer_count", "duration_minutes")}


@router.get("/market-days")
def market_days(market_id: str | None = Query(None),
                market_day: str | None = Query(None),
                db: Session = Depends(connection),
                actor: User = Depends(current)):
    rows, chains = service.market_day_rows(db, market_id, market_day)
    anomalies = [chain["truck_id"] for chain in chains.values() if chain["excluded"]]
    return {
        "rows": rows,
        "excluded_truck_count": len(anomalies),
        "excluded_truck_ids": sorted(anomalies),
        "rule": "整段用时只计给首次入队所在市场日；分叉、断链、成环车次暂不计入，原始记录保留。",
    }


@router.get("/trucks/{truck_id}")
def truck_detail(truck_id: str, db: Session = Depends(connection),
                 actor: User = Depends(current)):
    chains = service.current_chains(db)
    chain = chains.get(truck_id)
    if not chain:
        raise HTTPException(404, "找不到该车次的对账记录")
    # 同时给出原始入库行，证明页面回查的是保留下来的原始记录与补报痕迹。
    raw_rows = db.scalars(select(TruckEvent).where(
        TruckEvent.truck_id == truck_id).order_by(TruckEvent.event_time, TruckEvent.id)).all()
    return {
        **_chain_brief(chain),
        "events": chain["events"],
        "raw_records": [
            {"event_type": item.event_type, "seq": item.seq, "system": item.system,
             "market_id": item.market_id, "event_time": iso(item.event_time),
             "source": item.source, "origin": item.origin, "author": item.author}
            for item in raw_rows],
    }


@router.get("/anomalies")
def anomalies(db: Session = Depends(connection), actor: User = Depends(current)):
    return {"rows": [_chain_brief(chain) for chain in service.anomaly_rows(db)]}


@router.get("/batches")
def batches(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(CorrectionBatch).order_by(
        CorrectionBatch.batch_seq.desc()).limit(100)).all()
    return [
        {"id": item.id, "batch_seq": item.batch_seq, "status": item.status,
         "note": item.note, "touched_trucks": item.touched_trucks,
         "switches": item.switches, "reject_reasons": item.reject_reasons,
         "author": item.author, "created_at": iso(item.created_at),
         "applied_at": iso(item.applied_at)}
        for item in items
    ]


@router.post("/events", status_code=201)
def import_events(body: TruckEventImport, db: Session = Depends(connection),
                  actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    result = service.import_events(db, body.events, actor.username)
    record(db, actor.username, "truck_events.imported", "reconciliation", **result)
    db.commit()
    return result


@router.post("/corrections", status_code=201)
def submit_correction(body: CorrectionRequest, db: Session = Depends(connection),
                      actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    batch, applied = service.submit_correction_batch(
        db, body.batch_seq, body.note, body.corrections, actor.username)
    record(db, actor.username,
           f"correction.{APPLIED if applied else REJECTED}", batch.batch_seq,
           trucks=batch.touched_trucks, reasons=batch.reject_reasons)
    db.commit()
    return {
        "batch_seq": batch.batch_seq, "status": batch.status,
        "switches": batch.switches, "reject_reasons": batch.reject_reasons,
        "touched_trucks": batch.touched_trucks, "applied": applied,
    }
