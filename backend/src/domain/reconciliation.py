"""门岗入队/开卸记录与调度交接单的跨系统车次对账。

一条车次可能在多个市场间转场：门岗入队（gate）、调度交接单（handover）、
门岗开卸（unload）。本模块把三个来源的不可变事件按车次拼成转场链，
并实施下列业务口径：

- 正常链从「入队」起、到「开卸」止，中间允许零至多次调度交接；
- 交接分叉（同一事件后接多条边）、断链（缺入队/缺开卸、交接指向不存在事件）、
  成环（边构成环）时保留全部原始记录，涉事车次标记异常且不计入市场日汇总；
- 整段用时（首次入队至末次开卸）只计给首次入队所在的市场日，途经市场不分摊；
- 补报以批次为单位，先算齐原归属与新归属，再一次性切换；批次按序生效，
  迟到的旧批次不得覆盖已经生效的新归属。
"""

from datetime import datetime

GATE_QUEUE = "gate_queue"
HANDOVER = "handover"
UNLOAD = "unload"
EVENT_TYPES = (GATE_QUEUE, HANDOVER, UNLOAD)
SYSTEMS = ("门岗", "调度")

# 车次链状态
OK = "正常"
BROKEN = "断链"
FORK = "交接分叉"
CYCLE = "成环"
ANOMALY_STATES = (BROKEN, FORK, CYCLE)

# 补报批次状态
PENDING = "pending"
APPLIED = "applied"
REJECTED = "rejected"


class ReconciliationError(ValueError):
    """补报批次或事件数据不满足对账口径。"""


def _parse_ts(value, label):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ReconciliationError(f"{label}的时间须使用不带时区的本地时间。")
        return value
    if not isinstance(value, str):
        raise ReconciliationError(f"{label}的时间须为 ISO 8601 字符串。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ReconciliationError(f"{label}的时间格式有误：{value}") from None
    if parsed.tzinfo is not None:
        raise ReconciliationError(f"{label}的时间须使用不带时区的本地时间。")
    return parsed


def validate_event(event):
    """校验单条跨系统事件，返回规范化副本。"""
    if not isinstance(event, dict):
        raise ReconciliationError("事件须为对象。")
    required = ("truck_id", "seq", "event_type", "system", "market_id", "event_time")
    missing = [key for key in required if key not in event or event[key] in (None, "")]
    if missing:
        raise ReconciliationError(f"事件缺少必填字段：{'、'.join(missing)}。")
    truck = event["truck_id"]
    if not isinstance(truck, str) or not 1 <= len(truck.strip()) <= 64:
        raise ReconciliationError("车次标识须为 1–64 个字符。")
    seq = event["seq"]
    if type(seq) is not int or seq < 0:
        raise ReconciliationError("事件流水号须为非负整数。")
    kind = event["event_type"]
    if kind not in EVENT_TYPES:
        raise ReconciliationError(f"未知事件类型：{kind}。")
    system = event["system"]
    if system not in SYSTEMS:
        raise ReconciliationError("事件来源系统须为「门岗」或「调度」。")
    if kind == HANDOVER and system != "调度":
        raise ReconciliationError("调度交接单只能来源于调度系统。")
    if kind in (GATE_QUEUE, UNLOAD) and system != "门岗":
        raise ReconciliationError("入队与开卸记录只能来源于门岗系统。")
    market = event["market_id"]
    if not isinstance(market, str) or not 1 <= len(market.strip()) <= 300:
        raise ReconciliationError("市场名称须为 1–300 个字符。")
    stamp = _parse_ts(event["event_time"], f"车次 {truck.strip()} 流水 {seq}")
    normalized = {
        "truck_id": truck.strip(),
        "seq": seq,
        "event_type": kind,
        "system": system,
        "market_id": market.strip(),
        "event_time": stamp,
        "raw_time": event["raw_time"] if "raw_time" in event else event["event_time"],
    }
    if kind == HANDOVER:
        for link_key in ("from_seq", "to_seq"):
            if type(event.get(link_key)) is not int or event[link_key] < 0:
                raise ReconciliationError(f"交接单 #{seq} 的 {link_key} 须为非负整数。")
        from_kind = event.get("from_kind", GATE_QUEUE)
        to_kind = event.get("to_kind", UNLOAD)
        if from_kind not in (GATE_QUEUE, HANDOVER):
            raise ReconciliationError(f"交接单 #{seq} 的 from_kind 只能是入队或交接。")
        if to_kind not in (HANDOVER, UNLOAD):
            raise ReconciliationError(f"交接单 #{seq} 的 to_kind 只能是交接或开卸。")
        normalized.update(from_seq=event["from_seq"], from_kind=from_kind,
                          to_seq=event["to_seq"], to_kind=to_kind)
    return normalized


def _group_events(events):
    grouped = {}
    for event in events:
        grouped.setdefault(event["truck_id"], []).append(event)
    for truck, items in grouped.items():
        seen = set()
        for item in items:
            key = (item["event_type"], item["seq"])
            if key in seen:
                raise ReconciliationError(f"车次 {truck} 存在重复事件：{item['event_type']}#{item['seq']}。")
            seen.add(key)
        items.sort(key=lambda item: (item["event_time"], item["event_type"], item["seq"]))
    return grouped


def _build_one_chain(truck, items):
    """按事件类型构造边并判定链状态。返回 (state, reasons, ordered_events)。"""
    by_key = {(item["event_type"], item["seq"]): item for item in items}
    out_edges, in_edges = {}, {}
    reasons = []

    def add_edge(src, dst):
        out_edges.setdefault(src, []).append(dst)
        in_edges.setdefault(dst, []).append(src)

    queues = sorted((e for e in items if e["event_type"] == GATE_QUEUE),
                    key=lambda e: (e["event_time"], e["seq"]))
    unloads = sorted((e for e in items if e["event_type"] == UNLOAD),
                     key=lambda e: (e["event_time"], e["seq"]))
    handovers = [e for e in items if e["event_type"] == HANDOVER]

    # 调度交接单：from_seq 指向上一事件，to_seq 指向下一事件。
    for hand in handovers:
        src_key = (GATE_QUEUE, hand["from_seq"]) if hand.get("from_kind", GATE_QUEUE) == GATE_QUEUE \
            else (HANDOVER, hand["from_seq"])
        dst_key = (HANDOVER, hand["seq"])
        if src_key not in by_key:
            reasons.append(f"交接单 #{hand['seq']} 的上游事件 {src_key[0]}#{src_key[1]} 不存在（断链）。")
        else:
            add_edge(src_key, dst_key)
        next_kind = hand.get("to_kind")
        if next_kind is None:
            # 默认交接后要么还有交接，要么开卸；按常见口径落到开卸。
            next_kind = UNLOAD
        nxt_key = (next_kind, hand["to_seq"])
        if nxt_key not in by_key:
            reasons.append(f"交接单 #{hand['seq']} 的下游事件 {next_kind}#{hand['to_seq']} 不存在（断链）。")
        else:
            add_edge(dst_key, nxt_key)

    # 无交接时，首次入队直接到开卸。
    if not handovers:
        if queues and unloads:
            add_edge((GATE_QUEUE, queues[0]["seq"]), (UNLOAD, unloads[0]["seq"]))

    state = OK
    # 分叉：任一节点出边或入边不唯一（同一交接被多份单据引用等）。
    for key, targets in out_edges.items():
        if len(set(targets)) > 1:
            state = FORK
            reasons.append(f"事件 {key[0]}#{key[1]} 后接 {len(set(targets))} 条不同去向（交接分叉）。")
    for key, sources in in_edges.items():
        if len(set(sources)) > 1:
            state = FORK
            reasons.append(f"事件 {key[0]}#{key[1]} 有 {len(set(sources))} 个不同来源（交接分叉）。")

    # 成环：沿出边检测回到已访问节点。
    def has_cycle():
        visiting, done = set(), set()

        def dfs(node):
            visiting.add(node)
            for nxt in out_edges.get(node, ()):
                if nxt in visiting:
                    return True
                if nxt not in done and dfs(nxt):
                    return True
            visiting.discard(node)
            done.add(node)
            return False

        return any(dfs(node) for node in list(out_edges))

    if has_cycle():
        state = CYCLE
        reasons.append("转场边构成环（成环）。")

    # 断链：缺入队、缺开卸，或交接引用缺失（reasons 已记录）。
    if not queues:
        reasons.append("缺少门岗入队记录（断链）。")
    if not unloads:
        reasons.append("缺少门岗开卸记录（断链）。")
    if state == OK and reasons:
        state = BROKEN

    # 正常链还要求时间单调：每次事件不早于上一事件，且全部事件都挂在链上。
    ordered = list(items)
    if state == OK:
        chain_order = _walk_chain(out_edges, queues[0], by_key)
        if chain_order is None:
            state = BROKEN
            reasons.append("转场链无法从首次入队贯通至开卸（断链）。")
        elif len(chain_order) != len(items):
            state = BROKEN
            reached = {(event["event_type"], event["seq"]) for event in chain_order}
            orphaned = [f"{event['event_type']}#{event['seq']}"
                        for event in items if (event["event_type"], event["seq"]) not in reached]
            reasons.append(f"存在未挂接到转场链的孤立事件：{'、'.join(orphaned)}（断链）。")
        else:
            ordered = chain_order
            for earlier, later in zip(ordered, ordered[1:]):
                if later["event_time"] < earlier["event_time"]:
                    state = BROKEN
                    reasons.append(
                        f"{later['event_type']}#{later['seq']} 时间早于上一事件（断链）。")
                    break
    return state, reasons, ordered


def _walk_chain(out_edges, first_queue, by_key):
    node = (GATE_QUEUE, first_queue["seq"])
    chain, visited = [], set()
    while node in by_key:
        if node in visited:
            return None
        visited.add(node)
        chain.append(by_key[node])
        targets = out_edges.get(node)
        if not targets:
            break
        node = targets[0]
    if not chain or chain[-1]["event_type"] != UNLOAD:
        return None
    return chain


def build_chains(events):
    """把原始事件按车次组装为转场链。返回 {truck_id: chain_view}。"""
    normalized = [validate_event(event) for event in events]
    grouped = _group_events(normalized)
    chains = {}
    for truck, items in grouped.items():
        state, reasons, ordered = _build_one_chain(truck, items)
        queues = [e for e in ordered if e["event_type"] == GATE_QUEUE]
        unloads = [e for e in ordered if e["event_type"] == UNLOAD]
        first_queue = queues[0] if queues else None
        last_unload = unloads[-1] if unloads else None
        duration = None
        market_day = None
        if state == OK:
            duration = (last_unload["event_time"] - first_queue["event_time"]).total_seconds() / 60
            if duration < 0:
                state = BROKEN
                reasons.append("末次开卸早于首次入队（断链）。")
                duration = None
            else:
                market_day = first_queue["event_time"].date().isoformat()
        transfers = sum(1 for event in ordered if event["event_type"] == HANDOVER)
        chains[truck] = {
            "truck_id": truck,
            "state": state,
            "excluded": state in ANOMALY_STATES,
            "reasons": reasons,
            "market_day": market_day,
            "first_market": first_queue["market_id"] if first_queue else None,
            "transfer_count": transfers,
            "duration_minutes": round(duration, 2) if duration is not None else None,
            "events": [_public_event(event) for event in ordered],
        }
    return chains


def _public_event(event):
    view = {key: event[key] for key in ("truck_id", "seq", "event_type", "system", "market_id")}
    view["event_time"] = event["raw_time"]
    for extra in ("from_seq", "from_kind", "to_seq", "to_kind"):
        if extra in event:
            view[extra] = event[extra]
    return view


def apply_corrections(events, corrections):
    """在原始事件之上叠加按批次排序的补报，返回生效后的事件副本。

    corrections 为列表，每项含 batch_seq 与事件字段；同一批次整体生效，
    批次号小的先处理。补报事件按 (event_type, seq) 覆盖同车次同键事件，
    或在该键不存在时新增。
    """
    effective = [dict(event) for event in events]
    ordered = sorted(corrections, key=lambda item: (item["batch_seq"], item.get("item_seq", 0)))
    for correction in ordered:
        payload = dict(correction["event"])
        normalized = validate_event(payload)
        replaced = False
        for index, event in enumerate(effective):
            if (event["truck_id"] == normalized["truck_id"]
                    and (event["event_type"], event["seq"])
                    == (normalized["event_type"], normalized["seq"])):
                effective[index] = normalized
                replaced = True
                break
        if not replaced:
            effective.append(normalized)
    return effective


def market_day_summary(chains):
    """按首次入队的市场日汇总，异常车次不计入。"""
    summary = {}
    for chain in chains.values():
        if chain["excluded"]:
            continue
        key = (chain["first_market"], chain["market_day"])
        bucket = summary.setdefault(key, {
            "market_id": chain["first_market"],
            "market_day": chain["market_day"],
            "truck_count": 0,
            "total_duration_minutes": 0.0,
            "transfer_truck_count": 0,
            "truck_ids": [],
        })
        bucket["truck_count"] += 1
        bucket["total_duration_minutes"] += chain["duration_minutes"]
        bucket["transfer_truck_count"] += 1 if chain["transfer_count"] else 0
        bucket["truck_ids"].append(chain["truck_id"])
    rows = []
    for bucket in summary.values():
        count = bucket["truck_count"]
        rows.append({
            **bucket,
            "truck_ids": sorted(bucket["truck_ids"]),
            "avg_duration_minutes": round(bucket["total_duration_minutes"] / count, 2),
            "total_duration_minutes": round(bucket["total_duration_minutes"], 2),
        })
    rows.sort(key=lambda row: (row["market_day"], row["market_id"]))
    return rows


def attribution_map(chains):
    """返回 {truck_id: (market_id, market_day)}，仅含正常计入的车次。"""
    return {
        truck: (chain["first_market"], chain["market_day"])
        for truck, chain in chains.items()
        if not chain["excluded"]
    }


def plan_batch_switch(before_chains, after_chains, truck_ids):
    """对比补报前后的归属，生成一次性切换的差分清单。"""
    switches = []
    for truck in sorted(truck_ids):
        before = before_chains.get(truck)
        after = after_chains.get(truck)
        old_attr = None
        new_attr = None
        if before and not before["excluded"]:
            old_attr = {"market_id": before["first_market"], "market_day": before["market_day"],
                        "duration_minutes": before["duration_minutes"]}
        if after and not after["excluded"]:
            new_attr = {"market_id": after["first_market"], "market_day": after["market_day"],
                        "duration_minutes": after["duration_minutes"]}
        switches.append({
            "truck_id": truck,
            "old_attribution": old_attr,
            "new_attribution": new_attr,
            "changed": old_attr != new_attr,
            "before_state": before["state"] if before else "无记录",
            "after_state": after["state"] if after else "无记录",
        })
    return switches


def validate_batch(batch_seq, corrections, applied_max_seq, before_chains, after_chains):
    """批次提交前的口径校验，返回拒绝原因列表（空列表表示可生效）。"""
    errors = []
    if type(batch_seq) is not int or batch_seq <= 0:
        errors.append("补报批次号须为正整数。")
    elif applied_max_seq and batch_seq <= applied_max_seq:
        errors.append(
            f"批次 {batch_seq} 晚到：批次 {applied_max_seq} 已生效，旧批次不能覆盖新归属。")
    if not corrections:
        errors.append("补报批次至少包含一条事件。")
    touched = set()
    for correction in corrections:
        event = correction.get("event", {})
        truck = event.get("truck_id")
        if truck:
            touched.add(str(truck).strip())
    for truck in touched:
        before = before_chains.get(truck)
        after = after_chains.get(truck)
        # 异常车次在补报把链路修复为正常前，仍不计入；若补报后依旧异常则拒绝该批次，
        # 避免无意义写入，原始记录始终保留。
        if after and after["excluded"]:
            errors.append(f"车次 {truck} 补报后仍为「{after['state']}」，暂不计入，请核对交接依据。")
        if after is None:
            errors.append(f"车次 {truck} 在补报后无任何事件。")
    return errors
