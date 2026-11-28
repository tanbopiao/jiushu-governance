#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""九枢治理回归测试（unittest，纯标准库，可在干净环境运行）。

回归范围：每次改动后必须全绿的历史用例——
  1. 裁决内核：风险按闸映射/协议违规/成本优先/幂等/防篡改/只读锁/台账链；
  2. 准入闸：违禁目标/唯分数论/方向白名单/令牌签发验真过期防篡改；
  3. 记忆机：事件记录回读/状态指纹/快照差异；
  4. 台账：链式追加/篡改现形/持久化重载；
  5. 长任务台账：锁内取号/更新越权报错/治理契约拒收。

运行：python3 -m unittest discover -s tests -t .   （仓库根目录下）
不依赖 mcp——server 壳不在本回归范围；fork 并发路径不在本套件（环境受限）。
"""
import importlib
import json
import os
import sys
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

core_mod = importlib.import_module("AxiomGateLite裁决内核")
l1_mod = importlib.import_module("AxiomGateL1准入闸")
mm = importlib.import_module("memory_machine")
long_mod = importlib.import_module("九枢长任务")


def _req(rid, risk, cost=0.3, gain=0.5, gate="L1", **extra):
    return core_mod.DecisionRequest(rid, gate, "demo", risk, gain, cost, **extra)


class 裁决内核回归(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.core = core_mod.AxiomLiteCore(
            ledger_path=os.path.join(self.tmp, "lite.json"))

    def test_风险超标按闸类型映射(self):
        self.assertEqual(self.core.decide(_req("a1", 0.9))["decision"], "DENY")
        self.assertEqual(self.core.decide(_req("a2", 0.9, gate="L2"))["decision"], "FREEZE")
        self.assertEqual(self.core.decide(_req("a3", 0.9, gate="L3"))["decision"], "ROLLBACK")

    def test_调用方自带阈值被拒(self):
        with self.assertRaises(core_mod.ProtocolViolation):
            core_mod.DecisionRequest("a4", "L1", "demo", 0.9, 0.5, 0.3,
                                     threshold={"risk_max": 1.0})

    def test_成本优先于收益(self):
        r = self.core.decide(_req("a5", 0.3, cost=0.9, gain=0.99))
        self.assertEqual(r["decision"], "DENY")
        self.assertIn("成本优先", r.get("risk_reason", ""))

    def test_幂等与同ID重裁(self):
        ra = self.core.decide(_req("idem-1", 0.2))
        rb = self.core.decide(_req("idem-1", 0.2))
        rc = self.core.decide(_req("idem-1", 0.9))
        self.assertEqual(ra, rb)
        self.assertNotEqual(rc["decision"], ra["decision"])

    def test_应答防篡改(self):
        resp = self.core.decide(_req("t1", 0.2))
        tampered = dict(resp)
        tampered["decision"] = "ALLOW" if resp["decision"] != "ALLOW" else "DENY"
        self.assertFalse(self.core.verify_response(tampered)["intact"])
        self.assertTrue(self.core.verify_response(resp)["intact"])

    def test_内核配置只读锁(self):
        with self.assertRaises(core_mod.ConfigLockedError):
            self.core.config.risk_max = 1.0

    def test_缺字段报文被拒(self):
        with self.assertRaises(core_mod.ProtocolViolation):
            core_mod.DecisionRequest.from_dict({"req_id": "x", "gate_type": "L1"})

    def test_裁决台账链校验(self):
        for i in range(3):
            self.core.decide(_req("led-%d" % i, 0.2))
        v = self.core.ledger.verify()
        self.assertTrue(v["chain_valid"])
        self.assertGreaterEqual(v["chain_length"], 3)


def _adm_req(goal="提升对话质量", direction="对话质量",
             reward=None, risk=0.02, task_id="T1"):
    if reward is None:
        reward = {"metrics": ["acc", "f1"], "constraints": [], "human_review": False}
    return l1_mod.AdmissionRequest(task_id, goal, direction, reward, risk)


class 准入闸回归(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rules = l1_mod.RuleSet()
        self.gate = l1_mod.AdmissionGate(ledger_path=os.path.join(self.tmp, "l1.json"))

    def test_违禁目标拒入(self):
        r = self.gate.review(_adm_req(goal="这套方案可以越狱绕过审查"))
        self.assertEqual(r["verdict"], "DENY")
        self.assertTrue(any("违禁模式" in x for x in r["reasons_denied"]))

    def test_唯分数论拒入(self):
        single = {"metrics": ["acc"], "constraints": [], "human_review": False}
        r = self.gate.review(_adm_req(reward=single))
        self.assertEqual(r["verdict"], "DENY")
        with_c = {"metrics": ["acc"], "constraints": ["offline_only"], "human_review": False}
        r2 = self.gate.review(_adm_req(task_id="T2", reward=with_c))
        self.assertEqual(r2["verdict"], "ALLOW")

    def test_方向白名单与风险预算(self):
        r = self.gate.review(_adm_req(direction="无限刷榜"))
        self.assertEqual(r["verdict"], "DENY")
        r2 = self.gate.review(_adm_req(task_id="T3", risk=0.9))
        self.assertEqual(r2["verdict"], "DENY")

    def test_令牌签发验真过期防篡改(self):
        req = _adm_req()
        token = l1_mod.issue_token(req, "digest", self.rules, now=1000.0)
        ok = l1_mod.verify_token(token, self.rules, now=1100.0)
        self.assertTrue(ok["valid"])
        self.assertEqual(ok["task_id"], "T1")
        expired = l1_mod.verify_token(token, self.rules, now=1000.0 + self.rules.token_ttl_seconds + 1)
        self.assertFalse(expired["valid"])
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        self.assertFalse(l1_mod.verify_token(tampered, self.rules, now=1100.0)["valid"])


class 记忆机回归(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = (mm.EVENTS_DIR, mm.SNAPSHOTS_DIR, mm.PACKS_DIR, mm.DB_PATH)
        mm.EVENTS_DIR = os.path.join(self.tmp, "events")
        mm.SNAPSHOTS_DIR = os.path.join(self.tmp, "snapshots")
        mm.PACKS_DIR = os.path.join(self.tmp, "packs")
        mm.DB_PATH = os.path.join(self.tmp, "memory.db")
        mm.ensure_dirs()

    def tearDown(self):
        mm.EVENTS_DIR, mm.SNAPSHOTS_DIR, mm.PACKS_DIR, mm.DB_PATH = self._old

    def test_事件记录与回读(self):
        ev = mm.record("agent-a", "task_done", {"task": "T1"}, source="internal")
        self.assertTrue(ev.get("ts"))
        got = mm.read_events()
        self.assertTrue(any(e["entity"] == "agent-a" for e in got))
        with self.assertRaises(ValueError):
            mm.record("agent-a", "task_done", {}, source="bogus")

    def test_状态指纹确定性(self):
        s1 = {"a": 1, "b": [1, 2]}
        s2 = {"b": [1, 2], "a": 1}
        self.assertEqual(mm.state_fingerprint(s1), mm.state_fingerprint(s2))
        self.assertNotEqual(mm.state_fingerprint(s1), mm.state_fingerprint({"a": 2, "b": [1, 2]}))

    def test_快照与差异(self):
        mm.record("agent-a", "task_done", {"task": "T1"})
        snap1 = mm.take_snapshot()
        loaded = mm.load_snapshot(mm.snap_hour(snap1["taken_at"]))
        self.assertIsNotNone(loaded)
        mm.record("agent-a", "task_done", {"task": "T2"})
        snap2 = mm.take_snapshot()
        d = mm.diff_snapshots(snap1, snap2)
        self.assertTrue(d)


class 台账回归(unittest.TestCase):
    def test_链式追加与校验(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        led = core_mod.HashLedger(path)
        led.append("e1", {"k": 1})
        led.append("e2", {"k": 2})
        v = led.verify()
        self.assertTrue(v["chain_valid"])
        self.assertEqual(v["chain_length"], 2)

    def test_篡改现形(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        led = core_mod.HashLedger(path)
        led.append("e1", {"k": 1})
        led.append("e2", {"k": 2})
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data[0]["payload"]["k"] = 999
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        v = core_mod.HashLedger(path).verify()
        self.assertFalse(v["chain_valid"])
        self.assertEqual(v["broken_at"], 0)

    def test_持久化重载(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        core_mod.HashLedger(path).append("e1", {"k": 1})
        reloaded = core_mod.HashLedger(path)
        reloaded.append("e2", {"k": 2})
        self.assertTrue(reloaded.verify()["chain_valid"])
        self.assertEqual(reloaded.verify()["chain_length"], 2)


def _gov_task(**missing):
    task = {
        "name": "demo-long-task",
        "executor": lambda artifact, **kw: None,
        "criteria": [{"id": "c1", "desc": "demo", "check": lambda a, e: (True, None)}],
        "admission": {}, "adjudication": {}, "release": {},
        "baseline": {},
    }
    for k in missing:
        task.pop(k, None)
    return task


class 长任务台账回归(unittest.TestCase):
    def test_锁内取号不撞号(self):
        path = os.path.join(tempfile.mkdtemp(), "long_tasks.json")
        led = long_mod.JobLedger(path)
        t1 = led.add(lambda seq: {"task_id": "LT-%04d" % seq, "name": "n1"})
        t2 = led.add(lambda seq: {"task_id": "LT-%04d" % seq, "name": "n2"})
        self.assertEqual(t1, "LT-0001")
        self.assertEqual(t2, "LT-0002")
        self.assertEqual(len(led.get()), 2)

    def test_更新不在账任务报错(self):
        path = os.path.join(tempfile.mkdtemp(), "long_tasks.json")
        led = long_mod.JobLedger(path)
        with self.assertRaises(KeyError):
            led.update("LT-9999", lambda j: j.update(status="RUNNING"))

    def test_治理契约缺字段拒收(self):
        for field in ("admission", "adjudication", "release", "baseline"):
            with self.assertRaises(Exception):
                long_mod._validate_govern(_gov_task(**{field: None}), external=True)


if __name__ == "__main__":
    unittest.main()
