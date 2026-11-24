import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.config.database import Base  # noqa: E402
from src.models import identity, jobs, source, trucks  # noqa: E402,F401
from src.domain.reconciliation import APPLIED, REJECTED  # noqa: E402
from src.domain.truck_demo import demo_correction_batch, demo_truck_events  # noqa: E402
from src.services import reconciliation as service  # noqa: E402

ENGINE = create_engine("sqlite://")
SESSION = sessionmaker(ENGINE)


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(ENGINE)

    def setUp(self):
        self.db = SESSION()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def test_demo_import_keeps_anomalies_and_summary_rules(self):
        result = service.import_events(self.db, demo_truck_events(), "seed")
        self.assertEqual(result["skipped_duplicates"], 0)
        # 重复导入应全部跳过，原始行不可变。
        again = service.import_events(self.db, demo_truck_events(), "seed")
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["skipped_duplicates"], result["total"])

        chains = service.current_chains(self.db)
        states = {truck: chain["state"] for truck, chain in chains.items()}
        self.assertEqual(states["皖A-1005"], "交接分叉")
        self.assertEqual(states["皖A-1006"], "成环")
        self.assertEqual(states["皖A-1007"], "断链")
        self.assertEqual(states["皖A-1004"], "断链")
        self.assertEqual(states["皖A-1002"], "正常")
        self.assertEqual(chains["皖A-1002"]["market_day"], "2026-11-10")

        rows, _ = service.market_day_rows(self.db)
        # 异常车次不计入；南桥 11-10 含 1001/1002/1003 三辆。
        nanqiao = next(row for row in rows
                       if row["market_id"] == "南桥" and row["market_day"] == "2026-11-10")
        self.assertEqual(nanqiao["truck_count"], 3)
        self.assertEqual(sorted(nanqiao["truck_ids"]),
                         ["皖A-1001", "皖A-1002", "皖A-1003"])
        # 整段用时 60+130+120=310，均值 103.33。
        self.assertEqual(nanqiao["total_duration_minutes"], 310.0)
        self.assertEqual(nanqiao["avg_duration_minutes"], 103.33)
        self.assertEqual(nanqiao["transfer_truck_count"], 2)
        # 跨日转场车没有算到西苑 11-11。
        xiyuan_11 = [row for row in rows
                     if row["market_id"] == "西苑" and row["market_day"] == "2026-11-11"]
        self.assertEqual([row["truck_ids"] for row in xiyuan_11], [["皖A-1008"]])

    def test_correction_batch_atomic_switch_and_late_rejection(self):
        service.import_events(self.db, demo_truck_events(), "seed")
        self.assertTrue(service.current_chains(self.db)["皖A-1004"]["excluded"])

        demo = demo_correction_batch()
        batch, applied = service.submit_correction_batch(
            self.db, demo["batch_seq"], demo["note"], demo["corrections"], "analyst")
        self.assertTrue(applied)
        self.assertEqual(batch.status, APPLIED)
        self.db.flush()

        after = service.current_chains(self.db)["皖A-1004"]
        self.assertEqual(after["state"], "正常")
        self.assertEqual(after["market_day"], "2026-11-10")
        self.assertEqual(after["duration_minutes"], 150.0)
        switch = next(item for item in batch.switches if item["truck_id"] == "皖A-1004")
        self.assertIsNone(switch["old_attribution"])
        self.assertEqual(switch["new_attribution"]["market_id"], "南桥")
        # 原始事件仍保留，补报事件并入链上。
        self.assertEqual(len(after["events"]), 3)

        # batch_seq 非正整数在引擎层即被拒。
        late = [{
            "truck_id": "皖A-1004", "seq": 20, "event_type": "unload", "system": "门岗",
            "market_id": "西苑", "event_time": "2026-11-12T08:00",
        }]
        with self.assertRaises(ValueError):
            service.submit_correction_batch(self.db, 0, "迟到的旧批", late, "analyst")
        # 同序号重复提交也拒绝。
        with self.assertRaises(ValueError):
            service.submit_correction_batch(self.db, 1, "重复批次", late, "analyst")

        # 新的合法批次（seq=2）可以继续生效，归属一次性切到新时间。
        batch2, applied2 = service.submit_correction_batch(
            self.db, 2, "二次修正开卸时间", late, "analyst")
        self.assertTrue(applied2)
        self.assertEqual(batch2.status, APPLIED)
        # 2026-11-10 20:00 -> 2026-11-12 08:00 = 2160 分钟。
        self.assertEqual(service.current_chains(self.db)["皖A-1004"]["duration_minutes"], 2160.0)

        # 无法把异常链修复为正常的补报整批拒绝，且不改变归属、不删原始记录。
        broken = [{
            "truck_id": "皖A-1007", "seq": 1, "event_type": "handover", "system": "调度",
            "market_id": "北仓", "event_time": "2026-11-11T08:00",
            "from_seq": 99, "to_seq": 20,
        }]
        rejected_batch, rejected_applied = service.submit_correction_batch(
            self.db, 4, "无法修复断链", broken, "analyst")
        self.assertFalse(rejected_applied)
        self.assertEqual(rejected_batch.status, REJECTED)
        self.assertTrue(rejected_batch.reject_reasons)
        self.assertTrue(service.current_chains(self.db)["皖A-1007"]["excluded"])

    def test_late_batch_cannot_override_newer_attribution(self):
        # 独立构造：批次 2 先生效后，序号更小的批次必须被拒。
        base = [
            {"truck_id": "Z1", "seq": 1, "event_type": "gate_queue", "system": "门岗",
             "market_id": "南桥", "event_time": "2026-11-10T23:30"},
            {"truck_id": "Z1", "seq": 2, "event_type": "unload", "system": "门岗",
             "market_id": "南桥", "event_time": "2026-11-11T00:10"},
        ]
        service.import_events(self.db, base, "seed")
        # 批次 2：入队与开卸一起改到北仓 11-11，归属市场日由南桥 11-10 切到北仓 11-11。
        move = [
            {"truck_id": "Z1", "seq": 1, "event_type": "gate_queue", "system": "门岗",
             "market_id": "北仓", "event_time": "2026-11-11T00:20"},
            {"truck_id": "Z1", "seq": 2, "event_type": "unload", "system": "门岗",
             "market_id": "北仓", "event_time": "2026-11-11T01:10"},
        ]
        batch2, applied = service.submit_correction_batch(self.db, 2, "改归北仓", move, "analyst")
        self.assertTrue(applied)
        chain = service.current_chains(self.db)["Z1"]
        self.assertEqual(chain["first_market"], "北仓")
        self.assertEqual(chain["market_day"], "2026-11-11")

        # 旧批次（seq=1）晚算完提交：validate 判定晚到，整批拒绝。
        old = [{
            "truck_id": "Z1", "seq": 1, "event_type": "gate_queue", "system": "门岗",
            "market_id": "南桥", "event_time": "2026-11-10T23:30",
        }]
        rejected, was_applied = service.submit_correction_batch(
            self.db, 1, "旧批晚到，想盖回南桥", old, "analyst")
        self.assertFalse(was_applied)
        self.assertEqual(rejected.status, REJECTED)
        self.assertTrue(any("晚到" in reason for reason in rejected.reject_reasons))
        # 归属仍是批次 2 的新归属，没有被盖回去。
        self.assertEqual(service.current_chains(self.db)["Z1"]["first_market"], "北仓")


if __name__ == "__main__":
    unittest.main()
