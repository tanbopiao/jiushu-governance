#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
玄圭内核锁档试点 · XuanGuiKernelPilot V1.0
《全域锁档写入自治内核》的落地化最小运行体（试点，ZONGYUAN-ROOT 锁档之外，入册另议）。

对原方案的三处修订（按体系原法则，推演后定稿）：
  ① 双层锁档：写入时的自动确权/归档/审计全自动——但 ZONGYUAN-ROOT 登记册的封印层
     保留人工关口（登记零容忍是原规则，不入"无人值守"）；
  ② 冷存储不自动外发：玉匮冷备在本地产出加密就绪包+清单，外推（Gitee/GitHub/飞书）
     须用户自持凭据操作——已泄露旧令牌一律不用是原规则；
  ③ 审计失败即拒写（fail-closed）：玄衡册写不进去，高价值写入一律拒绝——载史铁律
     不接受"审计失败不影响写入"。

四层能力（按原方案落地）：
  锁档钩子层：资产/配置/规则/演化结果四类写入全拦截，违规内容 fail-closed 拒止；
  自动确权层：SHA256 + Merkle 链（叶子增量、根可独立重算）+ DID-BR-000002 锚定；
  自动归档层：本地双副本（热/冷）+ 加密就绪包（AES-256 留可插拔接口，缺省用
              哈希封条明文+诚实标注，不假装加密）；
  自动审计层：玄衡册只读追加链式台账，异常写入触发告警记录。
依赖：Python >= 3.7 标准库，零第三方依赖。
用法：
  python3 玄圭试点.py selftest
  python3 玄圭试点.py demo
"""

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid

__version__ = "1.1-pilot"

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(_HERE, "state", "xuangui")
DID = "DID-BR-000002"
LOCK_LEVEL = "META-003"
LOCK_KINDS = ("asset", "config", "rule", "evolution")  # 四类写入全部强制锁档
FORBIDDEN_MARK = "REJECT_TEST"  # 演示用违禁标记：命中即拒（真实规则表另接）


def _sha(t):
    return hashlib.sha256(t.encode("utf-8") if isinstance(t, str) else t).hexdigest()


def _canon(o):
    return json.dumps(o, sort_keys=True, ensure_ascii=False)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class XuanHengLedger:
    """玄衡册：只读追加、链式哈希、可独立校验（审计失败由调用方 fail-closed）。"""

    def __init__(self, path):
        self.path = path
        self.entries = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.entries = json.load(f).get("entries", [])

    def append(self, event, data):
        prev = self.entries[-1]["hash"] if self.entries else "GENESIS"
        rec = {"ts": _now(), "event": event, "data": data, "prev": prev}
        rec["hash"] = _sha(_canon({k: rec[k] for k in ("ts", "event", "data", "prev")}))[:32]
        self.entries.append(rec)
        self._flush()
        return rec

    def _flush(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp-" + uuid.uuid4().hex[:8]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": self.entries}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def verify(self):
        prev = "GENESIS"
        for e in self.entries:
            if e["prev"] != prev:
                return {"valid": False, "broken_at": e.get("event")}
            if e["hash"] != _sha(_canon({k: e[k] for k in ("ts", "event", "data", "prev")}))[:32]:
                return {"valid": False, "broken_at": e.get("event")}
            prev = e["hash"]
        return {"valid": True, "count": len(self.entries)}


class MerkleChain:
    """确权链：叶子=逐次写入哈希，根可独立重算验证。"""

    def __init__(self, path):
        self.path = path
        self.leaves = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.leaves = json.load(f).get("leaves", [])

    def append(self, content_sha):
        self.leaves.append(content_sha)
        self._flush()
        return self.root()

    def root(self):
        if not self.leaves:
            return "MERKLE-EMPTY"
        level = self.leaves[:]
        while len(level) > 1:
            if len(level) % 2:
                level.append(level[-1])
            level = [_sha(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
        return level[0]

    def _flush(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp-" + uuid.uuid4().hex[:8]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"leaves": self.leaves}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


class HashSealCipher:
    """缺省封条加密器：明文+SHA256 封条，诚实标注非 AES-256。
    AES-256 需受审第三方依赖，接口留在此处，另议后可插拔替换。"""

    name = "HASH-SEAL-PLAINTEXT(非AES-256,诚实标注)"

    def seal(self, content):
        blob = content.encode("utf-8")
        return blob, {"cipher": self.name, "seal": _sha(blob)}


class XuanGuiKernel:
    def __init__(self, root_dir=None, cipher=None):
        self.root = root_dir or os.path.join(STATE, "vault")
        self.hot = os.path.join(self.root, "hot")       # 热副本
        self.cold = os.path.join(self.root, "cold")     # 冷副本（本地玉匮）
        self.export_dir = os.path.join(self.root, "export_ready")  # 待外推就绪包
        for d in (self.hot, self.cold, self.export_dir):
            os.makedirs(d, exist_ok=True)
        self.ledger = XuanHengLedger(os.path.join(STATE, "xuanheng_ledger.json"))
        self.merkle = MerkleChain(os.path.join(STATE, "merkle_chain.json"))
        self.cipher = cipher or HashSealCipher()
        # v1.1：跨进程文件锁（共享正本 ledger/merkle 的写序化）；
        # 锁文件挂在 STATE 层，所有指向同一正本的内核实例共用一把锁。
        self.lock_path = os.path.join(STATE, "xuangui.lock")

    @contextlib.contextmanager
    def _exclusive(self):
        """共享正本跨进程互斥：写与校验全程持锁，杜绝并发覆盖。"""
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        lf = open(self.lock_path, "a+")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
            # 持锁后从盘上重载正本，保证多实例看到同一最新状态
            # （测试注入的替换件不重载，保留注入语义）
            if type(self.ledger) is XuanHengLedger:
                self.ledger = XuanHengLedger(self.ledger.path)
            if type(self.merkle) is MerkleChain:
                self.merkle = MerkleChain(self.merkle.path)
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()

    def _find_lock_by_hash(self, content_sha):
        """幂等查找：同内容哈希已锁档则复用原确权，不重写。"""
        for e in self.ledger.entries:
            if e["event"] == "asset_lock" and e["data"].get("sha256_hash") == content_sha:
                att = dict(e["data"])
                receipt = os.path.join(self.export_dir, att["asset_id"] + ".receipt.json")
                exp = (json.load(open(receipt, encoding="utf-8"))
                       if os.path.exists(receipt) else None)
                return att, exp
        return None, None

    def _rollback(self, asset_id, merkle_rolled):
        """半途失败回滚：删除已落盘副本与就绪包，弹回已追加的 Merkle 叶子，链不断。"""
        for p in (os.path.join(self.hot, asset_id + ".blob"),
                  os.path.join(self.cold, asset_id + ".blob"),
                  os.path.join(self.export_dir, asset_id + ".receipt.json")):
            if os.path.exists(p):
                os.remove(p)
        if merkle_rolled and self.merkle.leaves:
            self.merkle.leaves.pop()
            self.merkle._flush()
        self.ledger.append("write_rollback", {"asset_id": asset_id,
                                               "reason": "半途失败回滚，链未断"})

    # ---------- 锁档钩子层 ----------
    def write(self, kind, name, content):
        with self._exclusive():  # v1.1：全程持锁，跨进程写序化
            if kind not in LOCK_KINDS:
                # 未登记类型：fail-closed 拒止（锁档规则第 1~4 条：不得例外）
                self.ledger.append("hook_reject", {"kind": kind, "name": name,
                                                   "reason": "未登记写入类型"})
                raise PermissionError("写入类型 %r 未登记，fail-closed 拒止" % kind)
            if FORBIDDEN_MARK in str(content):
                self.ledger.append("hook_reject", {"kind": kind, "name": name,
                                                   "reason": "内容命中违禁规则"})
                raise PermissionError("内容命中锁档规则，写入被拦截")

            content_sha = _sha(str(content))
            # v1.1 幂等：同内容已锁档 → 复用原确权，不重写（断点重跑不再产生双锁）
            prev_att, prev_exp = self._find_lock_by_hash(content_sha)
            if prev_att is not None:
                return prev_att, prev_exp

            asset_id = "ASSET-%04d" % (len(self.merkle.leaves) + 1)  # v1.1：盘上重算，不缓存
            ts = int(time.time())

            # ---------- 自动归档层先行（双副本 + 校验），失败即止、无残留 ----------
            blob, seal_info = self.cipher.seal(str(content))
            hot_path = os.path.join(self.hot, asset_id + ".blob")
            cold_path = os.path.join(self.cold, asset_id + ".blob")
            for p in (hot_path, cold_path):
                with open(p, "wb") as f:
                    f.write(blob)
                if _sha(open(p, "rb").read()) != _sha(blob):
                    self._rollback(asset_id, merkle_rolled=False)
                    raise IOError("归档副本校验失败，写入拦截（fail-closed）")

            # ---------- 自动确权层（Merkle 追加；后续失败弹叶回滚，不留孤儿叶） ----------
            merkle_root = self.merkle.append(content_sha)
            attestation = {
                "event": "ASSET_LOCK", "asset_id": asset_id,
                "asset_type": kind.upper(), "sha256_hash": content_sha,
                "merkle_root": merkle_root, "did": DID,
                "timestamp": ts, "lock_level": LOCK_LEVEL,
            }
            export_manifest = {
                "event": "ASSET_ARCHIVE", "asset_id": asset_id,
                "asset_type": kind.upper(), "cipher": seal_info["cipher"],
                "seal": seal_info["seal"],
                "cold_storage_path": "PENDING_USER_PUSH(待用户自持凭据外推)",
                "local_storage_path": cold_path,
                "timestamp": ts, "lock_level": LOCK_LEVEL,
            }
            with open(os.path.join(self.export_dir, asset_id + ".receipt.json"), "w",
                      encoding="utf-8") as f:
                json.dump(export_manifest, f, ensure_ascii=False, indent=1)

            # ---------- 自动审计层（玄衡册，fail-closed；失败全量回滚） ----------
            audit = {
                "event": "ASSET_AUDIT", "asset_id": asset_id, "operator": "SYSTEM",
                "operation": "WRITE", "operation_time": ts,
                "operation_result": "SUCCESS", "error_message": "",
                "timestamp": ts, "lock_level": LOCK_LEVEL,
            }
            try:
                self.ledger.append("asset_audit", audit)  # 失败会抛异常 → 走回滚
                self.ledger.append("asset_lock", attestation)
            except Exception:
                self._rollback(asset_id, merkle_rolled=True)
                raise
            return attestation, export_manifest

    # ---------- 校验 ----------
    def verify(self):
        with self._exclusive():  # v1.1：校验全程持锁，读到的即盘上真值
            m_root = self.merkle.root()
            hot = {f: _sha(open(os.path.join(self.hot, f), "rb").read())
                   for f in os.listdir(self.hot)} if os.path.isdir(self.hot) else {}
            cold = {f: _sha(open(os.path.join(self.cold, f), "rb").read())
                    for f in os.listdir(self.cold)} if os.path.isdir(self.cold) else {}
            dual_ok = hot == cold
            # v1.1 逐资产核验：每笔 ASSET_LOCK 的哈希与盘上副本重算比对，
            # 覆盖写/丢件/篡改一律现形（治 v1.0 对覆盖写盲的缺陷）。
            locks = [e["data"] for e in self.ledger.entries if e["event"] == "asset_lock"]
            bad = []
            for a in locks:
                hp = os.path.join(self.hot, a["asset_id"] + ".blob")
                if not os.path.exists(hp) or _sha(open(hp, "rb").read()) != a["sha256_hash"]:
                    bad.append(a["asset_id"])
            ledger_v = self.ledger.verify()
            return {"merkle_root": m_root, "ledger": ledger_v,
                    "dual_copy_consistent": dual_ok,
                    "assets_checked": len(locks),
                    "assets_missing_or_tampered": bad,
                    "valid": bool(ledger_v.get("valid") and dual_ok and not bad)}


def _smoke():
    results = []

    def check(name, ok):
        results.append((name, ok))
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))

    k = XuanGuiKernel()

    # 1~4. 四类写入全部自动锁档
    kinds_ok = True
    for i, kind in enumerate(LOCK_KINDS):
        try:
            att, _ = k.write(kind, "自测件%d" % i, "自测内容-%s" % kind)
            kinds_ok = kinds_ok and att["event"] == "ASSET_LOCK"
        except Exception:
            kinds_ok = False
    check("四类写入自动锁档（资产/配置/规则/演化）", kinds_ok)

    # 5. 未登记类型与违禁内容 fail-closed
    refused = 0
    for kind, content in (("unknown_kind", "x"), ("rule", "REJECT_TEST 演示违禁")):
        try:
            k.write(kind, "越权件", content)
        except (PermissionError, IOError):
            refused += 1
    check("未登记类型/违禁内容 fail-closed 拒止", refused == 2)

    # 6. Merkle 根可独立重算
    check("Merkle 根独立重算一致", k.merkle.root() == k.verify()["merkle_root"])

    # 7. 玄衡册链式校验
    check("玄衡册链校验有效", k.ledger.verify()["valid"])

    # 8. 双副本一致
    check("热/冷双副本一致", k.verify()["dual_copy_consistent"])

    # 9. 审计 fail-closed：玄衡册写不进 → 写入整体拒绝
    k2 = XuanGuiKernel(root_dir=os.path.join(STATE, "vault2"))
    class _BrokenLedger(XuanHengLedger):
        def append(self, *a, **kw):
            raise IOError("玄衡册写失败（模拟）")
    k2.ledger = _BrokenLedger(k2.ledger.path)
    audit_blocked = False
    try:
        k2.write("rule", "试写件", "内容X")
    except IOError:
        audit_blocked = True
    check("审计失败即拒写（载史铁律，非方案原文的放行）", audit_blocked)

    # 10. 冷备外推就绪包：路径为待用户外推，不自动外发
    att, exp = k.write("asset", "外推件", "外推内容Y")
    check("冷备就绪包待外推（不自动外发）", exp["cold_storage_path"].startswith("PENDING_USER_PUSH"))

    # ---- v1.1 回归：双实例并发写不再撞号覆盖 ----
    kA = XuanGuiKernel(root_dir=k.root)
    a1, _ = kA.write("asset", "并发件A", "并发内容A")
    a2, _ = k.write("asset", "并发件B", "并发内容B")
    check("双实例写不撞号（v1.1 跨进程锁）", a1["asset_id"] != a2["asset_id"])
    v = k.verify()
    check("逐资产核验现形覆盖/丢件（v1.1）",
          v["valid"] and v["assets_checked"] >= 2 and not v["assets_missing_or_tampered"])

    # ---- v1.1 回归：同内容重复写幂等（断点重跑不产生双锁） ----
    p1, _ = k.write("asset", "同一产物", "相同内容")
    p2, _ = k.write("asset", "同一产物", "相同内容")
    check("同内容重复写幂等复用（v1.1）", p1["asset_id"] == p2["asset_id"])

    # ---- v1.1 回归：审计失败全量回滚，不留孤儿叶与残留副本 ----
    k3 = XuanGuiKernel(root_dir=os.path.join(STATE, "vault3"))
    class _BrokenLedger(XuanHengLedger):
        def append(self, *a, **kw):
            raise IOError("玄衡册写失败（模拟）")
    k3.ledger = _BrokenLedger(k3.ledger.path)
    leaves_before = len(k3.merkle.leaves)
    try:
        k3.write("rule", "回滚件", "回滚内容Z")
        rolled = False
    except IOError:
        rolled = True
    check("审计失败全量回滚无孤儿叶（v1.1）",
          rolled and len(k3.merkle.leaves) == leaves_before
          and not os.path.exists(os.path.join(k3.hot, "ASSET-0001.blob")))

    n_pass = sum(1 for _, ok in results if ok)
    print("结论：%d/%d 过" % (n_pass, len(results)))
    return n_pass == len(results)


def _demo():
    k = XuanGuiKernel()
    att, exp = k.write("rule", "演示规则件", "元宪法第一条：所有资产必须锁档")
    print(_canon(att))
    print(_canon(exp))
    print("玄衡册：", k.ledger.verify())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        print("=== 玄圭内核锁档试点自测 V%s ===" % __version__)
        sys.exit(0 if _smoke() else 1)
    elif cmd == "demo":
        _demo()
    else:
        print("用法：python3 玄圭试点.py [selftest|demo]")
