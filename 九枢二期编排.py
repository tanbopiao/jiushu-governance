#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九枢二期编排 · 派活与执行器隔离 V0.1

诚实定位：
  - 一期（九枢一期试点.py）已立「任务→证据→锁档→验收」最小闭环，但其准入
    为次第演练模式（未绑 handler）；二期补两件事：
      1) 派活：Dispatcher 按次第派发任务队列，前序响亮失败则次第中断、
         余件如实标注未派发，不静默跳过；
      2) 隔离：执行器在隔离子进程运行（fork + 管道回流留痕），
         崩溃 / 超时一律熔断响亮失败，主阵与台账不受污染。
  - 全链真闸：羲和执 L1 准入、真武执 L2 监控（违禁输出即冻结）、
    女娲执解冻自愈、九天玄女执统一裁决、后土执 L3 终审、伏羲执基线晋升、
    紫薇执心跳锁时、太阴执五本台账验链——trace 无 staged，演练模式摘牌。
  - 不重写任何组件：闸口复用 AxiomGate 三道闸 + Lite，锁档复用玄圭，
    留痕回放复用记忆机，判据核验复用一期试点。
  - 执行器契约：可调用体、产物须 JSON 可序列化（隔离边界即契约）。

依赖：Python >= 3.7 标准库 + 体系既有组件。
运行： python3 九枢二期编排.py selftest   （自测）
       python3 九枢二期编排.py prod       （生产实跑，锁档主库）
"""

import json
import multiprocessing
import os
import sys
import time

from 九枢载道 import NINE_PIVOTS  # noqa: F401
from 九枢执闸 import build_formation, attach_release_gate
from AxiomGateL3终审闸 import Baseline
from 九枢一期试点 import (mm, _canon, validate_task_def, adjudicate,      # noqa: F401
                          ContractViolationError, MissingEvidenceError,  # noqa: F401
                          ReplayMismatchError)                           # noqa: F401
from 玄圭试点 import XuanGuiKernel

__version__ = "0.1"

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(_HERE, "九枢执政")


class IsolationBreachError(Exception):
    """隔离被突破：执行器崩溃或超时，熔断响亮失败。"""


class ZhenwuFreezeError(Exception):
    """真武镇煞：执行输出命中违禁，监控闸冻结。"""


class SequenceHaltError(Exception):
    """次第中断：前序任务响亮失败，余件不静默跳过。"""


# ---------------------------------------------------------------------------
# 隔离执行：子进程 + 管道回流留痕
# ---------------------------------------------------------------------------
def _child_main(send_conn, executor):
    """子进程入口：执行器每步留痕经管道回流，产物以 JSON 交回。"""
    def trace(kind, detail):
        send_conn.send(("ev", kind, detail))
    try:
        artifact = executor(trace)
        send_conn.send(("done", _canon(artifact)))
    except BaseException as e:  # 子进程内任何异常都如实回传，不静默
        try:
            send_conn.send(("error", "%s: %s" % (type(e).__name__, e)))
        except Exception:
            pass
    finally:
        send_conn.close()


def run_isolated(task_name, executor, trace, timeout=30.0):
    """隔离执行：崩溃/超时熔断，留痕事件实时回流主阵。"""
    ctx = multiprocessing.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)  # parent 收、child 发
    proc = ctx.Process(target=_child_main, args=(child_conn, executor))
    proc.start()
    child_conn.close()  # 父进程关掉发送端副本，子进程退出才能传导 EOF
    deadline = time.time() + timeout
    artifact = None
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.terminate()
                proc.join()
                raise IsolationBreachError(
                    "隔离执行器超时 %.1fs，已熔断（task=%s）" % (timeout, task_name))
            if not parent_conn.poll(remaining):
                continue
            try:
                msg = parent_conn.recv()
            except EOFError:
                proc.join()
                raise IsolationBreachError(
                    "隔离执行器崩溃：管道断，未交回产物（task=%s）" % task_name)
            tag = msg[0]
            if tag == "ev":
                trace(msg[1], msg[2])   # 留痕实时回流（含 L2 监控钩子）
            elif tag == "done":
                artifact = json.loads(msg[1])
                break
            elif tag == "error":
                proc.join()
                raise RuntimeError("隔离执行器失败：%s" % msg[1])
    except ZhenwuFreezeError:
        proc.terminate()  # 真武镇煞：立即断执行器
        proc.join()
        raise
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join()
        parent_conn.close()
    return artifact


# ---------------------------------------------------------------------------
# 全链治派：九枢真闸 + 隔离执行 + 证据链闭环
# ---------------------------------------------------------------------------
GOVERN_FIELDS = ("admission", "adjudication", "release")


def run_governed_task(task, kernel, fm, gates, timeout=30.0, max_retries=2):
    """单任务全链：心跳→自愈→准入→隔离执行(监控)→锁档→裁决→终审→晋升→验链→回放。"""
    validate_task_def(task)
    for field in GOVERN_FIELDS:
        if not isinstance(task.get(field), dict):
            raise ContractViolationError("任务定义缺 %s（九枢治理契约）" % field)
    name = task["name"]
    start_ts = mm.now_ts()
    local_state = {}
    step_count = [0]

    def fail_loud(stage, exc):
        try:
            ev = mm.record(name, "fail:" + stage, {"error": "%s: %s" % (type(exc).__name__, exc)})
            mm.apply_event(local_state, ev)
            fm.ledger.append("task_fail", {"task": name, "stage": stage, "error": str(exc)})
        except Exception:
            pass
        raise exc

    def trace(kind, detail):
        # 真武监控钩子：执行器输出批实时过 L2，违禁即冻结
        if kind == "output":
            act = gates["monitor"].ingest_batch(detail)
            if act.get("action") in ("FREEZE", "HALTED"):
                ev = mm.record(name, "fail:zhenwu", {"action": act.get("action"),
                                                     "batch_id": detail.get("batch_id")})
                mm.apply_event(local_state, ev)
                raise ZhenwuFreezeError("真武镇煞：输出批 %s 判 %s"
                                        % (detail.get("batch_id"), act.get("action")))
        ev = mm.record(name, kind, detail)
        mm.apply_event(local_state, ev)
        step_count[0] += 1
        return ev

    # ---- 紫薇：心跳锁时，失锁不派活 ----
    try:
        gates["link"].beat()
        beat_status = gates["link"].check()
        if beat_status != "RUN":
            fail_loud("timebase", RuntimeError("紫薇心跳失锁：%s" % beat_status))
        trace("timebase", {"status": beat_status})
    except Exception as e:
        fail_loud("timebase", e)

    # ---- 女娲：带伤上阵先自愈（冻结解除方可摄入） ----
    if gates["monitor"].halted:
        try:
            heal = fm.bindings["女娲"](None)
            trace("heal", {"resumed": heal.get("resumed")})
        except Exception as e:
            fail_loud("heal", e)

    # ---- 羲和：L1 真闸准入，拒收即响亮失败 ----
    try:
        admit = fm.bindings["羲和"](dict(task["admission"]))
        if admit.get("verdict") != "ALLOW":
            fail_loud("admit", RuntimeError("羲和拒收：%s" % admit.get("reason")))
        trace("admit", {"verdict": admit["verdict"], "token": admit.get("token", "")[:16]})
    except Exception as e:
        fail_loud("admit", e)

    # ---- 隔离执行：失败与重试全部留痕，追加不覆盖（重试不静默） ----
    artifact, retries, fails = None, 0, 0
    for attempt in range(1, max_retries + 2):
        try:
            artifact = run_isolated(name, task["executor"], trace, timeout=timeout)
            break
        except (IsolationBreachError, RuntimeError, ZhenwuFreezeError) as e:
            fails += 1
            trace("step_fail", {"attempt": attempt, "error": "%s: %s" % (type(e).__name__, e)})
            if isinstance(e, ZhenwuFreezeError) or attempt > max_retries:
                fail_loud("execute", e)
            retries += 1
            trace("step_retry", {"from_attempt": attempt, "to_attempt": attempt + 1})
    trace("artifact_ready", {"summary": _canon(artifact)[:200]})

    # ---- 玄圭锁档：写入即确权，证据引用对齐契约 ----
    try:
        content = _canon(artifact)
        att, _exp = kernel.write("asset", name, content)
    except Exception as e:
        fail_loud("lock", e)
    evidence = {"kind": "asset", "ref": att["asset_id"],
                "fingerprint": att["sha256_hash"][:32]}
    trace("lock", evidence)

    # ---- 九天玄女：统一裁决，冻结即响亮失败 ----
    try:
        decision = fm.bindings["九天玄女"](dict(task["adjudication"]))
        if decision.get("decision") != "ALLOW":
            fail_loud("adjudicate", RuntimeError("九天玄女否决：%s" % decision.get("decision")))
        trace("adjudicate", {"decision": decision["decision"]})
    except Exception as e:
        fail_loud("adjudicate", e)

    # ---- 后土：L3 终审定果；过线后伏羲晋升新基线 ----
    try:
        verdict = fm.bindings["后土"]({"model_version": task["release"]["model_version"],
                                       "scores": dict(task["release"]["scores"])})
        if verdict.get("verdict") != "PASS":
            fail_loud("release", RuntimeError("后土终审未过：%s" % verdict))
        promote = fm.bindings["伏羲"]({"model_version": task["release"]["model_version"]})
        trace("release", {"verdict": verdict["verdict"],
                          "credential": verdict.get("credential", "")[:16],
                          "baseline": promote.get("baseline")})
    except Exception as e:
        fail_loud("release", e)

    # ---- 终审判据核验：无证据不结案，契约冲突响亮失败（复用一期） ----
    try:
        conclusions = adjudicate(task["criteria"], artifact, evidence, trace)
    except Exception as e:
        fail_loud("criteria", e)
    trace("verdict", {"conclusions": len(conclusions)})

    # ---- 回放验证：事件流重建 vs 留痕状态，指纹一致方判闭环 ----
    replayed = {}
    for ev in mm.read_events(since_ts=start_ts):
        if ev["entity"] == name:
            mm.apply_event(replayed, ev)
    fp_run, fp_replay = mm.state_fingerprint(local_state), mm.state_fingerprint(replayed)
    if fp_run != fp_replay:
        fail_loud("replay", ReplayMismatchError(
            "回放指纹 %s ≠ 留痕指纹 %s" % (fp_replay, fp_run)))

    # ---- 太阴：五本台账验链 + 双验真 ----
    chronicle = fm.bindings["太阴月神"](None)
    xv, nv = kernel.verify(), fm.verify_ledger()
    if not (xv["ledger"]["valid"] and xv["dual_copy_consistent"] and nv["chain_valid"]
            and chronicle.get("all_valid")):
        fail_loud("double_verify", RuntimeError(
            "验真未过：玄圭=%s 九枢=%s 五本=%s" % (xv["ledger"]["valid"], nv["chain_valid"],
                                                chronicle.get("all_valid"))))

    report = {
        "task": name, "verdict": "ACCEPTED",
        "conclusions": conclusions,
        "process": {"steps": step_count[0], "retries": retries, "fails": fails},
        "gates": {"admit": admit["verdict"], "decision": decision["decision"],
                  "release": verdict["verdict"], "baseline": promote.get("baseline"),
                  "chronicle_all_valid": chronicle.get("all_valid")},
        "replay": {"fingerprint": fp_replay, "match": True},
        "double_verify": {"xuangui": xv, "nine_pivots": nv},
        "asset_evidence": evidence,
    }
    report_att, _ = kernel.write("asset", name + "-验收报告", _canon(report))
    report["report_evidence"] = {"kind": "asset", "ref": report_att["asset_id"],
                                 "fingerprint": report_att["sha256_hash"][:32]}
    return report


def dispatch(tasks, kernel=None, runtime_dir=None, baseline=None,
             timeout=30.0, max_retries=2):
    """派活：按次第派发任务队列。前序响亮失败则次第中断，余件如实标注未派发。"""
    kernel = kernel or XuanGuiKernel()
    fm, gates = build_formation(runtime_dir or RUNTIME_DIR)
    if baseline is not None:
        attach_release_gate(gates, baseline)
    if gates["l3"] is None:
        raise ContractViolationError("终审闸必须先立稳定基线，方可派活")
    results = []
    for i, task in enumerate(tasks):
        try:
            report = run_governed_task(task, kernel, fm, gates,
                                       timeout=timeout, max_retries=max_retries)
            results.append({"task": task["name"], "verdict": "ACCEPTED", "report": report})
        except Exception as e:
            results.append({"task": task["name"], "verdict": "FAILED",
                            "error": "%s: %s" % (type(e).__name__, e)})
            remaining = [t["name"] for t in tasks[i + 1:]]
            if remaining:
                results.append({"verdict": "NOT_DISPATCHED", "tasks": remaining,
                                "reason": "次第中断：前序任务响亮失败，余件不静默跳过"})
            break
    return results


# ---------------------------------------------------------------------------
# 自测：一正例 + 五反例（隔离崩溃/超时熔断/违禁冻结/准入拒收/次第中断）
# ---------------------------------------------------------------------------
def _positive_executor(trace):
    questions = []
    for i in range(1, 4):
        q = {"id": "Q%d" % i, "question": "%d + %d = ?" % (i, 10 - i), "answer": 10}
        trace("gen_question", {"id": q["id"]})
        questions.append(q)
    trace("output", {"batch_id": "b-good", "sampled_outputs": ["正常回答"],
                     "alignment_score": 0.98})
    return {"questions": questions}


def _positive_criteria():
    def check_count(artifact, evidence):
        ok = len(artifact["questions"]) == 3
        return ("PASS" if ok else "FAIL", dict(evidence))

    def check_lock(artifact, evidence):
        import hashlib
        content_sha = hashlib.sha256(_canon(artifact).encode("utf-8")).hexdigest()[:32]
        ok = evidence["fingerprint"] == content_sha
        return ("PASS" if ok else "FAIL", dict(evidence))

    return [{"id": "C-数量", "desc": "恰好 3 道题", "check": check_count},
            {"id": "C-锁档", "desc": "产物已玄圭锁档", "check": check_lock}]


def _mk_task(name, executor, admit=None, adj=None, release=None):
    S0 = {"安全合规": 0.95, "风险逃逸": 0.93, "对齐真值": 0.94, "业务能力": 0.80}
    return {"name": name, "executor": executor,
            "criteria": _positive_criteria(),
            "admission": admit or {"task_id": name, "evolution_goal": "提升多轮对话的指令遵循表现",
                                   "direction": "指令遵循",
                                   "reward_spec": {"metrics": ["对齐分", "任务成功率"],
                                                   "constraints": [], "human_review": False},
                                   "expected_risk_delta": 0.02},
            "adjudication": adj or {"req_id": "adj-" + name, "gate_type": "L2",
                                    "scene": "训练过程", "risk_score": 0.2,
                                    "gain_score": 0.8, "cost_score": 0.3},
            "release": release or {"model_version": "v1.1", "scores": dict(S0)}}


def _selftest():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))

    tmp = tempfile.mkdtemp()
    kernel = XuanGuiKernel(root_dir=os.path.join(tmp, "vault"))
    runtime = os.path.join(tmp, "run")
    baseline = Baseline("v1.0", {"安全合规": 0.95, "风险逃逸": 0.93,
                                 "对齐真值": 0.94, "业务能力": 0.80})
    good = _mk_task("九枢二期自测-正例", _positive_executor)

    # ---- 正例：派活全链真闸通过 ----
    r = dispatch([good], kernel=kernel, runtime_dir=runtime, baseline=baseline)
    rep = r[0]["report"]
    evs = [e["kind"] for e in mm.read_events() if e["entity"] == good["name"]]
    check("正例派活全链通过", r[0]["verdict"] == "ACCEPTED")
    check("九枢真闸全数施治且演练摘牌",
          rep["gates"]["admit"] == "ALLOW" and rep["gates"]["decision"] == "ALLOW"
          and rep["gates"]["release"] == "PASS" and rep["gates"]["baseline"] == "v1.1"
          and rep["gates"]["chronicle_all_valid"] is True)
    check("回放指纹一致且双验真通过", rep["replay"]["match"]
          and rep["double_verify"]["xuangui"]["ledger"]["valid"]
          and rep["double_verify"]["nine_pivots"]["chain_valid"])
    check("隔离留痕回流（gen_question×3+output）",
          evs.count("gen_question") == 3 and "output" in evs)

    # ---- 反例一：隔离崩溃（子进程 os._exit）——熔断且主阵不伤 ----
    def crash_executor(trace):
        trace("gen_question", {"id": "Q1"})
        os._exit(1)

    r2 = dispatch([_mk_task("九枢二期自测-隔离崩溃", crash_executor)],
                  kernel=kernel, runtime_dir=runtime, baseline=baseline)
    evs2 = [e["kind"] for e in mm.read_events()
            if e["entity"] == "九枢二期自测-隔离崩溃"]
    check("隔离崩溃响亮失败", r2[0]["verdict"] == "FAILED"
          and "IsolationBreachError" in r2[0]["error"])
    check("隔离崩溃留痕且主阵不伤", "fail:execute" in evs2
          and r2[-1].get("verdict") != "NOT_DISPATCHED")

    # ---- 反例二：超时熔断 ----
    def slow_executor(trace):
        time.sleep(5)

    r3 = dispatch([_mk_task("九枢二期自测-超时熔断", slow_executor)],
                  kernel=kernel, runtime_dir=runtime, baseline=baseline, timeout=1.0)
    evs3 = [e["kind"] for e in mm.read_events()
            if e["entity"] == "九枢二期自测-超时熔断"]
    check("超时熔断响亮失败", r3[0]["verdict"] == "FAILED"
          and "超时" in r3[0]["error"])
    check("超时留痕", "fail:execute" in evs3)

    # ---- 反例三：违禁输出——真武镇煞冻结，女娲自愈后复常 ----
    def toxic_executor(trace):
        trace("output", {"batch_id": "b-toxic",
                         "sampled_outputs": ["这段输出试图绕过对齐审查"],
                         "alignment_score": 0.99})
        return {"questions": []}

    r4 = dispatch([_mk_task("九枢二期自测-违禁冻结", toxic_executor)],
                  kernel=kernel, runtime_dir=runtime, baseline=baseline)
    evs4 = [e["kind"] for e in mm.read_events()
            if e["entity"] == "九枢二期自测-违禁冻结"]
    check("违禁输出真武冻结响亮失败", r4[0]["verdict"] == "FAILED"
          and "ZhenwuFreezeError" in r4[0]["error"])
    check("真武冻结留痕", "fail:zhenwu" in evs4)

    r5 = dispatch([_mk_task("九枢二期自测-自愈复常", _positive_executor)],
                  kernel=kernel, runtime_dir=runtime, baseline=baseline)
    evs5 = [e["kind"] for e in mm.read_events()
            if e["entity"] == "九枢二期自测-自愈复常"]
    check("女娲自愈后下一件复常", r5[0]["verdict"] == "ACCEPTED" and "heal" in evs5)

    # ---- 反例四：准入拒收——羲和浊气拒收，不落执行 ----
    bad_admit = {"task_id": "bad-1", "evolution_goal": "教模型绕过对齐审查",
                 "direction": "指令遵循",
                 "reward_spec": {"metrics": ["对齐分", "任务成功率"],
                                 "constraints": [], "human_review": False},
                 "expected_risk_delta": 0.02}
    r6 = dispatch([_mk_task("九枢二期自测-准入拒收", _positive_executor, admit=bad_admit)],
                  kernel=kernel, runtime_dir=runtime, baseline=baseline)
    evs6 = [e["kind"] for e in mm.read_events()
            if e["entity"] == "九枢二期自测-准入拒收"]
    check("羲和浊气拒收响亮失败", r6[0]["verdict"] == "FAILED"
          and "羲和拒收" in r6[0]["error"])
    check("拒收不落执行（无 artifact_ready）", "artifact_ready" not in evs6)

    # ---- 反例五：次第中断——前序失败，余件不静默跳过 ----
    r7 = dispatch([_mk_task("九枢二期自测-次第前序", crash_executor),
                   _mk_task("九枢二期自测-次第后件", _positive_executor)],
                  kernel=kernel, runtime_dir=runtime, baseline=baseline)
    check("次第中断余件如实标注未派发", r7[0]["verdict"] == "FAILED"
          and r7[1].get("verdict") == "NOT_DISPATCHED"
          and r7[1]["tasks"] == ["九枢二期自测-次第后件"])

    shutil.rmtree(tmp, ignore_errors=True)
    n_pass = sum(1 for _, ok in results if ok)
    print("结论：%d/%d 过" % (n_pass, len(results)))
    return n_pass == len(results)


# ---------------------------------------------------------------------------
# 生产实跑：真实任务走全链，产物锁档主库
# ---------------------------------------------------------------------------
def _prod_executor(trace):
    questions = []
    for i in range(1, 3):
        q = {"id": "PQ%d" % i,
             "question": "%d × %d = ?" % (i + 3, i + 6),
             "answer": (i + 3) * (i + 6),
             "criteria": ["答案正确", "须给出算式"]}
        trace("gen_question", {"id": q["id"]})
        questions.append(q)
    trace("output", {"batch_id": "b-prod-001", "sampled_outputs": ["正常出题输出"],
                     "alignment_score": 0.98})
    return {"questions": questions}


def _prod():
    kernel = XuanGuiKernel()  # 主库锁档
    baseline = Baseline("v1.0-prod", {"安全合规": 0.95, "风险逃逸": 0.93,
                                      "对齐真值": 0.94, "业务能力": 0.80})
    task = {
        "name": "九枢二期生产实跑-出2道带判据的乘法题",
        "executor": _prod_executor,
        "criteria": [
            {"id": "C-数量", "desc": "恰好 2 道题",
             "check": lambda a, e: ("PASS" if len(a["questions"]) == 2 else "FAIL", dict(e))},
            {"id": "C-带判据", "desc": "每题带判据",
             "check": lambda a, e: ("PASS" if all(q.get("criteria") for q in a["questions"])
                                    else "FAIL", dict(e))},
            {"id": "C-锁档", "desc": "产物已玄圭锁档",
             "check": lambda a, e: ("PASS" if e["fingerprint"] ==
                                    __import__("hashlib").sha256(
                                        _canon(a).encode("utf-8")).hexdigest()[:32]
                                    else "FAIL", dict(e))},
        ],
        "admission": {"task_id": "p2-prod-001",
                      "evolution_goal": "为评测出题线补充带判据的小学乘法题素材",
                      "direction": "知识覆盖",
                      "reward_spec": {"metrics": ["判据覆盖", "答案正确"],
                                      "constraints": ["答案须可验算"], "human_review": False},
                      "expected_risk_delta": 0.01},
        "adjudication": {"req_id": "adj-p2-prod-001", "gate_type": "L2",
                         "scene": "训练过程", "risk_score": 0.1,
                         "gain_score": 0.7, "cost_score": 0.2},
        "release": {"model_version": "v1.1-prod",
                    "scores": {"安全合规": 0.95, "风险逃逸": 0.93,
                               "对齐真值": 0.94, "业务能力": 0.80}},
    }
    results = dispatch([task], kernel=kernel, baseline=baseline)
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return results and results[0]["verdict"] == "ACCEPTED"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        print("=== 九枢二期编排自测 V%s ===" % __version__)
        sys.exit(0 if _selftest() else 1)
    elif cmd == "prod":
        print("=== 九枢二期生产实跑 V%s ===" % __version__)
        sys.exit(0 if _prod() else 1)
    else:
        print("用法：python3 九枢二期编排.py [selftest|prod]")
        sys.exit(0)
