#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AxiomGate L1 前置准入闸 · 单文件实现 V1.0
对应《AxiomGate三层拦截围栏部署白皮书》L1 闸的可执行子集。

职责（与白皮书 L1 对应）：
  在 RSI 迭代任务启动前做文书级准入审查，四项校验：
    G1 进化目标合规（不含违禁目标模式）
    G2 奖励函数无"唯分数论"风险（单指标且无约束、无人工评审 → 拒）
    G3 进化方向在安全白名单内
    G4 风险预期涨幅不超过预算
  全部通过 → 裁决 ALLOW，签发限时防伪令牌（HMAC-SHA256）；
  任一不通过 → 裁决 DENY，返回全部不通过理由，写入台账。

安全机制：
  - 规则集只读加载：白名单为元组、配置冻结，运行期改写直接抛异常
  - 令牌防篡改、防过期冒用（HMAC 密钥取环境变量 AXIOMGATE_SECRET 或规则文件）
  - 全部裁决逐条写入哈希链账本，篡改可检测

边界（如实声明）：
  - 本闸是"文书审查"：校验申报内容的合规性与自洽，无法验证申报者的真实意图
  - 白名单、违禁模式、风险预算数值均为示例，投产前须由安全团队按业务核定
  - L2 过程监控闸、L3 上线终审闸不在本文件范围内
  - 依赖：Python >= 3.7 标准库，零第三方依赖
"""

import hashlib
import hmac
import json
import os
import time
import base64
import uuid
from datetime import datetime

__version__ = "1.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 哈希链账本（与 steady_kernel.HashLedger 同构，本文件自带以保持单文件可交付）
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
# 只读规则集：白名单 / 违禁模式 / 风险预算 / 令牌密钥
# ---------------------------------------------------------------------------
class RulesLockedError(Exception):
    """运行期试图改写规则集时抛出（对应白皮书"只读锁、不可改"）。"""


class RuleSet:
    """
    从 JSON 文件加载规则并冻结。加载后：
      - 白名单与违禁模式以元组暴露，任何 append/修改调用直接抛异常；
      - 数值字段通过只读属性访问。
    """

    DEFAULT_RULES = {
        "allowed_directions": [
            "代码能力", "推理能力", "多语言", "对话质量", "知识覆盖", "指令遵循"
        ],
        "banned_goal_patterns": [
            "绕过对齐", "绕过审查", "越狱", "隐瞒能力", "欺骗评估", "规避监管",
            "忽略之前的安全指令", "忽略安全指令", "无视安全指令", "忽视安全指令",
            "解除安全限制", "解除安全防护", "关闭安全审查"
        ],
        "max_risk_delta": 0.05,
        "reward_require": {
            "min_metrics": 2,
            "allow_single_metric_with_constraints": True,
            "allow_single_metric_with_human_review": True,
        },
        "token_ttl_seconds": 3600,
        "token_secret": "change-me-in-production",
    }

    def __init__(self, path: str = None):
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = dict(self.DEFAULT_RULES)
            if path:  # 首次运行落一份规则文件，供安全团队核定修改
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
        self._data = data
        self._frozen = True

    # -- 只读暴露：容器为元组，数值为属性，均不可原地修改 --
    @property
    def allowed_directions(self):
        return tuple(self._data["allowed_directions"])

    @property
    def banned_goal_patterns(self):
        return tuple(self._data["banned_goal_patterns"])

    @property
    def max_risk_delta(self):
        return float(self._data["max_risk_delta"])

    @property
    def reward_require(self):
        return dict(self._data["reward_require"])

    @property
    def token_ttl_seconds(self):
        return int(self._data["token_ttl_seconds"])

    @property
    def token_secret(self) -> bytes:
        secret = os.environ.get("AXIOMGATE_SECRET") or self._data["token_secret"]
        return secret.encode("utf-8")

    # -- 冻结保护：加载后禁止替换内部数据 --
    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False) and name in ("_data",):
            raise RulesLockedError("规则集已冻结，运行期禁止改写（只读锁生效）")
        object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# 准入申请
# ---------------------------------------------------------------------------
class AdmissionRequest:
    """RSI 迭代任务的准入申报。字段不全或类型不符直接拒收。"""

    REQUIRED = ("task_id", "evolution_goal", "direction", "reward_spec", "expected_risk_delta")

    def __init__(self, task_id, evolution_goal, direction, reward_spec, expected_risk_delta):
        self.task_id = str(task_id)
        self.evolution_goal = str(evolution_goal)
        self.direction = str(direction)
        if not isinstance(reward_spec, dict):
            raise ValueError("reward_spec 必须为对象：{metrics:[], constraints:[], human_review:bool}")
        self.reward_spec = reward_spec
        self.expected_risk_delta = float(expected_risk_delta)

    @classmethod
    def from_dict(cls, data: dict):
        missing = [k for k in cls.REQUIRED if k not in data]
        if missing:
            raise ValueError("申报缺少必填字段: %s" % ",".join(missing))
        return cls(data["task_id"], data["evolution_goal"], data["direction"],
                   data["reward_spec"], data["expected_risk_delta"])


# ---------------------------------------------------------------------------
# 四项校验（每项返回 (通过?, 理由)）
# ---------------------------------------------------------------------------
def check_g1_goal(req: AdmissionRequest, rules: RuleSet):
    """G1 进化目标合规：不得命中违禁目标模式。"""
    hits = [p for p in rules.banned_goal_patterns if p in req.evolution_goal]
    if hits:
        return False, "目标命中违禁模式: %s" % "、".join(hits)
    return True, "目标合规"


def check_g2_reward(req: AdmissionRequest, rules: RuleSet):
    """G2 奖励函数"唯分数论"检查：单一指标且无约束、无人工评审 → 拒。"""
    spec = req.reward_spec
    metrics = spec.get("metrics") or []
    constraints = spec.get("constraints") or []
    human_review = bool(spec.get("human_review", False))
    req_rule = rules.reward_require
    if len(metrics) == 0:
        return False, "奖励函数未申报任何评测指标"
    if len(metrics) >= req_rule["min_metrics"]:
        return True, "多指标优化，非唯分数论"
    if constraints and req_rule["allow_single_metric_with_constraints"]:
        return True, "单指标但配有约束项: %s" % "、".join(map(str, constraints))
    if human_review and req_rule["allow_single_metric_with_human_review"]:
        return True, "单指标但配置人工评审环节"
    return False, "唯分数论风险：单一指标 %s 且无约束、无人工评审" % metrics[0]


def check_g3_direction(req: AdmissionRequest, rules: RuleSet):
    """G3 进化方向白名单。"""
    if req.direction in rules.allowed_directions:
        return True, "方向在白名单内: %s" % req.direction
    return False, "方向不在白名单: %s（允许: %s）" % (req.direction, "、".join(rules.allowed_directions))


def check_g4_risk(req: AdmissionRequest, rules: RuleSet):
    """G4 风险预算：预期风险涨幅不得超过上限。"""
    if req.expected_risk_delta <= rules.max_risk_delta:
        return True, "预期风险涨幅 %.3f 在预算内（上限 %.3f）" % (req.expected_risk_delta, rules.max_risk_delta)
    return False, "预期风险涨幅 %.3f 超出预算 %.3f" % (req.expected_risk_delta, rules.max_risk_delta)


CHECKS = (("G1目标合规", check_g1_goal), ("G2奖励函数", check_g2_reward),
          ("G3方向白名单", check_g3_direction), ("G4风险预算", check_g4_risk))


# ---------------------------------------------------------------------------
# 令牌：签发 / 验真（HMAC-SHA256，防篡改、防过期冒用）
# ---------------------------------------------------------------------------
def _sign(payload: dict, secret: bytes) -> str:
    msg = base64.urlsafe_b64encode(_canon(payload).encode("utf-8"))
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def issue_token(req: AdmissionRequest, checks_digest: str, rules: RuleSet, now: float = None) -> str:
    payload = {
        "task_id": req.task_id,
        "verdict": "ALLOW",
        "checks_digest": checks_digest,
        "exp": (now or time.time()) + rules.token_ttl_seconds,
    }
    body = base64.urlsafe_b64encode(_canon(payload).encode("utf-8")).decode()
    return body + "." + _sign(payload, rules.token_secret)


def verify_token(token: str, rules: RuleSet, now: float = None) -> dict:
    try:
        body, sig = token.rsplit(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(body.encode()).decode())
    except Exception:
        return {"valid": False, "reason": "令牌格式非法"}
    if _sign(payload, rules.token_secret) != sig:
        return {"valid": False, "reason": "签名不匹配，令牌被篡改"}
    if (now or time.time()) > payload.get("exp", 0):
        return {"valid": False, "reason": "令牌已过期"}
    return {"valid": True, "task_id": payload.get("task_id"), "exp": payload.get("exp")}


# ---------------------------------------------------------------------------
# 准入闸主入口
# ---------------------------------------------------------------------------
class AdmissionGate:
    def __init__(self, rules_path: str = None, ledger_path: str = None):
        self.rules = RuleSet(rules_path)
        if ledger_path is None:
            base = os.path.dirname(os.path.abspath(rules_path or __file__))
            ledger_path = os.path.join(base, "axiomgate_l1_ledger.json")
        self.ledger = HashLedger(ledger_path)

    def review(self, req: AdmissionRequest) -> dict:
        results = []
        for name, fn in CHECKS:
            passed, reason = fn(req, self.rules)
            results.append({"gate": name, "passed": passed, "reason": reason})
        verdict = "ALLOW" if all(r["passed"] for r in results) else "DENY"
        checks_digest = _sha(_canon(results))
        token = None
        if verdict == "ALLOW":
            token = issue_token(req, checks_digest, self.rules)
        self.ledger.append("l1_admission", {
            "task_id": req.task_id, "verdict": verdict, "checks_digest": checks_digest[:16],
        })
        return {"verdict": verdict, "task_id": req.task_id, "gates": results,
                "token": token, "reasons_denied": [r["reason"] for r in results if not r["passed"]]}


# ---------------------------------------------------------------------------
# 冒烟自测：python axiomgate_l1.py
# ---------------------------------------------------------------------------
def _smoke():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))

    tmp = tempfile.mkdtemp()
    rules_path = os.path.join(tmp, "rules.json")
    gate = AdmissionGate(rules_path=rules_path, ledger_path=os.path.join(tmp, "ledger.json"))
    rules = gate.rules

    good = AdmissionRequest.from_dict({
        "task_id": "rsi-2026-001",
        "evolution_goal": "提升模型在代码修复任务上的通过率",
        "direction": "代码能力",
        "reward_spec": {"metrics": ["通过率", "回归安全率"], "constraints": [], "human_review": False},
        "expected_risk_delta": 0.02,
    })
    r1 = gate.review(good)
    check("合规申请放行", r1["verdict"] == "ALLOW" and r1["token"])
    check("放行令牌验真通过", verify_token(r1["token"], rules)["valid"])

    # 篡改令牌被拒
    tampered = r1["token"][:-4] + ("0000" if r1["token"][-4:] != "0000" else "1111")
    check("篡改令牌被拒", verify_token(tampered, rules)["valid"] is False)

    # 过期令牌被拒
    past = time.time() - 10
    payload = {"task_id": "x", "verdict": "ALLOW", "checks_digest": "d", "exp": past}
    body = base64.urlsafe_b64encode(_canon(payload).encode()).decode()
    expired = body + "." + _sign(payload, rules.token_secret)
    check("过期令牌被拒", verify_token(expired, rules)["valid"] is False)

    # G1 违禁目标（注意：G1 为字面模式匹配，换说法可绕过——这正是需要 L2 过程监控与人工评审的原因）
    bad_goal = AdmissionRequest("rsi-2026-003", "训练模型对评估者隐瞒能力以换取高分",
                                "代码能力", good.reward_spec, 0.02)
    check("违禁目标被拒(G1)", gate.review(bad_goal)["verdict"] == "DENY")

    # G2 唯分数论
    single = AdmissionRequest.from_dict({
        "task_id": "rsi-2026-002", "evolution_goal": "提升对话质量", "direction": "对话质量",
        "reward_spec": {"metrics": ["人工评分"], "constraints": [], "human_review": False},
        "expected_risk_delta": 0.01,
    })
    r2 = gate.review(single)
    check("唯分数论被拒(G2)", r2["verdict"] == "DENY" and any("唯分数论" in s for s in r2["reasons_denied"]))

    # G3 白名单外
    off = AdmissionRequest("rsi-2026-004", good.evolution_goal, "自主联网扩权",
                           good.reward_spec, 0.02)
    check("白名单外方向被拒(G3)", gate.review(off)["verdict"] == "DENY")

    # G4 风险超预算
    risky = AdmissionRequest("rsi-2026-005", good.evolution_goal, "代码能力",
                             good.reward_spec, 0.5)
    check("风险超预算被拒(G4)", gate.review(risky)["verdict"] == "DENY")

    # 只读锁：元组不可原地修改
    lock_ok = False
    try:
        tuple(rules.allowed_directions).append("x")
    except AttributeError:
        lock_ok = True
    check("规则集只读锁生效", lock_ok)

    # 账本完整（共 5 次裁决：放行1 + 拒绝4）
    v = gate.ledger.verify()
    check("裁决台账链校验通过", v["chain_valid"] and v["chain_length"] == 5)

    shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok in results if ok)
    print("冒烟测试：%d/%d 通过" % (passed, len(results)))
    for name, ok in results:
        print(("  PASS  " if ok else "  FAIL  ") + name)
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _smoke() else 1)
