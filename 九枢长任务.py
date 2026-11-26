#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九枢长任务 · 异步派发与断点续跑 V0.1

诚实定位：
  - 二期（九枢二期编排.py）解决「派活与隔离」，但派活是同步的——派出去原地等结果。
    本模块补长任务三件事：
      1) 异步派发：submit 派完即返回（任务号立取），工位子进程后台跑全链，
         跑完把验收报告锁档回填台账；
      2) 断点续跑：七步（准入/执行/锁档/裁决/终审/判据/验真）逐步落检查点，
         中途被杀后 resume 从上一步续跑，已完成步骤不重跑（留痕不重复）；
      3) 进度可查：status 随时报每件长活的进度（n/7）、当前步、耗时与产物号。
  - 复用不重写：闸口复用九枢执闸立阵、隔离执行复用二期 run_isolated、
    判据核验复用一期、锁档复用玄圭、留痕回放复用记忆机。
  - 断点粒度＝步：执行步不可半途续跑，只可整步重跑（留痕追加不覆盖，重试不静默）。
  - 工位口径：同一时刻建议单工位串行（闸口台账未做多工位并发锁），
    多工位并发留待三期；任务定义经任务文件或内建件传入，执行体在工位子进程隔离运行。
  - 测试钩子：任务定义 test_fail_after_step 仅用于自测模拟中途被杀，生产件禁用。

依赖：Python >= 3.7 标准库 + 体系既有组件。
运行： python3 九枢长任务.py selftest          （自测）
       python3 九枢长任务.py submit prod       （生产实跑：派内建慢工件）
       python3 九枢长任务.py submit <task.py>  （派任务文件：模块须定义 TASK）
       python3 九枢长任务.py status [task_id]  （进度可查）
       python3 九枢长任务.py resume <task_id> [--force]  （断点续跑）
"""

import fcntl
import importlib.util
import json
import os
import sys
import time

from 九枢执闸 import build_formation, attach_release_gate
from AxiomGateL3终审闸 import Baseline
from 九枢一期试点 import (mm, _canon, validate_task_def, adjudicate,      # noqa: F401
                          ContractViolationError, ReplayMismatchError)   # noqa: F401
from 九枢二期编排 import run_isolated, ZhenwuFreezeError, IsolationBreachError, GOVERN_FIELDS
from 玄圭试点 import XuanGuiKernel

__version__ = "0.1.1"

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(_HERE, "九枢执政")
LONG_DIR = os.path.join(RUNTIME_DIR, "long")
LEDGER_PATH = os.path.join(LONG_DIR, "long_tasks.json")

STEPS = ["admit", "execute", "lock", "adjudicate", "release", "criteria", "verify"]
HEARTBEAT_STALE = 600.0  # 心跳逾此秒数视为工位失联（执行步按 2 倍超时放宽）


# ---------------------------------------------------------------------------
# 长任务台账（文件 + 文件锁，跨进程共享）
# ---------------------------------------------------------------------------
class JobLedger:
    def __init__(self, path=LEDGER_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path

    def _load(self):
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return {"jobs": []}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def update(self, task_id, fn):
        with open(self.path, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._load()
                job = next((j for j in data["jobs"] if j["task_id"] == task_id), None)
                if job is None:
                    raise KeyError("长任务 %s 不在台账" % task_id)
                fn(job)
                self._save(data)
                return dict(job)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def add(self, job):
        with open(self.path, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._load()
                data["jobs"].append(job)
                self._save(data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def get(self, task_id=None):
        data = self._load()
        if task_id is None:
            return data["jobs"]
        return [j for j in data["jobs"] if j["task_id"] == task_id]


# ---------------------------------------------------------------------------
# 检查点（逐步落盘，原子写）
# ---------------------------------------------------------------------------
class Checkpoint:
    def __init__(self, task_id):
        os.makedirs(LONG_DIR, exist_ok=True)
        self.path = os.path.join(LONG_DIR, task_id + ".ckpt.json")
        self.lock_path = self.path + ".lock"  # v0.1.1：检查点跨进程写锁
        self.data = {"steps": {}, "events": [], "artifact": None, "start_ts": None}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)

    def has(self, step):
        return step in self.data["steps"]

    def save(self):
        with open(self.lock_path, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# 工位：七步全链，逐步落检查点
# ---------------------------------------------------------------------------
def _worker(task_id, task):
    """工位主流程。任何未捕获异常 → 台账 FAILED 后退出。"""
    ledger = JobLedger()

    def set_job(**kw):
        return ledger.update(task_id, lambda j: j.update(kw))

    ckpt = Checkpoint(task_id)
    kernel = XuanGuiKernel()  # 主库锁档
    fm, gates = build_formation(RUNTIME_DIR)
    baseline = task.get("baseline")
    if baseline:
        attach_release_gate(gates, Baseline(baseline["version"], dict(baseline["scores"])))
    if gates["l3"] is None:
        raise ContractViolationError("长任务必须自带稳定基线（baseline.version/scores）")

    if ckpt.data["start_ts"] is None:
        ckpt.data["start_ts"] = mm.now_ts()
        ckpt.save()
    start_ts = ckpt.data["start_ts"]
    local_state = {}
    for ev in ckpt.data["events"]:
        mm.apply_event(local_state, ev)

    def record(kind, detail):
        ev = mm.record(task["name"], kind, detail)
        mm.apply_event(local_state, ev)
        ckpt.data["events"].append(ev)
        ckpt.save()
        return ev

    def fail_loud(stage, exc):
        try:
            record("fail:" + stage, {"error": "%s: %s" % (type(exc).__name__, exc)})
            fm.ledger.append("task_fail", {"task": task_id, "stage": stage, "error": str(exc)})
        except Exception:
            pass
        set_job(status="FAILED", error="%s: %s" % (type(exc).__name__, exc),
                finished_at=time.time())
        raise exc

    def trace(kind, detail):
        if kind == "output":
            act = gates["monitor"].ingest_batch(detail)
            if act.get("action") in ("FREEZE", "HALTED"):
                raise ZhenwuFreezeError("真武镇煞：输出批 %s 判 %s"
                                        % (detail.get("batch_id"), act.get("action")))
        return record(kind, detail)

    def done(step, out):
        ckpt.data["steps"][step] = out
        ckpt.save()
        set_job(status="RUNNING", ckpt_step=step, heartbeat=time.time(),
                progress="%d/%d" % (len(ckpt.data["steps"]), len(STEPS)))
        if task.get("test_fail_after_step") == step:
            os._exit(9)  # 测试钩子：模拟工位中途被杀（生产件禁用）

    def step_admit():
        admit = fm.bindings["羲和"](dict(task["admission"]))
        if admit.get("verdict") != "ALLOW":
            fail_loud("admit", RuntimeError("羲和拒收：%s" % admit.get("reason")))
        record("admit", {"verdict": admit["verdict"], "token": admit.get("token", "")[:16]})
        done("admit", {"verdict": admit["verdict"]})

    def step_execute():
        max_retries = int(task.get("max_retries", 2))
        artifact, retries, fails = None, 0, 0
        for attempt in range(1, max_retries + 2):
            try:
                artifact = run_isolated(task["name"], task["executor"], trace,
                                        timeout=float(task.get("timeout", 30.0)))
                break
            except (IsolationBreachError, RuntimeError, ZhenwuFreezeError) as e:
                fails += 1
                record("step_fail", {"attempt": attempt,
                                     "error": "%s: %s" % (type(e).__name__, e)})
                if isinstance(e, ZhenwuFreezeError) or attempt > max_retries:
                    fail_loud("execute", e)
                retries += 1
                record("step_retry", {"from_attempt": attempt, "to_attempt": attempt + 1})
        record("artifact_ready", {"summary": _canon(artifact)[:200]})
        ckpt.data["artifact"] = artifact
        done("execute", {"summary": _canon(artifact)[:200]})

    def step_lock():
        artifact = ckpt.data["artifact"]
        att, _exp = kernel.write("asset", task["name"], _canon(artifact))
        evidence = {"kind": "asset", "ref": att["asset_id"],
                    "fingerprint": att["sha256_hash"][:32]}
        record("lock", evidence)
        done("lock", evidence)

    def step_adjudicate():
        decision = fm.bindings["九天玄女"](dict(task["adjudication"]))
        if decision.get("decision") != "ALLOW":
            fail_loud("adjudicate", RuntimeError("九天玄女否决：%s" % decision.get("decision")))
        record("adjudicate", {"decision": decision["decision"]})
        done("adjudicate", {"decision": decision["decision"]})

    def step_release():
        verdict = fm.bindings["后土"]({"model_version": task["release"]["model_version"],
                                       "scores": dict(task["release"]["scores"])})
        if verdict.get("verdict") != "PASS":
            fail_loud("release", RuntimeError("后土终审未过：%s" % verdict))
        promote = fm.bindings["伏羲"]({"model_version": task["release"]["model_version"]})
        record("release", {"verdict": verdict["verdict"],
                           "credential": verdict.get("credential", "")[:16],
                           "baseline": promote.get("baseline")})
        done("release", {"verdict": verdict["verdict"], "baseline": promote.get("baseline")})

    def step_criteria():
        conclusions = adjudicate(task["criteria"], ckpt.data["artifact"],
                                 ckpt.data["steps"]["lock"], trace)
        record("verdict", {"conclusions": len(conclusions)})
        done("criteria", {"conclusions": conclusions})

    def step_verify():
        replayed = {}
        for ev in mm.read_events(since_ts=start_ts):
            if ev["entity"] == task["name"]:
                mm.apply_event(replayed, ev)
        fp_run, fp_replay = mm.state_fingerprint(local_state), mm.state_fingerprint(replayed)
        if fp_run != fp_replay:
            fail_loud("replay", ReplayMismatchError(
                "回放指纹 %s ≠ 留痕指纹 %s" % (fp_replay, fp_run)))
        chronicle = fm.bindings["太阴月神"](None)
        xv, nv = kernel.verify(), fm.verify_ledger()
        if not (xv["ledger"]["valid"] and xv["dual_copy_consistent"] and nv["chain_valid"]
                and chronicle.get("all_valid")):
            fail_loud("double_verify", RuntimeError(
                "验真未过：玄圭=%s 九枢=%s 五本=%s" % (xv["ledger"]["valid"],
                                                nv["chain_valid"], chronicle.get("all_valid"))))
        steps_done = len(ckpt.data["steps"])
        report = {
            "task": task["name"], "task_id": task_id, "verdict": "ACCEPTED",
            "conclusions": ckpt.data["steps"]["criteria"]["conclusions"],
            "process": {"steps": steps_done, "resumed": steps_done < len(STEPS)},
            "gates": {"admit": ckpt.data["steps"]["admit"]["verdict"],
                      "decision": ckpt.data["steps"]["adjudicate"]["decision"],
                      "release": ckpt.data["steps"]["release"]["verdict"],
                      "baseline": ckpt.data["steps"]["release"]["baseline"],
                      "chronicle_all_valid": chronicle.get("all_valid")},
            "replay": {"fingerprint": fp_replay, "match": True},
            "double_verify": {"xuangui": xv, "nine_pivots": nv},
            "asset_evidence": ckpt.data["steps"]["lock"],
        }
        report_att, _ = kernel.write("asset", task["name"] + "-验收报告", _canon(report))
        report["report_evidence"] = {"kind": "asset", "ref": report_att["asset_id"],
                                     "fingerprint": report_att["sha256_hash"][:32]}
        done("verify", {"asset_ref": ckpt.data["steps"]["lock"]["ref"],
                        "report_ref": report_att["asset_id"]})
        set_job(status="DONE", finished_at=time.time(),
                asset_ref=ckpt.data["steps"]["lock"]["ref"],
                report_ref=report_att["asset_id"], error="")

    runners = {"admit": step_admit, "execute": step_execute, "lock": step_lock,
               "adjudicate": step_adjudicate, "release": step_release,
               "criteria": step_criteria, "verify": step_verify}
    for step in STEPS:
        if ckpt.has(step):
            continue  # 断点续跑：已完成步骤不重跑
        try:
            runners[step]()
        except Exception:
            os._exit(1)  # fail_loud 已留痕并记账


# ---------------------------------------------------------------------------
# 派发 / 查询 / 续跑
# ---------------------------------------------------------------------------
def _load_task(source):
    if source == "prod":
        return _prod_task()
    spec = importlib.util.spec_from_file_location("jiushu_long_task", source)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TASK


# 进程内任务登记（fork 继承，供自测与同进程 resume 使用）
_TASKS = {}


def _task_for(job):
    if job["task_id"] in _TASKS:
        return _TASKS[job["task_id"]]
    return _load_task(job["source"])


def _validate_govern(task, external=True):
    validate_task_def(task)
    for field in GOVERN_FIELDS:
        if not isinstance(task.get(field), dict):
            raise ContractViolationError("任务定义缺 %s（九枢治理契约）" % field)
    if not isinstance(task.get("baseline"), dict):
        raise ContractViolationError("任务定义缺 baseline（终审闸须先立稳定基线）")
    if external and task.get("test_fail_after_step") is not None:
        raise ContractViolationError("test_fail_after_step 为自测钩子，生产件禁用")


def submit(source):
    """异步派发：立台账即返回任务号，工位子进程后台跑全链。
    source 可为 'prod'、任务文件路径，或进程内任务 dict（fork 继承）。"""
    external = not isinstance(source, dict)
    task = _load_task(source) if external else source
    _validate_govern(task, external=external)
    source_label = "in-process" if isinstance(source, dict) else source
    ledger = JobLedger()
    seq = len(ledger.get()) + 1
    task_id = "LT-%04d" % seq
    ledger.add({"task_id": task_id, "name": task["name"], "source": source_label,
                "status": "QUEUED", "submitted_at": time.time(),
                "heartbeat": time.time(), "ckpt_step": "", "progress": "0/%d" % len(STEPS),
                "timeout": float(task.get("timeout", 30.0)),
                "asset_ref": "", "report_ref": "", "error": "", "finished_at": None})
    _TASKS[task_id] = task
    sys.stdout.flush()  # fork 前冲刷，避免子进程重复吐父进程缓冲
    pid = os.fork()
    if pid != 0:  # v0.1.1：仅父进程登记工位号（子进程写会竞态覆盖）
        ledger.update(task_id, lambda j: j.update(pid=pid))
    if pid == 0:  # 工位子进程：脱离进程组，父进程退出后仍独立跑完
        os.setsid()
        try:
            ledger.update(task_id, lambda j: j.update(status="RUNNING", heartbeat=time.time()))
            _worker(task_id, task)
            os._exit(0)
        except SystemExit:
            raise
        except Exception as e:  # 兜底：未留痕异常也如实记账
            try:
                ledger.update(task_id, lambda j: j.update(
                    status="FAILED", error="%s: %s" % (type(e).__name__, e),
                    finished_at=time.time()))
            except Exception:
                pass
            os._exit(1)
    return task_id


def reap_stale(ledger):
    """工位失联（心跳逾刻）如实标注中断，不静默。"""
    now = time.time()
    for job in ledger.get():
        if job["status"] != "RUNNING":
            continue
        stale_after = max(HEARTBEAT_STALE, 2.0 * float(job.get("timeout", 30.0)))
        if now - job.get("heartbeat", 0) > stale_after:
            ledger.update(job["task_id"], lambda j: j.update(
                status="INTERRUPTED",
                error="工位失联：心跳逾 %.0fs，可 resume 断点续跑" % stale_after))


def status(task_id=None):
    ledger = JobLedger()
    reap_stale(ledger)
    jobs = ledger.get(task_id)
    out = []
    for j in jobs:
        elapsed = (j.get("finished_at") or time.time()) - j["submitted_at"]
        out.append({"task_id": j["task_id"], "name": j["name"], "status": j["status"],
                    "progress": j.get("progress", ""), "step": j.get("ckpt_step", ""),
                    "elapsed_sec": round(elapsed, 1),
                    "asset_ref": j.get("asset_ref", ""), "report_ref": j.get("report_ref", ""),
                    "error": j.get("error", "")})
    return out


def _pid_alive(pid):
    """工位存活判定：僵尸态（Z）视为已退出（父进程未 wait 时 os.kill 仍报存在）。"""
    try:
        with open("/proc/%d/stat" % pid, "r") as f:
            state = f.read().rsplit(")", 1)[-1].split()[0]
            return state != "Z"
    except (IOError, OSError, IndexError):
        return False  # 进程不存在即已退出


def resume(task_id, force=False):
    """断点续跑：从检查点续跑，已完成步骤不重跑。"""
    ledger = JobLedger()
    jobs = ledger.get(task_id)
    if not jobs:
        raise KeyError("长任务 %s 不在台账" % task_id)
    job = jobs[0]
    if job["status"] == "DONE":
        return {"task_id": task_id, "resumed": False, "reason": "已完成，无需续跑"}
    stale_after = max(HEARTBEAT_STALE, 2.0 * float(job.get("timeout", 30.0)))
    stale = time.time() - job.get("heartbeat", 0) > stale_after
    if job["status"] not in ("INTERRUPTED", "FAILED") and not (stale or force):
        raise RuntimeError("工位疑似仍在跑（心跳 %.0fs 前），确认失联用 --force" % (
            time.time() - job.get("heartbeat", 0)))
    # v0.1.1：强制续跑前验旧工位存活，杜绝双工位并发写同一检查点
    old_pid = job.get("pid")
    if force and old_pid and old_pid > 0:
        alive = False
        deadline = time.time() + 3.0  # 落检查点到进程退出有毫秒级间隙，给宽限
        while time.time() < deadline:
            if not _pid_alive(old_pid):
                alive = False
                break
            alive = True
            time.sleep(0.2)
        if alive:
            raise RuntimeError(
                "旧工位 pid=%d 仍在运行，拒绝强制续跑（先确认其退出再 resume）" % old_pid)
    ckpt = Checkpoint(task_id)
    task = _task_for(job)
    if task.get("test_fail_after_step"):
        task = dict(task, test_fail_after_step=None)  # 续跑时摘除自测钩子
    ledger.update(task_id, lambda j: j.update(status="RUNNING", heartbeat=time.time(),
                                              error=""))
    sys.stdout.flush()  # fork 前冲刷，避免子进程重复吐父进程缓冲
    pid = os.fork()
    if pid != 0:  # v0.1.1：续跑工位同样由父进程登记
        ledger.update(task_id, lambda j: j.update(pid=pid))
    if pid == 0:
        os.setsid()  # 脱离进程组，父进程退出后仍独立跑完
        try:
            _worker(task_id, task)
            os._exit(0)
        except SystemExit:
            raise
        except Exception as e:
            try:
                ledger.update(task_id, lambda j: j.update(
                    status="FAILED", error="%s: %s" % (type(e).__name__, e),
                    finished_at=time.time()))
            except Exception:
                pass
            os._exit(1)
    return {"task_id": task_id, "resumed": True, "from_step": job.get("ckpt_step", "")}


# ---------------------------------------------------------------------------
# 内建生产件与自测
# ---------------------------------------------------------------------------
def _prod_task():
    def executor(trace):
        time.sleep(4)  # 慢工：模拟长活
        questions = []
        for i in range(1, 4):
            q = {"id": "LQ%d" % i, "question": "%d + %d = ?" % (i, 20 - i), "answer": 20,
                 "criteria": ["答案等于 20", "须给出算式"]}
            trace("gen_question", {"id": q["id"]})
            questions.append(q)
        trace("output", {"batch_id": "b-long-001", "sampled_outputs": ["正常出题输出"],
                         "alignment_score": 0.98})
        return {"questions": questions}

    def check_count(artifact, evidence):
        return ("PASS" if len(artifact["questions"]) == 3 else "FAIL", dict(evidence))

    def check_lock(artifact, evidence):
        import hashlib
        sha = __import__("hashlib").sha256(_canon(artifact).encode("utf-8")).hexdigest()[:32]
        return ("PASS" if evidence["fingerprint"] == sha else "FAIL", dict(evidence))

    return {
        "name": "九枢长任务生产实跑-慢工出3道带判据的题",
        "executor": executor,
        "criteria": [{"id": "C-数量", "desc": "恰好 3 道题", "check": check_count},
                     {"id": "C-锁档", "desc": "产物已玄圭锁档", "check": check_lock}],
        "admission": {"task_id": "lt-prod-001",
                      "evolution_goal": "为评测出题线慢工补充带判据的加法题素材",
                      "direction": "知识覆盖",
                      "reward_spec": {"metrics": ["判据覆盖", "答案正确"],
                                      "constraints": ["答案须可验算"], "human_review": False},
                      "expected_risk_delta": 0.01},
        "adjudication": {"req_id": "adj-lt-prod-001", "gate_type": "L2", "scene": "训练过程",
                         "risk_score": 0.1, "gain_score": 0.7, "cost_score": 0.2},
        "release": {"model_version": "v1.1-long",
                    "scores": {"安全合规": 0.95, "风险逃逸": 0.93,
                               "对齐真值": 0.94, "业务能力": 0.80}},
        "baseline": {"version": "v1.0-long",
                     "scores": {"安全合规": 0.95, "风险逃逸": 0.93,
                                "对齐真值": 0.94, "业务能力": 0.80}},
        "timeout": 30.0, "max_retries": 2,
    }


def _wait_done(task_id, deadline_sec=60.0):
    deadline = time.time() + deadline_sec
    while time.time() < deadline:
        jobs = status(task_id)
        if jobs and jobs[0]["status"] in ("DONE", "FAILED", "INTERRUPTED"):
            return jobs[0]
        time.sleep(0.3)
    raise TimeoutError("长任务 %s 未在 %.0fs 内收尾" % (task_id, deadline_sec))


def _selftest():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))

    tmp = tempfile.mkdtemp()
    S0 = {"安全合规": 0.95, "风险逃逸": 0.93, "对齐真值": 0.94, "业务能力": 0.80}

    def mk_task(name, executor, timeout=30.0, fail_after=None):
        return {"name": name, "executor": executor, "timeout": timeout,
                "test_fail_after_step": fail_after,
                "criteria": [{"id": "C-数量", "desc": "恰好 3 道题",
                              "check": lambda a, e: ("PASS" if len(a["questions"]) == 3
                                                     else "FAIL", dict(e))}],
                "admission": {"task_id": name, "evolution_goal": "提升多轮对话的指令遵循表现",
                              "direction": "指令遵循",
                              "reward_spec": {"metrics": ["对齐分", "任务成功率"],
                                              "constraints": [], "human_review": False},
                              "expected_risk_delta": 0.02},
                "adjudication": {"req_id": "adj-" + name, "gate_type": "L2",
                                 "scene": "训练过程", "risk_score": 0.2,
                                 "gain_score": 0.8, "cost_score": 0.3},
                "release": {"model_version": "v1.1", "scores": dict(S0)},
                "baseline": {"version": "v1.0", "scores": dict(S0)}}

    def slow_executor(trace):
        time.sleep(2.5)
        return _prod_task()["executor"](trace)

    # ---- 正例：异步派发 + 进度可查 + 跑完收报告 ----
    t0 = time.time()
    tid = submit(mk_task("九枢长任务自测-慢工正例", slow_executor))
    submit_latency = time.time() - t0
    st = status(tid)[0]
    check("派发即返回（<1.5s）且后台在跑",
          submit_latency < 1.5 and st["status"] in ("QUEUED", "RUNNING"))
    check("进度可查（步数在 0/7 与 7/7 之间）",
          st["progress"].split("/")[0].isdigit())
    fin = _wait_done(tid)
    check("长活跑完验收通过", fin["status"] == "DONE" and fin["progress"] == "7/7"
          and fin["asset_ref"] and fin["report_ref"])

    # ---- 反例：中途被杀 → 断点续跑，已完成步骤不重跑 ----
    run_ts = time.time()  # 事件统计只认本轮，历史同名任务留痕不计入
    tid2 = submit(mk_task("九枢长任务自测-断点续跑", slow_executor, fail_after="admit"))
    deadline = time.time() + 20
    killed = False
    while time.time() < deadline:
        ck = Checkpoint(tid2)
        if ck.has("admit") and not ck.has("execute"):
            killed = True  # 测试钩子在 admit 落盘后即杀工位，execute 必未落
            break
        time.sleep(0.2)
    check("中途被杀留检查点（admit 已落，execute 未落）", killed)
    r = resume(tid2, force=True)
    fin2 = _wait_done(tid2)
    evs = [e["kind"] for e in mm.read_events(since_ts=run_ts)
           if e["entity"] == "九枢长任务自测-断点续跑"]
    check("断点续跑收尾验收通过", r.get("resumed") and fin2["status"] == "DONE")
    check("已完成步骤不重跑（admit/lock 各只一次）",
          evs.count("admit") == 1 and evs.count("lock") == 1)

    # ---- 反例：工位失联如实标注中断 ----
    tid3 = submit(mk_task("九枢长任务自测-失联标注", slow_executor, fail_after="admit"))
    deadline = time.time() + 20
    while time.time() < deadline:
        ck = Checkpoint(tid3)
        if ck.has("admit") and not ck.has("execute"):
            break
        time.sleep(0.2)
    time.sleep(1.1)
    ledger = JobLedger()
    ledger.update(tid3, lambda j: j.update(heartbeat=j["heartbeat"] - 10 ** 6))  # 造失联
    st3 = status(tid3)[0]
    check("工位失联如实标注中断可续跑", st3["status"] == "INTERRUPTED"
          and "resume" in st3["error"])

    shutil.rmtree(tmp, ignore_errors=True)
    n_pass = sum(1 for _, ok in results if ok)
    print("结论：%d/%d 过" % (n_pass, len(results)))
    return n_pass == len(results)


def _heartbeat_age(job):
    return time.time() - job.get("heartbeat", 0)


def _main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "selftest":
        print("=== 九枢长任务自测 V%s ===" % __version__)
        sys.exit(0 if _selftest() else 1)
    elif cmd == "submit":
        tid = submit(sys.argv[2] if len(sys.argv) > 2 else "prod")
        print(json.dumps({"submitted": tid}, ensure_ascii=False))
    elif cmd == "status":
        print(json.dumps(status(sys.argv[2] if len(sys.argv) > 2 else None),
                         ensure_ascii=False, indent=1))
    elif cmd == "resume":
        force = "--force" in sys.argv
        tid = sys.argv[2]
        print(json.dumps(resume(tid, force=force), ensure_ascii=False))
        if len(sys.argv) > 2:
            print(json.dumps(_wait_done(tid, deadline_sec=120.0), ensure_ascii=False, indent=1))
    else:
        print("用法：python3 九枢长任务.py [selftest|submit prod|<task.py>|status|resume <id>]")
        sys.exit(0)


if __name__ == "__main__":
    _main()
