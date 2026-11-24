import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.domain.reconciliation import (  # noqa: E402
    ANOMALY_STATES, APPLIED, GATE_QUEUE, HANDOVER, UNLOAD,
    apply_corrections, attribution_map, build_chains, market_day_summary,
    plan_batch_switch, validate_batch,
)


def gate(truck, seq, market, ts, kind=GATE_QUEUE):
    return {"truck_id": truck, "seq": seq, "event_type": kind, "system": "门岗",
            "market_id": market, "event_time": ts}


def queue(truck, seq, market, ts):
    return gate(truck, seq, market, ts, GATE_QUEUE)


def unload(truck, seq, market, ts):
    return gate(truck, seq, market, ts, UNLOAD)


def handover(truck, seq, market, ts, from_seq, to_seq, from_kind=GATE_QUEUE, to_kind=UNLOAD):
    return {"truck_id": truck, "seq": seq, "event_type": HANDOVER, "system": "调度",
            "market_id": market, "event_time": ts,
            "from_seq": from_seq, "from_kind": from_kind, "to_seq": to_seq, "to_kind": to_kind}


class ChainTests(unittest.TestCase):
    def test_single_market_direct_chain(self):
        events = [
            queue("T1", 1, "南桥", "2026-11-10T07:00"),
            unload("T1", 2, "南桥", "2026-11-10T08:30"),
        ]
        chains = build_chains(events)
        chain = chains["T1"]
        self.assertEqual(chain["state"], "正常")
        self.assertFalse(chain["excluded"])
        self.assertEqual(chain["duration_minutes"], 90.0)
        self.assertEqual(chain["market_day"], "2026-11-10")
        self.assertEqual(chain["first_market"], "南桥")
        self.assertEqual(chain["transfer_count"], 0)

    def test_multi_market_transfer_credited_to_first_queue_day(self):
        # T2：南桥入队 -> 交接至西苑 -> 西苑开卸；跨日也只归首次入队市场日。
        events = [
            queue("T2", 1, "南桥", "2026-11-10T22:30"),
            handover("T2", 10, "西苑", "2026-11-10T23:10", from_seq=1, to_seq=20),
            unload("T2", 20, "西苑", "2026-11-11T00:40"),
        ]
        chains = build_chains(events)
        chain = chains["T2"]
        self.assertEqual(chain["state"], "正常")
        self.assertEqual(chain["transfer_count"], 1)
        # 整段 130 分钟，归属南桥 11-10，而不是西苑 11-11。
        self.assertEqual(chain["duration_minutes"], 130.0)
        self.assertEqual(chain["market_day"], "2026-11-10")
        self.assertEqual(chain["first_market"], "南桥")
        summary = market_day_summary(chains)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["market_id"], "南桥")
        self.assertEqual(summary[0]["market_day"], "2026-11-10")
        self.assertEqual(summary[0]["truck_count"], 1)
        self.assertEqual(summary[0]["avg_duration_minutes"], 130.0)

    def test_two_handovers_walk_in_order(self):
        events = [
            queue("T3", 1, "南桥", "2026-11-10T06:00"),
            handover("T3", 10, "西苑", "2026-11-10T06:40", 1, 11, to_kind=HANDOVER),
            handover("T3", 11, "北仓", "2026-11-10T07:20", 10, 20, from_kind=HANDOVER),
            unload("T3", 20, "北仓", "2026-11-10T08:00"),
        ]
        chain = build_chains(events)["T3"]
        self.assertEqual(chain["state"], "正常")
        self.assertEqual(chain["transfer_count"], 2)
        self.assertEqual([e["seq"] for e in chain["events"]], [1, 10, 11, 20])
        self.assertEqual(chain["duration_minutes"], 120.0)

    def test_broken_chain_missing_unload_excluded_but_kept(self):
        events = [queue("T4", 1, "南桥", "2026-11-10T06:00")]
        chain = build_chains(events)["T4"]
        self.assertEqual(chain["state"], "断链")
        self.assertTrue(chain["excluded"])
        self.assertIsNone(chain["duration_minutes"])
        self.assertIsNone(chain["market_day"])
        # 原始记录保留在链上供回查。
        self.assertEqual(len(chain["events"]), 1)
        self.assertEqual(market_day_summary(build_chains(events)), [])

    def test_broken_chain_handover_points_nowhere(self):
        events = [
            queue("T5", 1, "南桥", "2026-11-10T06:00"),
            handover("T5", 10, "西苑", "2026-11-10T07:00", from_seq=1, to_seq=99),
        ]
        chain = build_chains(events)["T5"]
        self.assertEqual(chain["state"], "断链")
        self.assertTrue(chain["excluded"])
        self.assertTrue(any("不存在" in reason for reason in chain["reasons"]))

    def test_fork_detected(self):
        # 同一入队被两份交接单引向不同开卸。
        events = [
            queue("T6", 1, "南桥", "2026-11-10T06:00"),
            handover("T6", 10, "西苑", "2026-11-10T06:30", 1, 20),
            handover("T6", 11, "北仓", "2026-11-10T06:40", 1, 21),
            unload("T6", 20, "西苑", "2026-11-10T07:30"),
            unload("T6", 21, "北仓", "2026-11-10T07:50"),
        ]
        chain = build_chains(events)["T6"]
        self.assertEqual(chain["state"], "交接分叉")
        self.assertTrue(chain["excluded"])
        # 原始事件全部保留（1 入队 + 2 交接 + 2 开卸）。
        self.assertEqual(len(chain["events"]), 5)

    def test_cycle_detected(self):
        # 两份交接互相指向，构成环。
        events = [
            queue("T7", 1, "南桥", "2026-11-10T06:00"),
            handover("T7", 10, "西苑", "2026-11-10T06:30", 1, 11, to_kind=HANDOVER),
            handover("T7", 11, "西苑", "2026-11-10T06:50", 10, 10, from_kind=HANDOVER, to_kind=HANDOVER),
        ]
        chain = build_chains(events)["T7"]
        self.assertEqual(chain["state"], "成环")
        self.assertTrue(chain["excluded"])

    def test_missing_queue_is_broken(self):
        events = [unload("T8", 20, "西苑", "2026-11-10T08:00")]
        chain = build_chains(events)["T8"]
        self.assertEqual(chain["state"], "断链")
        self.assertTrue(any("入队" in reason for reason in chain["reasons"]))

    def test_orphan_event_is_broken_and_kept(self):
        # 第二次入队/开卸没有交接挂接，不能被静默丢弃。
        events = [
            queue("T11", 1, "南桥", "2026-11-10T06:00"),
            unload("T11", 2, "南桥", "2026-11-10T07:00"),
            queue("T11", 3, "西苑", "2026-11-10T09:00"),
            unload("T11", 4, "西苑", "2026-11-10T09:40"),
        ]
        chain = build_chains(events)["T11"]
        self.assertEqual(chain["state"], "断链")
        self.assertTrue(chain["excluded"])
        self.assertTrue(any("孤立事件" in reason for reason in chain["reasons"]))
        self.assertEqual(len(chain["events"]), 4)

    def test_time_reversal_is_broken(self):
        # 链顺序贯通，但开卸时间早于首次入队。
        events = [
            queue("T12", 1, "南桥", "2026-11-10T08:00"),
            handover("T12", 10, "西苑", "2026-11-10T08:30", 1, 20),
            unload("T12", 20, "西苑", "2026-11-10T07:50"),
        ]
        chain = build_chains(events)["T12"]
        self.assertEqual(chain["state"], "断链")
        self.assertTrue(any("时间早于" in reason for reason in chain["reasons"]))


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        # T9 原链断链（交接指向不存在的开卸），补报后修复并改归市场日。
        self.base = [
            queue("T9", 1, "南桥", "2026-11-10T20:00"),
            handover("T9", 10, "西苑", "2026-11-10T21:00", 1, 20),
        ]

    def test_correction_repairs_and_switch_is_atomic(self):
        before = build_chains(self.base)
        self.assertTrue(before["T9"]["excluded"])
        corrections = [
            {"batch_seq": 1, "item_seq": 1,
             "event": unload("T9", 20, "西苑", "2026-11-10T22:00")},
        ]
        effective = apply_corrections(self.base, corrections)
        after = build_chains(effective)
        self.assertEqual(after["T9"]["state"], "正常")
        switch = plan_batch_switch(before, after, {"T9"})[0]
        self.assertTrue(switch["changed"])
        self.assertIsNone(switch["old_attribution"])  # 原来异常不计入
        self.assertEqual(switch["new_attribution"]["market_id"], "南桥")
        self.assertEqual(switch["new_attribution"]["market_day"], "2026-11-10")
        self.assertEqual(switch["new_attribution"]["duration_minutes"], 120.0)
        errors = validate_batch(1, corrections, 0, before, after)
        self.assertEqual(errors, [])

    def test_correction_moves_attribution_to_another_market_day(self):
        # 补报修正首次入队的时间/市场：从南桥 11-10 改为北仓 11-11。
        base = [
            queue("T10", 1, "南桥", "2026-11-10T23:30"),
            unload("T10", 2, "南桥", "2026-11-11T00:10"),
        ]
        before = build_chains(base)
        corrections = [
            {"batch_seq": 3, "item_seq": 1,
             "event": queue("T10", 1, "北仓", "2026-11-11T00:20")},
            {"batch_seq": 3, "item_seq": 2,
             "event": unload("T10", 2, "北仓", "2026-11-11T01:10")},
        ]
        after = build_chains(apply_corrections(base, corrections))
        switch = plan_batch_switch(before, after, {"T10"})[0]
        self.assertTrue(switch["changed"])
        self.assertEqual(switch["old_attribution"]["market_day"], "2026-11-10")
        self.assertEqual(switch["new_attribution"]["market_day"], "2026-11-11")
        self.assertEqual(switch["new_attribution"]["market_id"], "北仓")
        # 旧批次（seq 小于已生效的 3）必须被拒绝，不能盖回去。
        late = [{"batch_seq": 2, "item_seq": 1,
                 "event": queue("T10", 1, "南桥", "2026-11-10T23:30")}]
        late_after = build_chains(apply_corrections(base, late))
        errors = validate_batch(2, late, applied_max_seq=3,
                                before_chains=before, after_chains=late_after)
        self.assertTrue(any("晚到" in item for item in errors))

    def test_batch_that_keeps_anomaly_is_rejected_but_raw_kept(self):
        before = build_chains(self.base)
        # 补报仍是一条指向空事件的交接，链依旧断。
        corrections = [
            {"batch_seq": 1, "item_seq": 1,
             "event": handover("T9", 10, "西苑", "2026-11-10T21:05", 1, 88)},
        ]
        after = build_chains(apply_corrections(self.base, corrections))
        errors = validate_batch(1, corrections, 0, before, after)
        self.assertTrue(any("仍为" in item for item in errors))
        # 原始事件未被删除。
        self.assertEqual(len(self.base), 2)

    def test_summary_excludes_all_anomaly_states(self):
        events = [
            queue("OK", 1, "南桥", "2026-11-10T06:00"),
            unload("OK", 2, "南桥", "2026-11-10T07:00"),
            queue("BAD", 1, "南桥", "2026-11-10T06:00"),
        ]
        rows = market_day_summary(build_chains(events))
        self.assertEqual([row["truck_ids"] for row in rows], [["OK"]])
        self.assertEqual(attribution_map(build_chains(events)).keys(), {"OK"})

    def test_invalid_event_rejected(self):
        with self.assertRaises(ValueError):
            build_chains([{"truck_id": "X", "seq": 1, "event_type": HANDOVER,
                           "system": "门岗", "market_id": "南桥",
                           "event_time": "2026-11-10T06:00"}])
        with self.assertRaises(ValueError):
            build_chains([queue("X", 1, "南桥", "not-a-date")])


if __name__ == "__main__":
    unittest.main()
