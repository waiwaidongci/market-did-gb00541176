"""车次对账的入库、链重建与补报批次服务。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.domain import reconciliation as engine
from src.models.trucks import CorrectionBatch, TruckEvent
from src.utils.time import now


def _domain_event(item: TruckEvent):
    """把持久化事件还原成引擎需要的字典（含交接单的 from/to 链接）。"""
    event = dict(item.payload)
    event["event_time"] = item.event_time
    return event


def _effective_events(db):
    """当前生效事件 = 原始事件叠加全部已生效补报，按 (车次,类型,流水) 后者覆盖。"""
    rows = db.scalars(
        select(TruckEvent).where(TruckEvent.source == "source").order_by(TruckEvent.id)
    ).all()
    events = [_domain_event(item) for item in rows]
    applied = db.scalars(
        select(CorrectionBatch)
        .where(CorrectionBatch.status == engine.APPLIED)
        .order_by(CorrectionBatch.batch_seq)
    ).all()
    corrections = [item for batch in applied for item in batch.corrections]
    if corrections:
        events = engine.apply_corrections(events, corrections)
    return events


def current_chains(db):
    return engine.build_chains(_effective_events(db))


def import_events(db, raw_events, actor):
    """导入一批原始事件；重复 (车次,类型,流水) 跳过，原始行不可变。"""
    normalized = [engine.validate_event(dict(event)) for event in raw_events]
    inserted, skipped = 0, 0
    for event in normalized:
        exists = db.scalar(
            select(TruckEvent.id).where(
                TruckEvent.truck_id == event["truck_id"],
                TruckEvent.event_type == event["event_type"],
                TruckEvent.seq == event["seq"],
                TruckEvent.source == "source",
            ).with_for_update())
        if exists:
            skipped += 1
            continue
        payload = {key: event[key] for key in event if key not in ("event_time", "raw_time")}
        db.add(TruckEvent(
            truck_id=event["truck_id"], seq=event["seq"], event_type=event["event_type"],
            system=event["system"], market_id=event["market_id"], event_time=event["event_time"],
            payload=payload, source="source", origin="import", author=actor))
        inserted += 1
    return {"inserted": inserted, "skipped_duplicates": skipped, "total": len(normalized)}


def submit_correction_batch(db, batch_seq, note, raw_corrections, actor):
    """算齐原归属与新归属后一次性切换；不满足口径则整批拒绝并留痕。"""
    if type(batch_seq) is not int or batch_seq <= 0:
        raise engine.ReconciliationError("补报批次号须为正整数。")
    if not raw_corrections:
        raise engine.ReconciliationError("补报批次至少包含一条事件。")

    # 行锁串行化批次提交，避免两个批次同时通过序号检查。
    existing = db.scalar(select(CorrectionBatch).where(
        CorrectionBatch.batch_seq == batch_seq).with_for_update())
    if existing:
        raise engine.ReconciliationError(f"批次号 {batch_seq} 已存在，不能重复提交。")
    applied_max = db.scalar(
        select(CorrectionBatch.batch_seq)
        .where(CorrectionBatch.status == engine.APPLIED)
        .order_by(CorrectionBatch.batch_seq.desc()).limit(1)) or 0

    # 规范化补报事件，并校验内部结构。
    corrections = []
    touched = set()
    for item_seq, raw in enumerate(raw_corrections, start=1):
        event = engine.validate_event(dict(raw))
        touched.add(event["truck_id"])
        stored = {key: event[key] for key in event if key not in ("event_time", "raw_time")}
        stored["event_time"] = event["raw_time"]
        corrections.append({"batch_seq": batch_seq, "item_seq": item_seq, "event": stored})

    before_chains = current_chains(db)
    effective = engine.apply_corrections(_effective_events(db), corrections)
    after_chains = engine.build_chains(effective)
    errors = engine.validate_batch(
        batch_seq, corrections, applied_max, before_chains, after_chains)

    switches = engine.plan_batch_switch(before_chains, after_chains, touched)
    batch = CorrectionBatch(
        batch_seq=batch_seq, note=note, corrections=corrections,
        touched_trucks=sorted(touched), author=actor,
        switches=switches, reject_reasons=errors)
    if errors:
        batch.status = engine.REJECTED
        db.add(batch)
        return batch, False

    # 原子切换：补报事件落库 + 批次置为已生效，同一事务提交。
    for correction in corrections:
        event = engine.validate_event(dict(correction["event"]))
        payload = {key: event[key] for key in event if key not in ("event_time", "raw_time")}
        row = TruckEvent(
            truck_id=event["truck_id"], seq=event["seq"], event_type=event["event_type"],
            system=event["system"], market_id=event["market_id"], event_time=event["event_time"],
            payload=payload, source="correction", origin=f"batch-{batch_seq}", author=actor)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise engine.ReconciliationError(
                f"批次 {batch_seq} 与已入库补报事件冲突，请核对车次流水。") from None
    batch.status = engine.APPLIED
    batch.applied_at = now()
    db.add(batch)
    return batch, True


def market_day_rows(db, market_id=None, market_day=None):
    chains = current_chains(db)
    rows = engine.market_day_summary(chains)
    if market_id:
        rows = [row for row in rows if row["market_id"] == market_id]
    if market_day:
        rows = [row for row in rows if row["market_day"] == market_day]
    return rows, chains


def anomaly_rows(db):
    chains = current_chains(db)
    return [chain for chain in chains.values() if chain["excluded"]]
