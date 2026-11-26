#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数字空间记忆机 · 第一版实验（V0.1）

分层设计：
  热层（正本）  = 本目录 events/ 事件流（JSONL 追加写）+ memory.db 索引
  快照（对账）  = snapshots/ 定期状态快照（JSON + SHA256 指纹）
  冷层（预留）  = 打包后推送魔搭私有仓（本版只做本地打包，不联网）

核心闭环：
  record  -> 事件追加进事件流并写入索引
  snapshot-> 当前状态拍快照并记指纹
  diff    -> 两个快照之间算差分
  replay  -> 从任一快照 + 事件流重建指定时刻的状态（可核验）
  pack    -> 打包冷备（供后续推魔搭）

仅依赖 Python 标准库。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
EVENTS_DIR = os.path.join(BASE, "events")
SNAPSHOTS_DIR = os.path.join(BASE, "snapshots")
PACKS_DIR = os.path.join(BASE, "packs")
DB_PATH = os.path.join(BASE, "memory.db")

# 时间分层（秒）：7 天内细粒度快照全留，90 天内保留整点快照，更久只留事件册与指纹
TIER_FINE = 7 * 86400
TIER_MID = 90 * 86400


def now_ts():
    return time.time()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def snap_hour(ts):
    """快照文件名按整点对齐，便于分层与去重。"""
    return datetime.fromtimestamp(ts).strftime("%Y%m%d%H")


def ensure_dirs():
    for d in (EVENTS_DIR, SNAPSHOTS_DIR, PACKS_DIR):
        os.makedirs(d, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts REAL NOT NULL,
               entity TEXT NOT NULL,
               kind TEXT NOT NULL,
               payload TEXT NOT NULL,
               fingerprint TEXT NOT NULL
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_entity_ts ON events(entity, ts)"
    )
    return conn


def state_fingerprint(state: dict) -> str:
    """对状态做确定性指纹：排序后 JSON 序列化再取 SHA256。"""
    blob = json.dumps(state, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------- 事件流（正本） ----------

def event_file_path(ts):
    """事件流按天分文件，避免单文件无限膨胀。"""
    return os.path.join(
        EVENTS_DIR, datetime.fromtimestamp(ts).strftime("events-%Y%m%d.jsonl")
    )


def record(entity: str, kind: str, payload: dict, source: str = "internal") -> dict:
    """source: internal=体系内部动作（正本，完整）；external=外部观测（采样，带证据）"""
    if source not in ("internal", "external"):
        raise ValueError("source 必须是 internal 或 external")
    ts = now_ts()
    event = {
        "ts": ts,
        "entity": entity,
        "kind": kind,
        "source": source,
        "payload": payload,
    }
    line = json.dumps(event, ensure_ascii=False)
    os.makedirs(EVENTS_DIR, exist_ok=True)  # 全新部署时目录尚不存在，按需自建
    with open(event_file_path(ts), "a", encoding="utf-8") as f:
        f.write(line + "\n")

    conn = get_db()
    conn.execute(
        "INSERT INTO events (ts, entity, kind, payload, fingerprint) VALUES (?,?,?,?,?)",
        (ts, entity, kind, json.dumps(payload, ensure_ascii=False), state_fingerprint(event)),
    )
    conn.commit()
    conn.close()
    return event


def read_events(since_ts: float = 0.0, until_ts: float = None):
    """顺序读事件流（正本），返回 (ts, event) 列表。"""
    if until_ts is None:
        until_ts = now_ts()
    out = []
    if not os.path.isdir(EVENTS_DIR):
        return out
    for name in sorted(os.listdir(EVENTS_DIR)):
        if not name.startswith("events-") or not name.endswith(".jsonl"):
            continue
        path = os.path.join(EVENTS_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if since_ts < ev["ts"] <= until_ts:
                    out.append(ev)
    out.sort(key=lambda e: e["ts"])
    return out


# ---------- 状态与快照 ----------

def apply_event(state: dict, ev: dict) -> dict:
    """事件如何改变状态。V0.1 用最简语义：
       kind=upsert -> state[entity] = payload
       kind=delete -> state.pop(entity, None)
       其他 kind    -> 记录到 state.setdefault('_log', []) 便于观察
    """
    entity, kind, payload = ev["entity"], ev["kind"], ev["payload"]
    if kind in ("upsert", "observed"):
        # observed = 外部实体的认知状态更新（我们观测到的样子）
        state[entity] = payload
    elif kind == "delete":
        state.pop(entity, None)
    else:
        state.setdefault("_log", []).append(
            {"ts": ev["ts"], "entity": entity, "kind": kind, "payload": payload}
        )
    return state


def current_state() -> dict:
    state = {}
    for ev in read_events():
        apply_event(state, ev)
    return state


def snapshot_path(hour_key: str) -> str:
    return os.path.join(SNAPSHOTS_DIR, f"snap-{hour_key}.json")


def take_snapshot() -> dict:
    ts = now_ts()
    state = current_state()
    hour_key = snap_hour(ts)
    # 同一小时重复拍摄：带序号保留多份，不覆盖
    path = snapshot_path(hour_key)
    seq = 1
    while os.path.exists(path):
        seq += 1
        path = os.path.join(SNAPSHOTS_DIR, f"snap-{hour_key}-{seq:02d}.json")
    snap = {
        "hour_key": os.path.basename(path)[len("snap-"):-len(".json")],
        "taken_at": ts,
        "taken_at_str": fmt_ts(ts),
        "fingerprint": state_fingerprint(state),
        "state": state,
    }
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)  # 全新部署时目录尚不存在，按需自建
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    return snap


def load_snapshot(hour_key: str):
    path = snapshot_path(hour_key)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def latest_snapshot_before(ts: float):
    """找实际拍摄时间（taken_at）在 ts 之前（含）最近的快照。
       同一小时重复拍摄时保留最新一份，文件名带序号避免覆盖。"""
    best, best_taken = None, -1.0
    if not os.path.isdir(SNAPSHOTS_DIR):
        return None
    for name in sorted(os.listdir(SNAPSHOTS_DIR)):
        if not name.startswith("snap-") or not name.endswith(".json"):
            continue
        with open(os.path.join(SNAPSHOTS_DIR, name), "r", encoding="utf-8") as f:
            snap = json.load(f)
        if snap.get("taken_at", -1.0) <= ts and snap.get("taken_at", -1.0) > best_taken:
            best, best_taken = snap, snap["taken_at"]
    return best


# ---------- 差分与回放 ----------

def diff_snapshots(old: dict, new: dict) -> dict:
    """对比两个快照的状态，输出增/改/删。"""
    a, b = old.get("state", {}), new.get("state", {})
    added = {k: b[k] for k in b.keys() - a.keys()}
    removed = {k: a[k] for k in a.keys() - b.keys()}
    changed = {
        k: {"from": a[k], "to": b[k]}
        for k in a.keys() & b.keys()
        if a[k] != b[k]
    }
    return {
        "from_hour": old.get("hour_key"),
        "to_hour": new.get("hour_key"),
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def replay(at_ts: float = None) -> dict:
    """从 at_ts 之前最近的快照出发，重放事件流，重建该时刻状态。
       快照指纹校验通过才继续，保证可核验。"""
    if at_ts is None:
        at_ts = now_ts()
    snap = latest_snapshot_before(at_ts)
    if snap is not None:
        fp = state_fingerprint(snap["state"])
        if fp != snap["fingerprint"]:
            raise RuntimeError(f"快照指纹不匹配：{snap['hour_key']}，疑似被篡改或损坏")
        state = json.loads(json.dumps(snap["state"]))  # 深拷贝
        base_ts = snap["taken_at"]
    else:
        state, base_ts = {}, 0.0
    for ev in read_events(since_ts=base_ts, until_ts=at_ts):
        apply_event(state, ev)
    return {"at": at_ts, "at_str": fmt_ts(at_ts), "state": state,
            "fingerprint": state_fingerprint(state), "from_snapshot": snap["hour_key"] if snap else None}


# ---------- 冷备打包 ----------

def pack() -> str:
    ts = now_ts()
    tag = datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")
    out = os.path.join(PACKS_DIR, f"memory-pack-{tag}.json")
    manifest = {
        "packed_at": ts,
        "packed_at_str": fmt_ts(ts),
        "events_files": sorted(os.listdir(EVENTS_DIR)) if os.path.isdir(EVENTS_DIR) else [],
        "snapshots": {},
        "note": "本包为冷备正本镜像，可推送魔搭私有仓 zongyuanroot/yuanji-works",
    }
    for name in sorted(os.listdir(SNAPSHOTS_DIR)):
        path = os.path.join(SNAPSHOTS_DIR, name)
        with open(path, "rb") as f:
            manifest["snapshots"][name] = hashlib.sha256(f.read()).hexdigest()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return out


# ---------- 分层清理（V0.1 先只报告不删除，避免误伤） ----------

def retention_report() -> dict:
    now = now_ts()
    keep, demote, drop = [], [], []
    if os.path.isdir(SNAPSHOTS_DIR):
        for name in sorted(os.listdir(SNAPSHOTS_DIR)):
            if not (name.startswith("snap-") and name.endswith(".json")):
                continue
            hour_key = name[len("snap-"):-len(".json")]
            try:
                snap_ts = datetime.strptime(hour_key, "%Y%m%d%H").timestamp()
            except ValueError:
                continue
            age = now - snap_ts
            if age <= TIER_FINE:
                keep.append(hour_key)
            elif age <= TIER_MID:
                demote.append(hour_key)
            else:
                drop.append(hour_key)
    return {"keep_fine": keep, "keep_mid": demote, "candidates_drop": drop}


def main():
    parser = argparse.ArgumentParser(description="数字空间记忆机 V0.1")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record", help="记录一条事件")
    p.add_argument("entity")
    p.add_argument("kind")
    p.add_argument("payload", help='JSON 字符串，如 \'{"status":"ok"}\'')
    p.add_argument("--source", default="internal", choices=["internal", "external"],
                   help="internal=内部动作，external=外部观测")

    sub.add_parser("snapshot", help="对当前状态拍快照")
    sub.add_parser("state", help="查看当前重建状态与指纹")
    sub.add_parser("pack", help="打包冷备")
    sub.add_parser("retention", help="查看时间分层清理报告")

    p = sub.add_parser("diff", help="对比两个整点快照")
    p.add_argument("old_hour")
    p.add_argument("new_hour")

    p = sub.add_parser("replay", help="重建指定时刻状态（默认现在）")
    p.add_argument("at", nargs="?", help='时刻，如 "2026-11-26 08:00:00"')

    args = parser.parse_args()

    ensure_dirs()
    if args.cmd == "record":
        ev = record(args.entity, args.kind, json.loads(args.payload), source=args.source)
        print(json.dumps({"ok": True, "ts": fmt_ts(ev["ts"]), "entity": ev["entity"],
                          "kind": ev["kind"], "source": ev["source"]}, ensure_ascii=False))
    elif args.cmd == "snapshot":
        snap = take_snapshot()
        print(json.dumps({"ok": True, "hour": snap["hour_key"],
                          "fingerprint": snap["fingerprint"],
                          "entities": len([k for k in snap["state"] if k != "_log"])},
                         ensure_ascii=False))
    elif args.cmd == "state":
        r = replay()
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "diff":
        old, new = load_snapshot(args.old_hour), load_snapshot(args.new_hour)
        if old is None or new is None:
            print(json.dumps({"ok": False, "error": "快照不存在"}, ensure_ascii=False))
            sys.exit(1)
        print(json.dumps(diff_snapshots(old, new), ensure_ascii=False, indent=2))
    elif args.cmd == "replay":
        at = datetime.strptime(args.at, "%Y-%m-%d %H:%M:%S").timestamp() if args.at else None
        print(json.dumps(replay(at), ensure_ascii=False, indent=2))
    elif args.cmd == "pack":
        print(json.dumps({"ok": True, "pack": os.path.relpath(pack(), BASE)}, ensure_ascii=False))
    elif args.cmd == "retention":
        print(json.dumps(retention_report(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
