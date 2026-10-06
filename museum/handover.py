"""运输节点交接核验（按评估版本，纯函数）。

对批次内每件文物独立判定：
- 前节点在交接时刻前（含该时刻）的末条读数、后节点在交接时刻后（含该时刻）
  的首条读数均须存在；
- 两条读数各自距交接时刻不得超过该文物约定采样间隔；
- 两条读数之间不得跨该版本的缺测区间；
- 交接时刻不得处于未关闭（open）风险事件的起止闭区间内。
全部满足为「通过」(pass)，任一不满足为「待核」(review)。

预览与确认共用核验令牌（preview_token）：由批次、前后节点、交接时刻、
评估版本号与逐件结论规范化后取 SHA-256；确认时重算比对，当前版本或核验
结果（含事件关闭状态）一旦变化即失效，须重新预览。
"""
from __future__ import annotations

import hashlib
import json
from datetime import timezone

from .evaluate import METRIC_LABELS

PASS = "pass"
REVIEW = "review"
STATUS_LABELS = {PASS: "通过", REVIEW: "待核"}

PASS_REASON = ("符合交接条件：前后节点读数齐备且距交接时刻未超约定采样间隔，"
               "两读数间未跨缺测区间，交接时刻未处于未关闭风险事件内")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _reading_brief(reading, node, handover_dt):
    """读数摘要（含与交接时刻的距离秒数）；reading 为 None 时返回 None。"""
    if reading is None:
        return None
    return {
        "node_id": node["id"],
        "node_name": node["name"],
        "ts": _iso(reading["ts"]),
        "temperature": reading["temperature"],
        "humidity": reading["humidity"],
        "seconds_from_handover": abs((reading["ts"] - handover_dt).total_seconds()),
    }


def check_artifact(artifact, readings, gaps, events, handover_dt, prev_node, next_node):
    """单件文物的交接核验。

    artifact: 含 id、name、sample_interval（秒）；
    readings: 该文物当前版本读数列表，每项含 node_id、ts(datetime)、temperature、humidity；
    gaps: 该文物当前版本缺测区间，每项含 start_ts/end_ts(datetime)；
    events: 该文物当前版本事件，每项含 id、metric、status、start_ts/end_ts(datetime)；
    prev_node/next_node: {"id", "name"}。
    返回逐件结论字典（通过/待核、原因、对应读数与事件）。
    """
    interval = artifact["sample_interval"]
    before = [r for r in readings
              if r["node_id"] == prev_node["id"] and r["ts"] <= handover_dt]
    after = [r for r in readings
             if r["node_id"] == next_node["id"] and r["ts"] >= handover_dt]
    prev_reading = max(before, key=lambda r: r["ts"]) if before else None
    next_reading = min(after, key=lambda r: r["ts"]) if after else None

    reasons = []
    if prev_reading is None:
        reasons.append(f"前节点「{prev_node['name']}」在交接时刻前无读数")
    else:
        secs = (handover_dt - prev_reading["ts"]).total_seconds()
        if secs > interval:
            reasons.append(
                f"前节点末条读数距交接时刻 {secs:g} 秒，超过约定采样间隔 {interval} 秒")
    if next_reading is None:
        reasons.append(f"后节点「{next_node['name']}」在交接时刻后无读数")
    else:
        secs = (next_reading["ts"] - handover_dt).total_seconds()
        if secs > interval:
            reasons.append(
                f"后节点首条读数距交接时刻 {secs:g} 秒，超过约定采样间隔 {interval} 秒")

    spanned_gaps = []
    if prev_reading is not None and next_reading is not None:
        for g in gaps:
            if prev_reading["ts"] <= g["start_ts"] and g["end_ts"] <= next_reading["ts"]:
                spanned_gaps.append({
                    "start_ts": _iso(g["start_ts"]), "end_ts": _iso(g["end_ts"])})
                reasons.append(
                    f"前后读数之间跨缺测区间（{_iso(g['start_ts'])} 至 {_iso(g['end_ts'])}）")

    hit_events = []
    for e in events:
        if e.get("status") == "closed":
            continue
        if e["start_ts"] <= handover_dt <= e["end_ts"]:
            label = METRIC_LABELS.get(e["metric"], e["metric"])
            hit_events.append({
                "id": e["id"],
                "metric": e["metric"],
                "metric_label": label,
                "start_ts": _iso(e["start_ts"]),
                "end_ts": _iso(e["end_ts"]),
                "status": e.get("status", "open"),
            })
            reasons.append(
                f"交接时刻处于未关闭风险事件（{label}，"
                f"{_iso(e['start_ts'])} 至 {_iso(e['end_ts'])}）内")

    status = REVIEW if reasons else PASS
    return {
        "artifact_id": artifact["id"],
        "artifact_name": artifact["name"],
        "sample_interval": interval,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reasons": reasons or [PASS_REASON],
        "prev_reading": _reading_brief(prev_reading, prev_node, handover_dt),
        "next_reading": _reading_brief(next_reading, next_node, handover_dt),
        "spanned_gaps": spanned_gaps,
        "events": hit_events,
    }


def preview_token(batch_id, prev_node_id, next_node_id, handover_iso, version_no, items):
    """核验令牌：确认交接时重算比对，版本或核验结果变化即失效。"""
    norm = {
        "batch_id": batch_id,
        "prev_node_id": prev_node_id,
        "next_node_id": next_node_id,
        "handover_ts": handover_iso,
        "version": version_no,
        "items": items,
    }
    raw = json.dumps(norm, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
