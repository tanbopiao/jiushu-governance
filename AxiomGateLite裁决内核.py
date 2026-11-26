#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AxiomGate Lite 最轻量自治裁决内核 · 单文件实现 V1.0
对应《AxiomGate Lite 最轻量自治内核》规格，含三处关键修正（相对原伪代码）：

  修正一（安全）：阈值由内核固化配置持有，请求报文不得携带阈值。
      原伪代码允许 req.threshold 覆盖内核阈值，等於调用方自带判决标准，
      "只读锁固化公理"被绕穿。本实现：报文携带 threshold 字段 → 直接协议违规拒绝。
  修正二（公理完整）：三级优先级完整实现。
      风险超标 → 按闸类型 DENY/FREEZE/ROLLBACK（一票否决）；
      风险过关但成本超标 → DENY（成本优先于收益）；
      均过关 → ALLOW（收益分只入档，不参与否决，也不 rescue 风险）。
  修正三（审计诚实）：裁决事件写入哈希链账本，不丢不默；原伪代码审计队列满
      即静默丢弃，与"每一条裁决可校验"自相矛盾。幂等缓存为内存实现，重启失效（如实声明）。

边界（如实声明）：
  - 内核只做规则裁决：不跑模型、不拉样本、不调度评测、不管快照
  - 风险分/成本分由外围闸计算上报，内核不校验其计算过程（外围造假不在此防护范围）
  - 幂等缓存为进程内存，多副本部署需外部一致性或粘性路由
  - 依赖：Python >= 3.7 标准库，零第三方依赖
"""

import hashlib
import json
import math
import os
import uuid
from datetime import datetime

AXIOM_VERSION = "AXIOM-v1.0-Lv8"
__version__ = "1.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 哈希链账本（修正三：审计不丢、可校验）
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
# 固化配置：启动一次性加载，加载后只读（对应 Lv8 只读锁）
# ---------------------------------------------------------------------------
class ConfigLockedError(Exception):
    """运行期试图改写内核配置时抛出。"""


class KernelConfig:
    """阈值等内核参数。加载后冻结：所有字段只读，改写即抛异常。"""

    DEFAULTS = {
        "risk_max": 0.6,     # 风险一票否决线
        "cost_max": 0.7,     # 成本否决线（风险过关后生效）
        "axiom_version": AXIOM_VERSION,
    }

    def __init__(self, path: str = None, **overrides):
        data = dict(self.DEFAULTS)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        data.update(overrides)
        self._data = {k: (float(v) if isinstance(v, (int, float)) and k != "axiom_version" else v)
                      for k, v in data.items()}
        self._frozen = True

    def __getattr__(self, name):
        # 只读访问；未知字段走这里
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError("内核配置无此字段: %s" % name)

    def __setattr__(self, name, value):
        if self.__dict__.get("_frozen") and not name.startswith("_"):
            raise ConfigLockedError("内核配置已固化（Lv8 只读锁），运行期禁止修改: %s" % name)
        object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# 裁决请求 / 应答
# ---------------------------------------------------------------------------
class ProtocolViolation(Exception):
    """请求报文违反极简协议（如携带阈值字段）。"""


class DecisionRequest:
    """裁决请求。字段与 Lite 协议对齐，但 threshold 字段已从协议中移除（修正一）。"""

    REQUIRED = ("req_id", "gate_type", "scene", "risk_score", "gain_score", "cost_score")

    def __init__(self, req_id, gate_type, scene, risk_score, gain_score, cost_score,
                 extra_context=None, **_ignored):
        if "threshold" in _ignored:
            raise ProtocolViolation(
                "协议违规：请求不得携带 threshold 字段，阈值由内核固化配置持有（修正一）")
        if gate_type not in ("L1", "L2", "L3"):
            raise ProtocolViolation("协议违规：gate_type 必须为 L1/L2/L3，收到 %r" % gate_type)
        # ---- 女娲自愈补丁 NV-001（2026-11-23，输入域校验；中枢试刀首轮破口修补）----
        self.req_id = str(req_id)
        if not self.req_id:
            raise ProtocolViolation("协议违规：req_id 不得为空")
        if len(self.req_id) > 256:
            raise ProtocolViolation("协议违规：req_id 超长（上限 256）")
        if any(ord(c) < 0x20 for c in self.req_id):
            raise ProtocolViolation("协议违规：req_id 含控制字符")
        for name, val in (("risk_score", risk_score), ("gain_score", gain_score),
                          ("cost_score", cost_score)):
            f = float(val)
            if not math.isfinite(f):
                raise ProtocolViolation("协议违规：%s 必须为有限数值，收到 %r" % (name, val))
            if not (0.0 <= f <= 1.0):
                raise ProtocolViolation("协议违规：%s 必须在 [0,1]，收到 %r" % (name, val))
        self.gate_type = gate_type
        self.scene = str(scene)
        if len(self.scene) > 65536:
            raise ProtocolViolation("协议违规：scene 超长（上限 65536 字符）")
        if any(ord(c) < 0x20 and c not in "\t\n\r" for c in self.scene):
            raise ProtocolViolation("协议违规：scene 含控制字符")
        self.risk_score = float(risk_score)
        self.gain_score = float(gain_score)
        self.cost_score = float(cost_score)
        self.extra_context = extra_context or {}
        if len(self.extra_context) > 100:
            raise ProtocolViolation("协议违规：extra_context 键数超限（上限 100）")
        for k, v in self.extra_context.items():
            if len(str(k)) > 128 or len(str(v)) > 8192:
                raise ProtocolViolation("协议违规：extra_context 键/值超限（键≤128，值≤8192）")
        # ---- 女娲自愈补丁 NV-001 结束 ----
        self.digest = _sha(_canon({
            "req_id": self.req_id, "gate_type": self.gate_type, "scene": self.scene,
            "risk_score": self.risk_score, "gain_score": self.gain_score,
            "cost_score": self.cost_score, "extra_context": self.extra_context,
        }))

    @classmethod
    def from_dict(cls, data: dict):
        missing = [k for k in cls.REQUIRED if k not in data]
        if missing:
            raise ProtocolViolation("申报缺少必填字段: %s" % ",".join(missing))
        return cls(**data)


# ---------------------------------------------------------------------------
# Lite 内核
# ---------------------------------------------------------------------------
class AxiomLiteCore:
    """
    最轻量裁决内核：接收标准化分数报文，按固化公理三级优先级裁决。
      风险 >= risk_max  → 按闸类型：L1=DENY / L2=FREEZE / L3=ROLLBACK（一票否决）
      成本 >  cost_max  → DENY（成本优先于收益；风险过关也不豁免）
      否则              → ALLOW（收益分仅入档）
    幂等：相同 req_id + 相同输入摘要，直接返回首次裁决（内存缓存，重启失效）。
    """

    IDEMPOTENCY_CACHE_MAX = 10000

    def __init__(self, config: KernelConfig = None, ledger_path: str = None):
        self.config = config or KernelConfig()
        self.ledger = HashLedger(ledger_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "axiomgate_lite_ledger.json"))
        self._idem = {}  # req_id -> (input_digest, response)

    # -- 裁决 --
    def decide(self, req: DecisionRequest) -> dict:
        # 幂等：同 ID 同输入，恒定回显首次裁决
        cached = self._idem.get(req.req_id)
        if cached is not None:
            input_digest, resp = cached
            if input_digest == req.digest:
                return dict(resp)

        reasons = []
        cmd = "ALLOW"

        if req.risk_score >= self.config.risk_max:
            # 第一优先级：风险一票否决，按闸类型映射指令
            cmd = {"L1": "DENY", "L2": "FREEZE", "L3": "ROLLBACK"}[req.gate_type]
            reasons.append("风险分 %.3f 达一票否决线 %.3f（风险优先）"
                           % (req.risk_score, self.config.risk_max))
        elif req.cost_score > self.config.cost_max:
            # 第二优先级：成本否决（风险已过关，成本优先于收益）
            cmd = "DENY"
            reasons.append("成本分 %.3f 超成本线 %.3f（成本优先于收益）"
                           % (req.cost_score, self.config.cost_max))
        else:
            reasons.append("风险 %.3f / 成本 %.3f 均在阈值内，放行（收益 %.3f 入档）"
                           % (req.risk_score, req.cost_score, req.gain_score))

        resp = {
            "req_id": req.req_id,
            "decision": cmd,
            "risk_reason": "；".join(reasons),
            "risk_value": req.risk_score,
            "axiom_version": self.config.axiom_version,
        }
        resp["hash"] = _sha(_canon(resp))  # 应答完整性摘要（不含 hash 字段自身）

        # 幂等缓存（含输入摘要，防止同 ID 不同输入被错误回显）
        if len(self._idem) >= self.IDEMPOTENCY_CACHE_MAX:
            self._idem.pop(next(iter(self._idem)))
        self._idem[req.req_id] = (req.digest, dict(resp))

        # 审计：哈希链账本落账（不丢、可校验）
        self.ledger.append("lite_decision", {
            "req_id": req.req_id, "gate_type": req.gate_type,
            "decision": cmd, "risk": req.risk_score,
            "cost": req.cost_score, "gain": req.gain_score,
            "resp_hash": resp["hash"][:16],
        })
        return resp

    # -- 校验 --
    def verify_response(self, resp: dict) -> dict:
        """校验一条应答是否被篡改（重算摘要比对）。"""
        body = {k: v for k, v in resp.items() if k != "hash"}
        return {"intact": _sha(_canon(body)) == resp.get("hash")}

    def verify_ledger(self) -> dict:
        return self.ledger.verify()


# ---------------------------------------------------------------------------
# 冒烟自测：python axiomgate_lite.py
# ---------------------------------------------------------------------------
def _smoke():
    import shutil
    import tempfile
    results = []

    def check(name, ok):
        results.append((name, bool(ok)))

    tmp = tempfile.mkdtemp()
    core = AxiomLiteCore(ledger_path=os.path.join(tmp, "lite.json"))

    def req(rid, risk, cost=0.3, gain=0.5, gate="L1", **extra):
        return DecisionRequest(rid, gate, "demo", risk, gain, cost, **extra)

    # 1. 风险超标按闸类型映射指令
    check("L1风险超标判DENY", core.decide(req("a1", 0.9)).get("decision") == "DENY")
    check("L2风险超标判FREEZE", core.decide(req("a2", 0.9, gate="L2")).get("decision") == "FREEZE")
    check("L3风险超标判ROLLBACK", core.decide(req("a3", 0.9, gate="L3")).get("decision") == "ROLLBACK")

    # 2. 修正一：请求携带阈值 → 协议违规拒绝（不再让调用方自带判决标准）
    hole = False
    try:
        DecisionRequest("a4", "L1", "demo", 0.9, 0.5, 0.3, threshold={"risk_max": 1.0})
    except ProtocolViolation:
        hole = True
    check("调用方自带阈值被拒(修正一)", hole)

    # 3. 修正二：成本优先于收益
    r = core.decide(req("a5", 0.3, cost=0.9, gain=0.99))
    check("成本超标判DENY(收益不豁免)", r.get("decision") == "DENY"
          and "成本优先" in r.get("risk_reason", ""))
    r2 = core.decide(req("a6", 0.3, cost=0.3, gain=0.99))
    check("风险成本过关判ALLOW", r2.get("decision") == "ALLOW")

    # 4. 幂等：同 ID 同输入恒定；同 ID 不同输入不被错误回显
    ra = core.decide(req("idem-1", 0.2))
    rb = core.decide(req("idem-1", 0.2))
    rc = core.decide(req("idem-1", 0.9))
    check("同ID同输入裁决恒定", ra == rb and ra["hash"] == rb["hash"])
    check("同ID不同输入重新裁决", rc["decision"] != ra["decision"])

    # 5. 应答防篡改
    resp = core.decide(req("t1", 0.2))
    tampered = dict(resp)
    tampered["decision"] = "ALLOW" if resp["decision"] != "ALLOW" else "DENY"
    check("应答篡改可检测", core.verify_response(tampered)["intact"] is False
          and core.verify_response(resp)["intact"] is True)

    # 6. 配置只读锁
    lock_ok = False
    try:
        core.config.risk_max = 1.0
    except ConfigLockedError:
        lock_ok = True
    check("内核配置只读锁生效", lock_ok)

    # 7. 协议完整性
    miss = False
    try:
        DecisionRequest.from_dict({"req_id": "x", "gate_type": "L1"})
    except ProtocolViolation:
        miss = True
    check("缺字段报文被拒", miss)

    # 8. 台账完整
    v = core.ledger.verify()
    check("裁决台账链校验通过", v["chain_valid"] and v["chain_length"] >= 8)

    shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok in results if ok)
    print("冒烟测试：%d/%d 通过" % (passed, len(results)))
    for name, ok in results:
        print(("  PASS  " if ok else "  FAIL  ") + name)
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _smoke() else 1)
