#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九枢载道 · NinePivotFormation V1.0
「九枢分职、闭环运化、中正恒一」的可执行化：九枢载道体系的宪章层实现。

定位（诚实声明，不装）：
  - 本模块把"九枢"落为治理体系的职权登记与治乱流程引擎：
    每枢绑定唯一能力，枢与能一一对应（互不僭越的机制化）；
    治乱七步有严格次第，乱序即"僭越"驳回并留痕；
    天道边界量化为风险刻度，逾界不自动施治，升级人工（fail-closed）；
    东华为唯一合法扩展位，须显式登记且不得兼任九枢（守正通变的机制化）。
  - 九枢的神话叙事是文化表达；本模块实现的是其治理结构，
    不声称天道本身可被代码执行。守理不执象，明道以化形。

依赖：Python >= 3.7 标准库，零第三方依赖
运行： python3 九枢载道.py
"""

import hashlib
import json
import os
import uuid
from datetime import datetime

__version__ = "1.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 哈希链账本（太阴载史之枢的载体，与系列前作同构）
# ---------------------------------------------------------------------------
class HistoryLedger:
    def __init__(self, path: str):
        self.path = path
        self._entries = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._entries = json.load(f)

    def append(self, kind: str, data: dict):
        prev = self._entries[-1]["chain"] if self._entries else "GENESIS"
        entry = {"kind": kind, "data": data, "prev": prev[:16],
                 "chain": _sha(prev + "|" + kind + "|" + _canon(data)),
                 "at": datetime.now().isoformat(timespec="seconds")}
        self._entries.append(entry)
        self._flush()
        return entry["chain"][:16]

    def verify(self) -> dict:
        for i, e in enumerate(self._entries):
            expect = _sha((e["prev"] if e["prev"] == "GENESIS" else
                           self._entries[i - 1]["chain"]) + "|" + e["kind"] + "|" + _canon(e["data"]))
            if expect != e["chain"]:
                return {"chain_valid": False, "chain_length": len(self._entries), "broken_at": i}
        return {"chain_valid": True, "chain_length": len(self._entries), "broken_at": None}

    def _flush(self):
        tmp = self.path + ".tmp-" + uuid.uuid4().hex[:8]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# 九枢职权宪章：先道后象，枢定其职
# ---------------------------------------------------------------------------
# 核心九枢：每枢一职，职不重叠（机制化"互不僭越"）
NINE_PIVOTS = {
    "羲和":       {"pivot": "甄别之枢", "capability": "discern",   "duty": "辨清浊、筛选正气"},
    "伏羲":       {"pivot": "立序之枢", "capability": "order",      "duty": "立秩序结构、定推演基"},
    "女娲":       {"pivot": "自愈之枢", "capability": "heal",       "duty": "补缺损、续文脉"},
    "西王母":     {"pivot": "循环之枢", "capability": "rebalance",  "duty": "调盈亏、衡气运"},
    "九天玄女":   {"pivot": "衡准之枢", "capability": "adjudicate", "duty": "勘合中正、仲裁万象"},
    "紫薇北极大帝": {"pivot": "时序之枢", "capability": "timebase", "duty": "锁时序基线、定因果先后"},
    "太阴月神":   {"pivot": "载史之枢", "capability": "chronicle",  "duty": "沉淀史鉴、存治乱之迹"},
    "真武大帝":   {"pivot": "镇煞之枢", "capability": "quarantine", "duty": "镇乱封浊、隔绝蔓延"},
    "后土":       {"pivot": "定果之枢", "capability": "converge",   "duty": "稳主脉、定因果大势"},
}

# 合法扩展位：唯东华可通域外，须显式登记且不得兼任九枢
LAWFUL_EXTENSION = "东华帝君"

# 治乱次第（第三节）：乱序执行即僭越，一律驳回
RESTORE_LITURGY = ["羲和", "九天玄女", "真武大帝", "伏羲", "紫薇北极大帝",
                   "后土", "女娲", "西王母", "太阴月神", "九天玄女"]


class UsurpationError(Exception):
    """僭越：乱了次第、兼了不该兼的职、越了不该越的界。"""


class NinePivotFormation:
    """九枢阵：职权登记 + 治乱流程引擎。
    handler 为各枢绑定的可执行能力（接入元极恒一算子/AxiomGate 闸口时即真实生效；
    未绑定 handler 时治乱流程只演练次第与留痕，如实声明，不假装施治）。"""

    # 天道边界：紊乱超过此刻度，不自动施治，升级人工（量化"逾极致"）
    HEAVEN_LIMIT = 0.95

    def __init__(self, ledger_path: str = None):
        if ledger_path is None:
            ledger_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "nine_pivots_ledger.json")
        self.ledger = HistoryLedger(ledger_path)
        self.bindings = {}   # 枢 -> 能力handler
        self.extensions = {}  # 扩展位 -> 登记信息

    # -- 职权登记：每枢一能、一能一枢 --
    def bind(self, pivot: str, handler=None) -> None:
        if pivot not in NINE_PIVOTS:
            raise UsurpationError("%s 不在九枢之列，不得擅自立枢" % pivot)
        # 能力唯一性：一个能力只归一枢（防两枢共管同一权柄）
        for other, info in NINE_PIVOTS.items():
            if other != pivot and other in self.bindings \
                    and NINE_PIVOTS[other]["capability"] == NINE_PIVOTS[pivot]["capability"] \
                    and self.bindings[other] is handler and handler is not None:
                raise UsurpationError("能力 %s 已由 %s 执掌，%s 不得兼任"
                                      % (NINE_PIVOTS[pivot]["capability"], other, pivot))
        self.bindings[pivot] = handler
        self.ledger.append("pivot_bind", {"pivot": pivot,
                                          "capability": NINE_PIVOTS[pivot]["capability"]})

    def register_extension(self, name: str, handler=None, isolate: bool = True) -> None:
        """东华之枢：域外通达，必须显式登记 + 默认隔离（屏障外道）。"""
        if name != LAWFUL_EXTENSION:
            raise UsurpationError("%s 非合法扩展位：九枢之外唯东华可通域外" % name)
        if name in self.bindings:
            raise UsurpationError("东华不得兼任九枢")
        if not isolate:
            raise UsurpationError("外联必须隔离运行：守正而后通变")
        self.extensions[name] = {"isolate": True, "handler": handler}
        self.ledger.append("extension_register", {"name": name, "isolate": True})

    # -- 治乱流程：次第即法，僭越即拒 --
    def restore(self, incident: str, severity: float,
                steps: list) -> dict:
        """按治乱次第施治。steps 为 [(枢名, 传入)] 序列，必须与宪章次第完全一致。
        severity 逾天道边界 → 不自动施治，升级人工。"""
        if severity > self.HEAVEN_LIMIT:
            self.ledger.append("heaven_limit", {"incident": incident, "severity": severity})
            return {"verdict": "ESCALATE",
                    "reason": "紊乱 %.2f 逾天道边界 %.2f，自动修复之功难施，升级人工敬畏处置"
                              % (severity, self.HEAVEN_LIMIT)}
        expected = RESTORE_LITURGY
        got = [s[0] for s in steps]
        if got != expected:
            self.ledger.append("usurpation", {"incident": incident, "got": got})
            raise UsurpationError("治乱次第僭越：宪章为 %s，来文为 %s" % (expected, got))
        trace = []
        for pivot, payload in steps:
            handler = self.bindings.get(pivot)
            if handler is not None:
                out = handler(payload)
            else:
                # 未绑定真实能力：如实声明本次为次第演练，不假装施治
                out = {"staged": True, "duty": NINE_PIVOTS[pivot]["duty"]}
            trace.append({"pivot": pivot, "result": out})
            self.ledger.append("restore_step", {"incident": incident, "pivot": pivot})
        if got[-1] != "九天玄女":
            raise UsurpationError("治乱必须以九天玄女终极勘合收束")
        self.ledger.append("restore_done", {"incident": incident, "steps": len(trace)})
        return {"verdict": "RESTORED", "incident": incident, "trace": trace}

    def verify_ledger(self) -> dict:
        return self.ledger.verify()


# ---------------------------------------------------------------------------
# 冒烟自测
# ---------------------------------------------------------------------------
def _smoke():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))

    tmp = tempfile.mkdtemp()
    fm = NinePivotFormation(ledger_path=os.path.join(tmp, "np.json"))

    # 1. 立阵：九枢全bind（演练用空handler）
    for p in NINE_PIVOTS:
        fm.bind(p)
    check("九枢全数立枢", len(fm.bindings) == 9)

    # 2. 僭越检测：擅自立枢 / 东华兼枢 / 外联不隔离
    usurp = False
    try:
        fm.bind("太上老君")
    except UsurpationError:
        usurp = True
    check("私立枢职被拒", usurp)
    fm.register_extension("东华帝君")
    check("东华合法登记", "东华帝君" in fm.extensions)
    usurp2 = False
    try:
        fm.bind("东华帝君")
    except UsurpationError:
        usurp2 = True
    check("东华兼枢被拒", usurp2)
    usurp3 = False
    try:
        fm.register_extension("东华帝君", isolate=False)
    except UsurpationError:
        usurp3 = True
    check("外联不隔离被拒", usurp3)

    # 3. 治乱次第：正序通过、乱序驳回
    canon_steps = [(p, None) for p in RESTORE_LITURGY]
    r = fm.restore("治乱演武-甲", severity=0.6, steps=canon_steps)
    check("合序治乱复正", r["verdict"] == "RESTORED" and len(r["trace"]) == 10)
    bad_order = canon_steps[::-1]
    usurped = False
    try:
        fm.restore("治乱演武-乙", severity=0.6, steps=bad_order)
    except UsurpationError:
        usurped = True
    check("乱序施治判僭越", usurped)

    # 4. 天道边界：逾刻度不自动施治，升级人工
    r2 = fm.restore("滔天之乱", severity=0.99, steps=canon_steps)
    check("逾天道边界升级人工", r2["verdict"] == "ESCALATE")

    # 5. 未绑定真实能力时如实声明"次第演练"
    check("演练模式如实标注", all(t["result"].get("staged") for t in r["trace"]))

    # 6. 载史台账完整
    v = fm.verify_ledger()
    check("载史台账链校验通过", v["chain_valid"] and v["chain_length"] >= 13)

    shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok in results if ok)
    print("冒烟测试：%d/%d 通过" % (passed, len(results)))
    for name, ok in results:
        print(("  PASS  " if ok else "  FAIL  ") + name)
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _smoke() else 1)
