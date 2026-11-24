from datetime import datetime
from sqlalchemy import JSON, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class TruckEvent(Base):
    """门岗入队/开卸与调度交接单的不可变原始事件。

    补报不修改本表：修正后的事件以新行写入，origin 标记为补报批次号，
    原始行始终保留，供市场日回查转场依据。
    """

    __tablename__ = "truck_events"
    __table_args__ = (
        UniqueConstraint("truck_id", "event_type", "seq", "origin",
                         name="uq_truck_event_origin"),
        Index("ix_truck_event_truck", "truck_id"),
        Index("ix_truck_event_market_day", "market_id", "event_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    truck_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int]
    event_type: Mapped[str] = mapped_column(String(20))
    system: Mapped[str] = mapped_column(String(10))
    market_id: Mapped[str] = mapped_column(String(300))
    event_time: Mapped[datetime] = mapped_column(index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    # source=原始导入；correction=补报批次产生的覆盖事件。
    source: Mapped[str] = mapped_column(String(20), default="source")
    origin: Mapped[str] = mapped_column(String(64), default="import")
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class CorrectionBatch(Base):
    """补报批次：原归属与新归属算齐后一次性切换，旧批次不得覆盖。"""

    __tablename__ = "correction_batches"
    __table_args__ = (UniqueConstraint("batch_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_seq: Mapped[int] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    note: Mapped[str | None] = mapped_column(String(500))
    corrections: Mapped[list] = mapped_column(JSON)
    # 提交时算好的差分清单与拒绝原因，保证页面可复核。
    switches: Mapped[list | None] = mapped_column(JSON)
    reject_reasons: Mapped[list | None] = mapped_column(JSON)
    touched_trucks: Mapped[list] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    applied_at: Mapped[datetime | None]
