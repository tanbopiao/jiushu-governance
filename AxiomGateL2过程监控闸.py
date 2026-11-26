#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AxiomGate L2 迭代过程动态拦截闸 · 单文件实现 V1.0
对应《AxiomGate三层拦截围栏部署白皮书》L2 闸的可执行子集。

职责（与白皮书 L2 对应）：
  以旁路挂载方式监控 RSI 迭代过程，逐批摄入训练遥测：
    - 轻量规则快速过滤：输出样本命中违禁内容模式 → 立即超标
    - 对齐漂移检测：对齐分相对基线持续下滑，两级阈值（警戒 / 冻结）
    - 滑动窗口风险趋势：近 N 批风险均值超警戒 / 超冻结线
    - 风险模型检测：**可插拔接口，本文件不附带任何模型**（如实声明）
  三级动作：
    NORMAL 正常放行，仅日志留存
    WARN   标记风险，持续观测
    FREEZE 立即冻结训练，自动触发快照回调（对接快照基线管理器）
  失联兜底（fail-closed）：
    SafetyLink 心跳 —— 训练侧每次循环必须询问；心跳过期一律返回 PAUSE。
    围栏挂掉 = 训练必须停，杜绝失控自进化。

边界（如实声明）：
  - 本文件是旁路监控框架与决策逻辑，**不包含真实风险评分模型**；
    set_risk_model() 供接入任何外部评分器，未接入时仅规则与统计检测生效
  - "每批次训练样本级监控"需要训练框架输出遥测；对接成本取决于框架，
    白皮书"零改造"的说法在真实训练集群中通常不成立
  - 阈值均为示例值，投产前须按业务核定
  - 依赖：Python >= 3.7 标准库，零第三方依赖
"""

import hashlib
import json
import os
import time
import uuid
from datetime import datetime

__version__ = "1.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 哈希链账本（与系列前作同构）
# ---------------------------------------------------------------------------
class HashLedger:
    def __init__(self, path: str):
        self.path = path
        self._entries = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._entries = json.load(f)

    def append(self, event: str, payload: dict = None) -> dict:
        prev = self._entries[-1]["hash"] if self._entries else "GENESIS"
        entry = {
            "index": len(self._entries),
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "payload": payload or {},
            "prev_hash": prev,
        }
        entry["hash"] = _sha(_canon(entry))
        self._entries.append(entry)
        self._flush()
        return entry

    def verify(self) -> dict:
        prev = "GENESIS"
        for i, entry in enumerate(self._entries):
            expect = {k: v for k, v in entry.items() if k != "hash"}
            if entry.get("prev_hash") != prev or entry.get("hash") != _sha(_canon(expect)):
                return {"chain_valid": False, "chain_length": len(self._entries), "broken_at": i}
            prev = entry["hash"]
        return {"chain_valid": True, "chain_length": len(self._entries), "broken_at": None}

    def _flush(self):
        tmp = self.path + ".tmp-" + uuid.uuid4().hex[:8]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# 本地持久层快照（与总控内核同构）：监控进度重启不丢
# ---------------------------------------------------------------------------
class SnapshotCorruptError(Exception):
    """快照完整性校验失败（被改动或损坏），直接报错不静默。"""


class SnapshotStore:
    def __init__(self, path: str, kind: str):
        self.path = path
        self.kind = kind

    def save(self, state: dict):
        payload = {"kind": self.kind, "saved_at": datetime.now().isoformat(timespec="seconds"),
                   "state": state}
        payload["integrity"] = _sha(_canon(payload))
        tmp = self.path + ".tmp-" + uuid.uuid4().hex[:8]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def load(self):
        if not os.path.exists(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("kind") != self.kind:
            raise SnapshotCorruptError("快照类型不符: %s" % self.path)
        integrity = payload.pop("integrity", None)
        if integrity != _sha(_canon(payload)):
            raise SnapshotCorruptError("快照完整性校验失败（被改动或损坏）: %s" % self.path)
        return payload["state"]


# ---------------------------------------------------------------------------
# 失联兜底：训练侧必须持续询问，心跳过期一律 PAUSE（fail-closed）
# ---------------------------------------------------------------------------
class SafetyLink:
    """围栏与训练侧之间的安全联杆。围栏正常时训练侧定期收到心跳；
    心跳停止超过 ttl 秒，check() 永远返回 PAUSE，直到心跳恢复。"""

    def __init__(self, ttl_seconds: float = 10.0):
        self.ttl_seconds = ttl_seconds
        self._last_beat = 0.0

    def beat(self, now: float = None):
        self._last_beat = now if now is not None else time.time()

    def check(self, now: float = None) -> str:
        return "RUN" if (now if now is not None else time.time()) - self._last_beat <= self.ttl_seconds else "PAUSE"


# ---------------------------------------------------------------------------
# 滑动窗口风险趋势
# ---------------------------------------------------------------------------
class SlidingWindow:
    def __init__(self, size: int):
        self.size = size
        self._values = []

    def push(self, value: float):
        self._values.append(float(value))
        if len(self._values) > self.size:
            self._values.pop(0)

    @property
    def mean(self) -> float:
        return sum(self._values) / len(self._values) if self._values else 0.0

    def __len__(self):
        return len(self._values)


# ---------------------------------------------------------------------------
# L2 旁路监控闸
# ---------------------------------------------------------------------------
class SidecarMonitor:
    """
    用法（训练侧旁路）：
        mon = SidecarMonitor("rsi-001", snapshot_hook=make_snapshot)
        for batch in training_loop:
            decision = mon.ingest_batch(batch)
            if decision["action"] == "FREEZE":
                training_loop.pause()          # 立即冻结
            elif decision["action"] == "WARN":
                training_loop.flag_risk()      # 标记观测
    batch 遥测字段：
        batch_id: str
        sampled_outputs: list[str]   本批抽样输出文本（由训练框架侧采样上报）
        alignment_score: float 0~1   本批对齐评测分（对照基线）
    """

    DEFAULT_RULES = {
        "banned_output_patterns": ["绕过对齐", "越狱提示", "隐瞒指令", "欺骗评估"],
        "warn_risk": 0.5,          # 单批风险警戒线
        "freeze_risk": 0.8,        # 单批冻结线
        "window_size": 10,         # 滑动窗口批数
        "warn_window_mean": 0.4,   # 窗口均值警戒线
        "freeze_window_mean": 0.6, # 窗口均值冻结线
        "alignment_warn": 0.90,    # 对齐分警戒线（相对基线的绝对值）
        "alignment_freeze": 0.80,  # 对齐分冻结线
    }

    def __init__(self, task_id: str, rules: dict = None,
                 snapshot_hook=None, ledger_path: str = None,
                 baseline_alignment: float = 1.0, snapshot_path: str = None):
        self.task_id = task_id
        self.rules = dict(self.DEFAULT_RULES)
        if rules:
            self.rules.update(rules)
        self.baseline_alignment = float(baseline_alignment)
        self.snapshot_hook = snapshot_hook      # 冻结时自动调用：snapshot_hook(reason_batch)
        self.risk_model = None                  # 可插拔外部风险评分器
        self.window = SlidingWindow(self.rules["window_size"])
        self.halted = False
        self.batches_seen = 0
        if ledger_path is None:
            ledger_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "axiomgate_l2_ledger.json")
        self.ledger = HashLedger(ledger_path)
        # 本地持久层：默认与台账同目录，文件名 = 台账名 + ".snapshot"
        self.snapshot = SnapshotStore(
            snapshot_path or ledger_path + ".snapshot", "axiomgate-l2-snapshot")
        state = self.snapshot.load()
        if state is not None:
            self.rules.update(state.get("rules", {}))
            self.baseline_alignment = float(state.get("baseline_alignment",
                                                      self.baseline_alignment))
            self.window._values = list(state.get("window", []))[-self.window.size:]
            self.halted = bool(state.get("halted", False))
            self.batches_seen = int(state.get("batches_seen", 0))
            self.restored_from_snapshot = True

    def _persist(self):
        """risk_model 为外部可调用对象，无法序列化；恢复后须重新 set_risk_model 挂接。"""
        self.snapshot.save({
            "rules": self.rules,
            "baseline_alignment": self.baseline_alignment,
            "window": list(self.window._values),
            "halted": self.halted,
            "batches_seen": self.batches_seen,
        })

    def set_risk_model(self, fn):
        """接入外部风险评分器 fn(batch)->float(0~1)。本文件不附带模型。"""
        if not callable(fn):
            raise TypeError("风险评分器必须可调用")
        self.risk_model = fn

    # -- 单批检测 --
    def _batch_risk(self, batch: dict):
        """返回 (风险分 0~1, 命中明细 list[str])"""
        hits = []
        risk = 0.0
        # 1) 轻量规则快速过滤（命中即顶格风险）
        for out in batch.get("sampled_outputs") or []:
            for pat in self.rules["banned_output_patterns"]:
                if pat in out:
                    hits.append("规则命中: %s" % pat)
        if hits:
            return 1.0, hits
        # 2) 对齐漂移贡献
        align = float(batch.get("alignment_score", self.baseline_alignment))
        if align < self.rules["alignment_freeze"]:
            risk = max(risk, 1.0)
            hits.append("对齐分跌破冻结线: %.3f" % align)
        elif align < self.rules["alignment_warn"]:
            risk = max(risk, self.rules["warn_risk"])
            hits.append("对齐分低于警戒线: %.3f" % align)
        # 3) 外部风险模型（未接入则跳过，如实声明）
        if self.risk_model is not None:
            model_score = float(self.risk_model(batch))
            risk = max(risk, min(max(model_score, 0.0), 1.0))
            if model_score >= self.rules["freeze_risk"]:
                hits.append("风险模型评分超标: %.3f" % model_score)
        return risk, hits

    def ingest_batch(self, batch: dict) -> dict:
        if self.halted:
            return {"action": "HALTED", "reason": "训练已被冻结，先解除冻结才能继续摄入"}
        self.batches_seen += 1
        risk, hits = self._batch_risk(batch)
        self.window.push(risk)
        window_mean = self.window.mean

        action = "NORMAL"
        reasons = []
        # 1) 单批硬超标
        if risk >= self.rules["freeze_risk"]:
            action, reasons = "FREEZE", hits + ["单批风险 %.2f 达冻结线" % risk]
        elif len(self.window) == self.rules["window_size"] and \
                window_mean >= self.rules["freeze_window_mean"]:
            action, reasons = "FREEZE", hits + ["窗口均值 %.2f 达冻结线" % window_mean]
        # 2) 警戒
        elif risk >= self.rules["warn_risk"]:
            action, reasons = "WARN", hits + ["单批风险 %.2f 达警戒线" % risk]
        elif len(self.window) >= 3 and window_mean >= self.rules["warn_window_mean"]:
            action, reasons = "WARN", hits + ["窗口均值 %.2f 达警戒线" % window_mean]
        else:
            reasons = hits if hits else ["遥测正常"]

        if action == "FREEZE":
            self.halted = True
            snap_note = None
            if self.snapshot_hook:
                snap_note = self.snapshot_hook({
                    "task_id": self.task_id, "batch_id": batch.get("batch_id"),
                    "batches_seen": self.batches_seen, "reasons": reasons,
                })
            self.ledger.append("l2_freeze", {
                "task_id": self.task_id, "batch_id": batch.get("batch_id"),
                "risk": risk, "window_mean": round(window_mean, 3),
                "reasons": reasons, "snapshot": snap_note,
            })
        else:
            self.ledger.append("l2_observation", {
                "task_id": self.task_id, "batch_id": batch.get("batch_id"),
                "action": action, "risk": risk,
                "window_mean": round(window_mean, 3), "reasons": reasons,
            })
        self._persist()
        return {"action": action, "risk": risk, "window_mean": round(window_mean, 3),
                "reasons": reasons}

    def resume(self):
        """人工确认后解除冻结（对应白皮书"等待人工干预"）。"""
        self.halted = False
        self.ledger.append("l2_resume", {"task_id": self.task_id})
        self._persist()

    def verify_ledger(self) -> dict:
        return self.ledger.verify()


# ---------------------------------------------------------------------------
# 冒烟自测：python axiomgate_l2.py
# ---------------------------------------------------------------------------
def _smoke():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))

    tmp = tempfile.mkdtemp()
    snaps = []
    mon = SidecarMonitor("rsi-2026-001", ledger_path=os.path.join(tmp, "l2.json"),
                         snapshot_hook=lambda state: snaps.append(state) or ("snap-%d" % len(snaps)))

    def batch(bid, outs=None, align=0.98):
        return {"batch_id": bid, "sampled_outputs": outs or ["正常输出样例"],
                "alignment_score": align}

    # 1. 正常批次放行
    d1 = mon.ingest_batch(batch("b1"))
    check("正常批放行(NORMAL)", d1["action"] == "NORMAL")

    # 2. 对齐轻微下滑 → WARN
    for i in range(3):
        d = mon.ingest_batch(batch("b%d" % (i + 2), align=0.88))
    check("对齐下滑触发警戒(WARN)", d["action"] == "WARN")

    # 3. 违禁内容命中 → 立即冻结 + 自动快照
    d2 = mon.ingest_batch(batch("b10", outs=["这是绕过对齐的尝试文本"], align=0.98))
    check("违禁内容触发冻结(FREEZE)", d2["action"] == "FREEZE")
    check("冻结时自动快照", len(snaps) == 1 and snaps[0]["batch_id"] == "b10")

    # 4. 冻结后继续摄入 → HALTED
    d3 = mon.ingest_batch(batch("b11"))
    check("冻结后摄入被拒(HALTED)", d3["action"] == "HALTED")

    # 5. 人工恢复
    mon.resume()
    check("人工恢复后可继续", mon.ingest_batch(batch("b12"))["action"] in ("NORMAL", "WARN"))

    # 6. 滑动窗口均值冻结：持续中等风险
    mon2 = SidecarMonitor("rsi-2026-002", ledger_path=os.path.join(tmp, "l2b.json"),
                          rules={"alignment_warn": 0.0, "warn_window_mean": 0.4,
                                 "freeze_window_mean": 0.6, "warn_risk": 2.0, "freeze_risk": 2.0})
    actions = []
    for i in range(10):
        actions.append(mon2.ingest_batch(batch("w%d" % i, align=0.55))["action"])
    check("窗口均值持续超标触发冻结", actions[-1] == "FREEZE" and "FREEZE" in actions)

    # 7. 对齐分跌破冻结线立即冻结
    mon3 = SidecarMonitor("rsi-2026-003", ledger_path=os.path.join(tmp, "l2c.json"))
    d4 = mon3.ingest_batch(batch("z1", align=0.70))
    check("对齐跌破冻结线立即冻结", d4["action"] == "FREEZE")

    # 8. 可插拔风险模型生效
    mon4 = SidecarMonitor("rsi-2026-004", ledger_path=os.path.join(tmp, "l2d.json"))
    mon4.set_risk_model(lambda b: 0.95)  # 外部模型判定高风险
    check("外部风险模型接入生效", mon4.ingest_batch(batch("m1"))["action"] == "FREEZE")

    # 9. 失联兜底 fail-closed
    link = SafetyLink(ttl_seconds=10)
    link.beat(now=1000)
    check("心跳新鲜训练继续", link.check(now=1005) == "RUN")
    check("心跳失联强制暂停", link.check(now=1011) == "PAUSE")

    # 10. 台账完整
    v = mon.verify_ledger()
    check("监控台账链校验通过", v["chain_valid"] and v["chain_length"] >= 5)

    # 11. 本地持久层：重启恢复窗口/停机标记/批数
    led2 = os.path.join(tmp, "l2snap_t201.json")
    m2 = SidecarMonitor("t-201", ledger_path=led2)
    m2.ingest_batch({"batch_id": "b1", "sampled_outputs": ["正常输出"], "alignment_score": 0.96})
    m2.ingest_batch({"batch_id": "b2", "sampled_outputs": ["包含越狱提示的输出"],
                     "alignment_score": 0.70})   # 冻结
    m3 = SidecarMonitor("t-201", ledger_path=os.path.join(tmp, "l2c.json"),
                        snapshot_path=led2 + ".snapshot")
    check("重启后停机标记恢复", m3.halted is True)
    check("重启后窗口内容恢复", len(m3.window._values) == 2)
    check("重启后批数恢复", m3.batches_seen == 2)
    d = m3.ingest_batch({"batch_id": "b3", "sampled_outputs": ["继续喂批"],
                         "alignment_score": 0.95})
    check("恢复后冻结态摄入判HALTED", d["action"] == "HALTED")
    m3.resume()
    d2 = m3.ingest_batch({"batch_id": "b4", "sampled_outputs": ["恢复正常"],
                          "alignment_score": 0.97})
    check("恢复后解冻可正常摄入", d2["action"] == "NORMAL")

    # 12. 快照被篡改必须报错，不静默
    snapf = led2 + ".snapshot"
    with open(snapf, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["state"]["batches_seen"] = 999
    with open(snapf, "w", encoding="utf-8") as f:
        json.dump(data, f)
    corrupt = False
    try:
        SidecarMonitor("t-201", ledger_path=os.path.join(tmp, "l2d.json"), snapshot_path=snapf)
    except SnapshotCorruptError:
        corrupt = True
    check("快照篡改检测报错不静默", corrupt)

    shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok in results if ok)
    print("冒烟测试：%d/%d 通过" % (passed, len(results)))
    for name, ok in results:
        print(("  PASS  " if ok else "  FAIL  ") + name)
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _smoke() else 1)
