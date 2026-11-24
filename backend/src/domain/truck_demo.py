"""车次跨系统对账演示事件。

覆盖：单市场直卸、跨市场转场（归属首次入队市场日）、断链、交接分叉、成环，
以及一条可由补报批次修复并改归市场日的断链车次。
"""

from src.domain.reconciliation import GATE_QUEUE, HANDOVER, UNLOAD


def _gate(truck, seq, market, ts, kind):
    return {"truck_id": truck, "seq": seq, "event_type": kind, "system": "门岗",
            "market_id": market, "event_time": ts}


def _queue(truck, seq, market, ts):
    return _gate(truck, seq, market, ts, GATE_QUEUE)


def _unload(truck, seq, market, ts):
    return _gate(truck, seq, market, ts, UNLOAD)


def _hand(truck, seq, market, ts, from_seq, to_seq, from_kind=GATE_QUEUE, to_kind=UNLOAD):
    return {"truck_id": truck, "seq": seq, "event_type": HANDOVER, "system": "调度",
            "market_id": market, "event_time": ts,
            "from_seq": from_seq, "from_kind": from_kind, "to_seq": to_seq, "to_kind": to_kind}


def demo_truck_events():
    return [
        # 皖A-1001：南桥单市场直卸，60 分钟，归南桥 11-10。
        _queue("皖A-1001", 1, "南桥", "2026-11-10T06:00"),
        _unload("皖A-1001", 2, "南桥", "2026-11-10T07:00"),
        # 皖A-1002：南桥入队，经西苑交接后跨日开卸；130 分钟只归南桥 11-10。
        _queue("皖A-1002", 1, "南桥", "2026-11-10T22:30"),
        _hand("皖A-1002", 10, "西苑", "2026-11-10T23:10", 1, 20),
        _unload("皖A-1002", 20, "西苑", "2026-11-11T00:40"),
        # 皖A-1003：两次交接、三个市场，120 分钟归北仓前的首次入队市场南桥。
        _queue("皖A-1003", 1, "南桥", "2026-11-10T06:00"),
        _hand("皖A-1003", 10, "西苑", "2026-11-10T06:40", 1, 11, to_kind=HANDOVER),
        _hand("皖A-1003", 11, "北仓", "2026-11-10T07:20", 10, 20, from_kind=HANDOVER),
        _unload("皖A-1003", 20, "北仓", "2026-11-10T08:00"),
        # 皖A-1004：断链——交接单指向不存在的开卸，补报批次 1 可修复。
        _queue("皖A-1004", 1, "南桥", "2026-11-10T20:00"),
        _hand("皖A-1004", 10, "西苑", "2026-11-10T21:00", 1, 20),
        # 皖A-1005：交接分叉——同一入队被两份交接单引向不同开卸。
        _queue("皖A-1005", 1, "南桥", "2026-11-10T06:00"),
        _hand("皖A-1005", 10, "西苑", "2026-11-10T06:30", 1, 20),
        _hand("皖A-1005", 11, "北仓", "2026-11-10T06:40", 1, 21),
        _unload("皖A-1005", 20, "西苑", "2026-11-10T07:30"),
        _unload("皖A-1005", 21, "北仓", "2026-11-10T07:50"),
        # 皖A-1006：成环——两份交接互相指回。
        _queue("皖A-1006", 1, "南桥", "2026-11-10T06:00"),
        _hand("皖A-1006", 10, "西苑", "2026-11-10T06:30", 1, 11, to_kind=HANDOVER),
        _hand("皖A-1006", 11, "西苑", "2026-11-10T06:50", 10, 10,
              from_kind=HANDOVER, to_kind=HANDOVER),
        # 皖A-1007：断链——只有开卸没有入队。
        _unload("皖A-1007", 20, "北仓", "2026-11-11T09:00"),
        # 皖A-1008：次日另一市场日的正常直卸，验证市场日分组。
        _queue("皖A-1008", 1, "西苑", "2026-11-11T05:30"),
        _unload("皖A-1008", 2, "西苑", "2026-11-11T06:20"),
    ]


# 补报批次 1：补齐皖A-1004 的开卸，链修复后归南桥 2026-11-10，用时 150 分钟。
def demo_correction_batch():
    return {
        "batch_seq": 1,
        "note": "西苑补开 11 月 10 日夜班开卸记录，修复皖A-1004 断链。",
        "corrections": [
            _unload("皖A-1004", 20, "西苑", "2026-11-10T22:30"),
        ],
    }
