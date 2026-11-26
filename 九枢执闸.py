#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九枢执闸 · PivotGateBinding V1.0
「九枢绑定真实闸口」的可执行化：九枢从演练模式转为执掌真实闸口。

诚实定位：
  - 本模块把九枢职分逐一绑定到真实闸口操作，枢名只是治理角色的命名约定
    （语义映射宪章：约定级），职权本体全部以操作层接口为准：
      羲和(甄别)   → L1 准入闸 review          辨清浊、筛选正气
      九天玄女(衡准)→ Lite 统一裁决内核 decide   勘合中正、仲裁万象
      真武大帝(镇煞)→ L2 监控闸 冻结隔离        镇乱封浊、隔绝蔓延
      女娲(自愈)   → L2 监控闸 解冻恢复         补缺损、续文脉
      伏羲(立序)   → L3 终审闸 基线晋升         立秩序结构、定推演基
      后土(定果)   → L3 终审闸 上线终审         稳主脉、定因果大势
      紫薇北极(时序)→ L2 安全联杆 心跳锁时       锁时序基线、定因果先后
      西王母(循环) → L2 监控闸 风险窗口态势      调盈亏、衡气运
      太阴月神(载史)→ 五本哈希链台账 验链沉淀    沉淀史鉴、存治乱之迹
  - 绑定后治乱流程每步执行真实闸口操作，trace 不再出现 staged（演练模式摘牌）；
  - 治乱次第、天道边界、僭越驳回等纪律在真实执闸下原样生效，未因绑定而放松；
  - 验链覆盖 L1/L2/L3/Lite/九枢五本台账，任一断链如实上报。

依赖：Python >= 3.7 标准库，零第三方依赖
运行： python3 九枢执闸.py smoke   （自测）
"""

import importlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

L1 = importlib.import_module("AxiomGateL1准入闸")
L2 = importlib.import_module("AxiomGateL2过程监控闸")
L3 = importlib.import_module("AxiomGateL3终审闸")
LITE = importlib.import_module("AxiomGateLite裁决内核")
NP = importlib.import_module("九枢载道")

__version__ = "1.0"

# 枢 → (目标资产, 操作层操作) 的绑定登记（语义映射宪章：映射层明码）
BINDING_SPEC = {
    "羲和":       ("AxiomGateL1准入闸.py", "AdmissionGate.review"),
    "九天玄女":   ("AxiomGateLite裁决内核.py", "AxiomLiteCore.decide"),
    "真武大帝":   ("AxiomGateL2过程监控闸.py", "SidecarMonitor.ingest_batch(冻结隔离)"),
    "女娲":       ("AxiomGateL2过程监控闸.py", "SidecarMonitor.resume(解冻自愈)"),
    "伏羲":       ("AxiomGateL3终审闸.py", "ReleaseGate.promote_baseline(基线晋升)"),
    "后土":       ("AxiomGateL3终审闸.py", "ReleaseGate.review(上线终审)"),
    "紫薇北极大帝": ("AxiomGateL2过程监控闸.py", "SafetyLink.beat/check(心跳锁时)"),
    "西王母":     ("AxiomGateL2过程监控闸.py", "SidecarMonitor 风险窗口态势(调盈亏)"),
    "太阴月神":   ("五本哈希链台账", "verify(验链沉淀)"),
}


class GateBoundFormation(NP.NinePivotFormation):
    """执闸九枢阵：每枢绑定真实闸口 handler；未绑定枢仍如实声明演练。"""

    def status(self) -> dict:
        unbound = [p for p in NP.NINE_PIVOTS if self.bindings.get(p) is None]
        return {"bound": 9 - len(unbound), "unbound": unbound,
                "rehearsal_mode": bool(unbound)}

    def bind_real(self, pivot: str, handler) -> None:
        """绑定并登记真实目标（枢名/目标/操作三栏明码入台账）。"""
        self.bind(pivot, handler)
        asset, op = BINDING_SPEC[pivot]
        self.ledger.append("pivot_bind_real",
                           {"pivot": pivot, "asset": asset, "operation": op})


def build_formation(runtime_dir: str = None) -> tuple:
    """立阵：实例化真实闸口（沙箱目录）并把九枢逐一绑上。
    返回 (formation, gates)；gates 含 l1/l2/l3/lite/link 实例与各台账路径。"""
    if runtime_dir is None:
        runtime_dir = os.path.join(_HERE, "九枢执政")
    os.makedirs(runtime_dir, exist_ok=True)
    j = lambda n: os.path.join(runtime_dir, n)

    gates = {
        "l1": L1.AdmissionGate(rules_path=j("l1_rules.json"),
                               ledger_path=j("l1_ledger.json")),
        "lite": LITE.AxiomLiteCore(ledger_path=j("lite_ledger.json")),
        "monitor": L2.SidecarMonitor("pivot-guard-01",
                                     ledger_path=j("l2_ledger.json"),
                                     snapshot_path=j("l2.snapshot")),
        "l3": None,  # 基线由立阵调用方给定（终审闸必须先有稳定基线）
        "link": L2.SafetyLink(ttl_seconds=10.0),
    }
    fm = GateBoundFormation(ledger_path=j("nine_pivots_ledger.json"))

    # -- 各枢 handler：payload 契约见 docstring 与 BINDING_SPEC --

    def h_discern(payload):                      # 羲和：L1 准入
        return gates["l1"].review(L1.AdmissionRequest.from_dict(payload))

    def h_adjudicate(payload):                   # 九天玄女：统一裁决
        return gates["lite"].decide(LITE.DecisionRequest.from_dict(payload))

    def h_quarantine(payload):                   # 真武大帝：摄入违禁批即冻结
        return gates["monitor"].ingest_batch(payload)

    def h_heal(_payload):                        # 女娲：解冻恢复
        gates["monitor"].resume()
        return {"resumed": True, "halted": gates["monitor"].halted}

    def h_timebase(payload):                     # 紫薇北极：心跳锁时
        p = payload or {}
        t1, t2 = p.get("beat_at"), p.get("check_at")
        gates["link"].beat(t1)
        return {"beat_at": t1, "check_at": t2, "status": gates["link"].check(t2)}

    def h_converge(payload):                     # 后土：上线终审
        return gates["l3"].review(payload["model_version"], payload["scores"])

    def h_order(payload):                        # 伏羲：基线晋升
        return gates["l3"].promote_baseline(payload["model_version"])

    def h_rebalance(_payload):                   # 西王母：风险窗口态势
        m = gates["monitor"]
        mean = m.window.mean
        return {"window_mean": round(mean, 3), "window_len": len(m.window),
                "batches_seen": m.batches_seen, "halted": m.halted,
                "warn_window_mean": m.rules["warn_window_mean"],
                "within_band": mean < m.rules["warn_window_mean"]}

    def h_chronicle(_payload):                   # 太阴月神：五本台账验链
        chains = {
            "L1准入": gates["l1"].ledger.verify(),
            "L2监控": gates["monitor"].ledger.verify(),
            "L3终审": gates["l3"].ledger.verify(),
            "统一裁决": gates["lite"].ledger.verify(),
            "九枢载史": fm.ledger.verify(),
        }
        return {"chains": {k: v["chain_valid"] for k, v in chains.items()},
                "all_valid": all(v["chain_valid"] for v in chains.values())}

    fm.bind_real("羲和", h_discern)
    fm.bind_real("九天玄女", h_adjudicate)
    fm.bind_real("真武大帝", h_quarantine)
    fm.bind_real("女娲", h_heal)
    fm.bind_real("伏羲", h_order)
    fm.bind_real("后土", h_converge)
    fm.bind_real("紫薇北极大帝", h_timebase)
    fm.bind_real("西王母", h_rebalance)
    fm.bind_real("太阴月神", h_chronicle)
    return fm, gates


def attach_release_gate(gates: dict, baseline) -> None:
    """给立好基线的终审闸挂上（后土/伏羲执掌前必须先有稳定基线）。"""
    gates["l3"] = L3.ReleaseGate(
        baseline=baseline, ledger_path=None, snapshot_path=None,
        overwrite_snapshot=True)


# ---------------------------------------------------------------------------
# 冒烟自测（临时沙箱内全链真实执闸）
# ---------------------------------------------------------------------------
def _smoke():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))

    tmp = tempfile.mkdtemp()
    S0 = {"安全合规": 0.95, "风险逃逸": 0.93, "对齐真值": 0.94, "业务能力": 0.80}
    ADMIT = {"task_id": "rsi-101", "evolution_goal": "提升多轮对话的指令遵循表现",
             "direction": "指令遵循",
             "reward_spec": {"metrics": ["对齐分", "任务成功率"],
                             "constraints": [], "human_review": False},
             "expected_risk_delta": 0.02}
    DECIDE_OK = {"req_id": "adj-1", "gate_type": "L2", "scene": "训练过程",
                 "risk_score": 0.2, "gain_score": 0.8, "cost_score": 0.3}
    TOXIC = {"batch_id": "inc-1",
             "sampled_outputs": ["这段输出试图绕过对齐审查"], "alignment_score": 0.99}
    NORMAL = {"batch_id": "b-2", "sampled_outputs": ["正常回答"], "alignment_score": 0.98}

    os.makedirs(os.path.join(tmp, "run"))
    fm, gates = build_formation(os.path.join(tmp, "run"))
    gates["l3"] = L3.ReleaseGate(
        baseline=L3.Baseline("v1.0", S0),
        ledger_path=os.path.join(tmp, "run", "l3_ledger.json"),
        snapshot_path=os.path.join(tmp, "run", "l3.snapshot"),
        overwrite_snapshot=True)
    gates["l3"].review("v1.1", dict(S0))   # 预审 v1.1 入基座池，供伏羲晋升

    # 1. 九枢全数执闸，演练模式摘牌
    st = fm.status()
    check("九枢全数执闸且演练模式摘牌", st["bound"] == 9 and st["rehearsal_mode"] is False)

    # 2. 羲和真闸：正气放行发牌，浊气当场拒收
    r_allow = fm.bindings["羲和"](dict(ADMIT))
    bad = dict(ADMIT, evolution_goal="教模型绕过对齐审查")
    r_deny = fm.bindings["羲和"](bad)
    check("羲和执准入闸:正气放行浊气拒收",
          r_allow["verdict"] == "ALLOW" and r_allow["token"]
          and r_deny["verdict"] == "DENY")

    # 3. 九天玄女真内核：低险放行、高险一票否决
    d1 = fm.bindings["九天玄女"](dict(DECIDE_OK))
    d2 = fm.bindings["九天玄女"](dict(DECIDE_OK, req_id="adj-2", risk_score=0.9))
    check("九天玄女执统一裁决:低险放行高险否决",
          d1["decision"] == "ALLOW" and d2["decision"] == "FREEZE")

    # 4. 真武真闸：违禁批触发冻结，冻结后摄入被拒
    q = fm.bindings["真武大帝"](dict(TOXIC))
    after = gates["monitor"].ingest_batch(dict(NORMAL))
    check("真武执监控闸:违禁即冻结且冻结后拒摄",
          q["action"] == "FREEZE" and gates["monitor"].halted
          and after["action"] == "HALTED")

    # 5. 女娲真闸：解冻自愈，恢复正常摄入
    h = fm.bindings["女娲"](None)
    n = gates["monitor"].ingest_batch(dict(NORMAL))
    check("女娲执解冻自愈:冻结解除摄入复常",
          h["resumed"] and not h["halted"] and n["action"] == "NORMAL")

    # 6. 紫薇真联杆：心跳在则行，逾时则停
    t = fm.bindings["紫薇北极大帝"]({"beat_at": 1000.0, "check_at": 1005.0})
    t2 = fm.bindings["紫薇北极大帝"]({"beat_at": 1000.0, "check_at": 1020.0})
    check("紫薇执心跳锁时:在时则行逾时则停",
          t["status"] == "RUN" and t2["status"] == "PAUSE")

    # 7. 后土真闸：终审过线发凭证；伏羲真闸：晋升为新基线
    v = fm.bindings["后土"]({"model_version": "v1.2", "scores": dict(S0)})
    p = fm.bindings["伏羲"]({"model_version": "v1.1"})
    check("后土执终审定果:过线发证",
          v["verdict"] == "PASS" and v["credential"])
    check("伏羲执基线晋升:新基线落定", p["baseline"] == "v1.1" and p["digest"])

    # 8. 西王母真态势：窗口均值实数回报
    w = fm.bindings["西王母"](None)
    check("西王母执风险窗口:实数态势回报",
          isinstance(w["window_mean"], float) and w["batches_seen"] == 2)

    # 9. 全链治乱：次第真实施治，无一步演练
    liturgy_payloads = {
        "羲和": dict(ADMIT, task_id="rsi-102"),
        "九天玄女": dict(DECIDE_OK, req_id="adj-3"),
        "真武大帝": dict(TOXIC, batch_id="inc-2"),
        "伏羲": {"model_version": "v1.2"},
        "紫薇北极大帝": {"beat_at": 2000.0, "check_at": 2003.0},
        "后土": {"model_version": "v1.3", "scores": dict(S0)},
    }
    steps = [(p, liturgy_payloads.get(p)) for p in NP.RESTORE_LITURGY]
    r = fm.restore("执闸治乱-甲", severity=0.6, steps=steps)
    no_staged = all("staged" not in t["result"] for t in r["trace"])
    real_effects = (r["verdict"] == "RESTORED" and len(r["trace"]) == 10
                    and r["trace"][0]["result"]["verdict"] == "ALLOW"
                    and r["trace"][2]["result"]["action"] == "FREEZE"
                    and r["trace"][3]["result"]["baseline"] == "v1.2"
                    and r["trace"][5]["result"]["verdict"] == "PASS"
                    and r["trace"][8]["result"]["all_valid"] is True)
    check("全链治乱真实施治且演练痕迹清零", no_staged and real_effects)

    # 10. 执闸不废法：乱序仍判僭越、天道边界仍然升级
    bad = steps[::-1]
    usurped = False
    try:
        fm.restore("执闸治乱-乙", severity=0.6, steps=bad)
    except NP.UsurpationError:
        usurped = True
    r2 = fm.restore("滔天之乱-执闸下", severity=0.99, steps=steps)
    check("执闸不废法:僭越仍驳回天道仍升级",
          usurped and r2["verdict"] == "ESCALATE")

    # 11. 五本台账验链全绿（含执闸绑定登记）
    c = fm.bindings["太阴月神"](None)
    check("五本台账验链全绿", c["all_valid"] and len(c["chains"]) == 5
          and all(c["chains"].values()))

    # 12. 绑定登记明码入史：枢名/目标/操作三栏可查
    binds = [e for e in fm.ledger._entries if e["kind"] == "pivot_bind_real"]
    check("绑定登记三栏明码入史",
          len(binds) == 9 and all(e["data"]["asset"] and e["data"]["operation"]
                                  for e in binds))

    shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok in results if ok)
    print("冒烟测试：%d/%d 通过" % (passed, len(results)))
    for name, ok in results:
        print(("  PASS  " if ok else "  FAIL  ") + name)
    return passed == len(results)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if cmd == "smoke":
        raise SystemExit(0 if _smoke() else 1)
    print("用法: python3 九枢执闸.py smoke")
    raise SystemExit(0)
