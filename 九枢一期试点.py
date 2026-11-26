#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九枢一期试点 · 证据链验收引擎最小闭环 V0.1

定位（诚实声明）：
  - 本模块是薄编排器，只做串联，不重写任何组件：
      接任务 → 九枢准入 → 执行留痕 → 玄圭锁档 → 终审出报告 → 回放验证。
  - 九枢准入使用试点专用台账（nine_pivots_pilot_ledger.json），
    不污染九枢载道正册；玄圭沿用本目录 state/xuangui 锁档体系；
    记忆机沿用 memory-machine 事件流正本（本仓库 memory-machine/）。
  - 三条反例纪律（差异化验证）：
      重试不静默：失败与重试全部留痕，追加不覆盖；
      契约冲突响亮失败：证据引用不符契约即拒，不降级兼容；
      无证据不结案：缺证据的判据报错而非跳过。

依赖：Python >= 3.7 标准库 + 本仓库组件（九枢载道/玄圭试点/记忆机）。
运行： python3 九枢一期试点.py selftest
"""

import importlib.util
import json
import os
import sys

__version__ = "0.1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_MM_PATH = os.path.join(_HERE, "memory-machine", "memory_machine.py")

_spec = importlib.util.spec_from_file_location("memory_machine", _MM_PATH)
mm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mm)

from 九枢载道 import NinePivotFormation, NINE_PIVOTS  # noqa: E402
from 玄圭试点 import XuanGuiKernel  # noqa: E402

PILOT_LEDGER = os.path.join(_HERE, "nine_pivots_pilot_ledger.json")

EVIDENCE_CONTRACT = ("kind", "ref", "fingerprint")  # 证据引用契约：三字段缺一即拒


class ContractViolationError(Exception):
    """证据引用不符契约：响亮失败，不降级兼容。"""


class MissingEvidenceError(Exception):
    """无证据不结案：判据缺证据即报错，不许跳过。"""


class ReplayMismatchError(Exception):
    """回放重建与留痕状态指纹不一致：闭环不成立。"""


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def validate_evidence(ev) -> None:
    """证据引用契约校验：{kind, ref, fingerprint} 三字段齐备且非空。"""
    if not isinstance(ev, dict):
        raise ContractViolationError("证据引用必须是 dict，得到 %r" % type(ev).__name__)
    for k in EVIDENCE_CONTRACT:
        if k not in ev or not isinstance(ev[k], str) or not ev[k].strip():
            raise ContractViolationError(
                "证据引用缺字段或为空：%s（契约=%s）" % (k, "/".join(EVIDENCE_CONTRACT)))
    extra = set(ev) - set(EVIDENCE_CONTRACT)
    if extra:
        raise ContractViolationError("证据引用含契约外字段：%s" % sorted(extra))


def validate_task_def(task) -> None:
    if not isinstance(task, dict):
        raise ContractViolationError("任务定义必须是 dict")
    if not task.get("name") or not isinstance(task["name"], str):
        raise ContractViolationError("任务定义缺 name（非空字符串）")
    if not callable(task.get("executor")):
        raise ContractViolationError("任务定义缺 executor（可调用执行体）")
    criteria = task.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ContractViolationError("任务定义缺 criteria（判据集，非空列表）")
    for c in criteria:
        if not isinstance(c, dict) or not c.get("id") or not c.get("desc") \
                or not callable(c.get("check")):
            raise ContractViolationError("判据须含 id/desc/check 三字段且 check 可调用")


def adjudicate(criteria, artifact, evidence, trace):
    """终审：逐条判据核验。每条结论必须携带证据引用；缺证据即响亮失败。"""
    conclusions = []
    for c in criteria:
        conclusion, ev = c["check"](artifact, evidence)
        if conclusion is not None and ev is None:
            raise MissingEvidenceError(
                "判据 %s 得出结论 %r 却无证据引用——无证据不结案" % (c["id"], conclusion))
        if ev is not None:
            validate_evidence(ev)
        conclusions.append({"criterion": c["id"], "desc": c["desc"],
                            "conclusion": conclusion, "evidence": ev})
    return conclusions


def run_task(task, kernel=None, formation=None, max_retries=2):
    """薄编排主流程：接任务→九枢准入→执行留痕→玄圭锁档→终审→回放验证。"""
    kernel = kernel or XuanGuiKernel()
    formation = formation or NinePivotFormation(ledger_path=PILOT_LEDGER)
    validate_task_def(task)
    name = task["name"]
    start_ts = mm.now_ts()
    local_state = {}
    step_count = [0]

    def trace(kind, detail):
        """执行留痕：每步追加事件流，绝不覆盖。"""
        ev = mm.record(name, kind, detail)
        mm.apply_event(local_state, ev)
        step_count[0] += 1
        return ev

    def fail_loud(stage, exc):
        """响亮失败且留痕：事件流与九枢台账双记，再原样抛出。"""
        try:
            trace("fail:" + stage, {"error": "%s: %s" % (type(exc).__name__, exc)})
            formation.ledger.append("task_fail", {"task": name, "stage": stage,
                                                  "error": str(exc)})
        except Exception:
            pass
        raise exc

    # ---- 2. 九枢准入：立阵 + 准入记档（未绑 handler，如实为次第演练） ----
    try:
        for p in NINE_PIVOTS:
            formation.bind(p)
        formation.ledger.append("task_admit",
                                {"task": name,
                                 "criteria": [c["id"] for c in task["criteria"]]})
    except Exception as e:
        fail_loud("admit", e)
    trace("admit", {"pivots": len(NINE_PIVOTS),
                    "criteria": [c["id"] for c in task["criteria"]]})

    # ---- 3. 执行留痕：失败与重试全部留痕，追加不覆盖（重试不静默） ----
    artifact, retries, fails = None, 0, 0
    for attempt in range(1, max_retries + 2):
        try:
            artifact = task["executor"](trace)
            break
        except Exception as e:
            fails += 1
            trace("step_fail", {"attempt": attempt, "error": str(e)})
            if attempt > max_retries:
                fail_loud("execute", e)
            retries += 1
            trace("step_retry", {"from_attempt": attempt, "to_attempt": attempt + 1})
    trace("artifact_ready", {"summary": _canon(artifact)[:200]})

    # ---- 4. 玄圭锁档：写入即确权，证据引用对齐契约 ----
    try:
        content = _canon(artifact)
        att, _exp = kernel.write("asset", name, content)
    except Exception as e:
        fail_loud("lock", e)
    evidence = {"kind": "asset", "ref": att["asset_id"],
                "fingerprint": att["sha256_hash"][:32]}
    trace("lock", evidence)

    # ---- 5. 终审出报告：无证据不结案，契约冲突响亮失败 ----
    try:
        conclusions = adjudicate(task["criteria"], artifact, evidence, trace)
    except Exception as e:
        fail_loud("adjudicate", e)
    trace("verdict", {"conclusions": len(conclusions)})

    # ---- 6. 回放验证：事件流重建 vs 留痕状态，指纹一致方判闭环 ----
    replayed = {}
    for ev in mm.read_events(since_ts=start_ts):
        if ev["entity"] == name:
            mm.apply_event(replayed, ev)
    fp_run, fp_replay = mm.state_fingerprint(local_state), mm.state_fingerprint(replayed)
    if fp_run != fp_replay:
        fail_loud("replay", ReplayMismatchError(
            "回放指纹 %s ≠ 留痕指纹 %s" % (fp_replay, fp_run)))

    # ---- 双验真：玄圭 + 九枢 ----
    xv, nv = kernel.verify(), formation.verify_ledger()
    if not (xv["ledger"]["valid"] and xv["dual_copy_consistent"] and nv["chain_valid"]):
        fail_loud("double_verify", RuntimeError(
            "双验真未过：玄圭=%s 九枢=%s" % (xv, nv)))

    report = {
        "task": name, "verdict": "ACCEPTED",
        "conclusions": conclusions,
        "process": {"steps": step_count[0], "retries": retries, "fails": fails},
        "replay": {"fingerprint": fp_replay, "match": True},
        "double_verify": {"xuangui": xv, "nine_pivots": nv},
        "asset_evidence": evidence,
    }
    report_att, _ = kernel.write("asset", name + "-验收报告", _canon(report))
    report["report_evidence"] = {"kind": "asset", "ref": report_att["asset_id"],
                                 "fingerprint": report_att["sha256_hash"][:32]}
    return report


# ---------------------------------------------------------------------------
# 自测：一正例（出 3 道带判据的题）+ 三反例（差异化验证）
# ---------------------------------------------------------------------------
def _positive_executor(trace):
    questions = []
    for i in range(1, 4):
        q = {"id": "Q%d" % i,
             "question": "%d + %d = ?" % (i, 10 - i),
             "answer": 10,
             "criteria": ["答案等于 10", "须给出算式"]}
        trace("gen_question", {"id": q["id"]})
        questions.append(q)
    return {"questions": questions}


def _positive_criteria():
    def check_count(artifact, evidence):
        ok = len(artifact["questions"]) == 3
        return ("PASS" if ok else "FAIL", dict(evidence))

    def check_criteria(artifact, evidence):
        ok = all(q.get("criteria") for q in artifact["questions"])
        return ("PASS" if ok else "FAIL", dict(evidence))

    def check_lock(artifact, evidence):
        import hashlib
        content_sha = hashlib.sha256(_canon(artifact).encode("utf-8")).hexdigest()[:32]
        ok = evidence["fingerprint"] == content_sha
        return ("PASS" if ok else "FAIL", dict(evidence))

    return [{"id": "C-数量", "desc": "恰好 3 道题", "check": check_count},
            {"id": "C-带判据", "desc": "每题带判据", "check": check_criteria},
            {"id": "C-锁档", "desc": "产物已玄圭锁档", "check": check_lock}]


def _flaky_executor(trace):
    """首次执行失败（模拟环境抖动），重试后成功。"""
    if not getattr(_flaky_executor, "_failed", False):
        _flaky_executor._failed = True
        raise RuntimeError("模拟环境抖动")
    return _positive_executor(trace)


def _selftest():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))

    tmp = tempfile.mkdtemp()
    kernel = XuanGuiKernel(root_dir=os.path.join(tmp, "vault"))
    formation = NinePivotFormation(ledger_path=os.path.join(tmp, "np.json"))

    # ---- 正例：全链路通过，报告 ≥3 条带证据引用结论 ----
    _flaky_executor._failed = False
    report = run_task({"name": "九枢一期试点-出3道带判据的题",
                       "executor": _flaky_executor,
                       "criteria": _positive_criteria()},
                      kernel=kernel, formation=formation)
    with_evidence = [c for c in report["conclusions"] if c["evidence"]]
    check("正例全链路通过", report["verdict"] == "ACCEPTED")
    check("报告含≥3条带证据引用结论", len(with_evidence) >= 3)
    check("重试留痕计入报告且不覆盖", report["process"]["retries"] == 1
          and report["process"]["fails"] == 1)
    evs = [e["kind"] for e in mm.read_events()
           if e["entity"] == "九枢一期试点-出3道带判据的题"]
    check("事件流含 step_fail 与 step_retry 双记录",
          "step_fail" in evs and "step_retry" in evs)
    check("回放指纹一致", report["replay"]["match"])
    check("玄圭+九枢双验真通过",
          report["double_verify"]["xuangui"]["ledger"]["valid"]
          and report["double_verify"]["xuangui"]["dual_copy_consistent"]
          and report["double_verify"]["nine_pivots"]["chain_valid"])

    # ---- 反例一：契约冲突响亮失败（证据引用缺 fingerprint）----
    def bad_contract_check(artifact, evidence):
        return ("PASS", {"kind": "asset", "ref": "X"})  # 缺 fingerprint

    try:
        run_task({"name": "反例-契约冲突", "executor": _positive_executor,
                  "criteria": [{"id": "C-坏", "desc": "坏契约", "check": bad_contract_check}]},
                 kernel=kernel, formation=formation)
        raised = None
    except ContractViolationError as e:
        raised = e
    check("契约冲突响亮失败", raised is not None)
    check("契约冲突留痕", "fail:adjudicate" in [e["kind"] for e in mm.read_events()
                                                if e["entity"] == "反例-契约冲突"])

    # ---- 反例二：无证据不结案 ----
    def no_evidence_check(artifact, evidence):
        return ("PASS", None)

    try:
        run_task({"name": "反例-无证据", "executor": _positive_executor,
                  "criteria": [{"id": "C-空", "desc": "无证据", "check": no_evidence_check}]},
                 kernel=kernel, formation=formation)
        raised = None
    except MissingEvidenceError as e:
        raised = e
    check("无证据不结案响亮失败", raised is not None)
    check("无证据失败留痕", "fail:adjudicate" in [e["kind"] for e in mm.read_events()
                                                  if e["entity"] == "反例-无证据"])

    # ---- 反例三：重试耗尽响亮失败且留痕 ----
    def always_fail(trace):
        raise RuntimeError("持续失败")

    t0 = mm.now_ts()

    try:
        run_task({"name": "反例-重试耗尽", "executor": always_fail,
                  "criteria": _positive_criteria()},
                 kernel=kernel, formation=formation, max_retries=1)
        raised = None
    except RuntimeError as e:
        raised = e
    check("重试耗尽响亮失败", raised is not None)
    evs = [e["kind"] for e in mm.read_events(since_ts=t0)
           if e["entity"] == "反例-重试耗尽"]
    check("重试耗尽全程留痕（2次fail+1次retry+终fail）",
          evs.count("step_fail") == 2 and evs.count("step_retry") == 1
          and "fail:execute" in evs)

    shutil.rmtree(tmp, ignore_errors=True)
    n_pass = sum(1 for _, ok in results if ok)
    print("结论：%d/%d 过" % (n_pass, len(results)))
    return n_pass == len(results)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        print("=== 九枢一期试点自测 V%s ===" % __version__)
        sys.exit(0 if _selftest() else 1)
    else:
        print("用法：python3 九枢一期试点.py [selftest]")
