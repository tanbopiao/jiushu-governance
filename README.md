<!-- mcp-name: io.github.tanbopiao/jiushu-governance -->

# 九枢 · 智能体进化治理运行时（Nine Pivots Governance Runtime）

一套纯 Python 标准库实现的**智能体自治进化治理运行时**：让智能体在"进化演习"中跑得起来，也停得下来——每一步都有闸口把关、有台账留痕、有检查点可续跑。

无第三方依赖，Python >= 3.7 即可运行。

## 这套东西解决什么问题

让智能体自主进化（自动出题、自动执行、自动晋升）最怕两件事：**跑飞了没人拦**，和**跑挂了没法接**。九枢把这两件事都变成流程的一部分：

- **跑之前**：准入闸先审任务（羲和 L1），没有稳定基线不许晋升（后土 L3 终审）；
- **跑之中**：过程监控盯着输出（真武 L2），违禁即冻结，冻结后先自愈再上阵（女娲）；
- **跑之后**：产物锁档进哈希链（玄圭），判据核验、回放指纹比对、五本台账验链（太阴），全过才算数；
- **中断了**：长任务模式逐步落检查点，从断点接着跑，已完成步骤不重跑。

## 组件一览

| 文件 | 角色 |
|---|---|
| `九枢载道.py` | 宪章：九枢定义、阵型与载史台账 |
| `九枢执闸.py` | 立阵：把九个枢逐一绑上真实闸口 |
| `AxiomGateL1准入闸.py` | L1 准入：任务进门先审 |
| `AxiomGateL2过程监控闸.py` | L2 监控：输出批违禁即冻结 |
| `AxiomGateL3终审闸.py` | L3 终审：对照稳定基线决定晋升 |
| `AxiomGateLite裁决内核.py` | 统一裁决内核（含女娲自愈） |
| `玄圭试点.py` | 锁档：产物写入 Merkle 链正本库 |
| `memory-machine/` | 记忆机：事件流正本 + 快照对账 + 回放 |
| `九枢一期试点.py` | 证据链验收引擎（薄编排器） |
| `九枢二期编排.py` | 派活与执行器隔离（fork 子进程） |
| `九枢长任务.py` | **长任务模式：异步派发 / 断点续跑 / 进度可查** |

## 快速开始

```bash
# 长任务自测（7 项检查全过即部署就绪）
python3 九枢长任务.py selftest

# 派一个内建慢工件（生产实跑）
python3 九枢长任务.py submit prod

# 查进度
python3 九枢长任务.py status

# 中断后续跑
python3 九枢长任务.py resume <task_id> [--force]
```

其他组件各自带自测：

```bash
python3 九枢载道.py selftest
python3 九枢执闸.py smoke
python3 玄圭试点.py selftest
python3 九枢一期试点.py selftest
python3 九枢二期编排.py selftest
```

## 长任务模式

`九枢长任务.py` 补齐长活三件事：

1. **异步派发**：`submit()` 立台账即返回任务号，工位子进程（fork + setsid）脱离调用方独立跑完，跑完把验收报告锁档回填台账；
2. **断点续跑**：七步流程（准入 → 执行 → 锁档 → 裁决 → 终审 → 判据 → 验真）每步落检查点，`resume()` 从上一步续跑，已完成步骤不重跑（留痕追加不覆盖）；
3. **进度可查**：`status()` 随时报进度（n/7）、当前步、耗时与产物号；工位失联心跳逾刻如实标注中断，不静默。

自定义任务写一个 Python 文件、暴露 `TASK` 字典即可（见 `examples/task_example.py`）：

```bash
python3 九枢长任务.py submit examples/task_example.py
```

### 诚实边界

- 断点粒度＝步：执行步不可半途续跑，只可整步重跑（重试不静默，全部留痕）；
- 同一时刻建议单工位串行（台账层未做多工位调度）；v0.1.1 起共享正本（玄圭锁档链）与检查点均已加跨进程文件锁，锁档按内容哈希幂等（断点重跑不产生双锁）；v0.1.2 起所有续跑路径（含非强制）均校验旧工位存活（僵尸态视为已退出），活工位一律拒续跑；
- 任务定义中的 `test_fail_after_step` 仅为自测钩子（模拟中途被杀），生产件禁用；
- 玄圭 Merkle 链为共享正本，自测产生的记录同样入链——这是设计口径，不是缺陷。

## 版本记录

- **v0.1.2（2026-11-26）**：发布后红蓝自攻（正反合）坐实四项缺陷并修复——①P0 长任务台账锁原打在被 `os.replace` 反复替换的台账文件上，并发时锁随 inode 分裂、临界区重叠，叠加共享 `.tmp` 路径会坏档（并发提交实证两段 JSON 拼接）；改为专用永不替换锁文件＋锁内取号＋唯一临时文件名；②P1 工位存活校验原只挂 `--force` 路径，误判中断后普通续跑会派双工位（实证执行留痕翻倍）；扩到所有续跑路径，另补父进程登记间隙的工位自补 pid；③P1 提交取号原在锁外（读条数＋1），并发提交撞号；取号入锁；④P2 玄圭回滚窗口原只覆盖审计段，确权链追加后、回执写完前的失败留「有叶无锁」孤儿叶且校验对其盲（实证 1 叶 0 锁仍报全绿）；回滚窗口扩至副本→确权→回执→审计全程，`verify()` 增加叶↔锁一一对应检查（`orphan_leaves` 字段）。附带修正：准入拒收原因改读 `reasons_denied`（原读 `reason` 恒空，溯源断链）。玄圭自测 14/14、长任务自测 9/9（含并发登记、活工位拒续跑、孤儿叶现形等新回归）。
- **v0.1.1（2026-11-26）**：评审驱动修复——①玄圭共享正本加跨进程文件锁＋资产号盘上重算，双进程并发写不再撞号覆盖；②`verify()` 增加逐资产哈希核验，覆盖写/丢件/篡改一律现形（v1.0 对覆盖写盲）；③锁档按内容哈希幂等，断点重跑不产生双锁；④锁档半途失败全量回滚（弹 Merkle 叶＋删副本），不留孤儿叶；⑤检查点加跨进程写锁，强制续跑前校验旧工位存活。玄圭自测 11/11、长任务自测 7/7。

## 设计纪律

- **重试不静默**：失败与重试全部留痕，追加不覆盖；
- **契约冲突响亮失败**：证据引用不符契约即拒，不降级兼容；
- **无证据不结案**：缺证据的判据报错而非跳过；
- **回放验证**：报告出库前，留痕指纹与事件流回放指纹必须一致。

## MCP 服务壳

`mcp_server.py` 把九枢的长任务能力与记忆机以 MCP 工具形式对外暴露，
供任意 MCP 客户端（Claude、Cursor 等）通过 stdio 调用。
运行时本体零依赖纯标准库；服务壳仅额外依赖 `mcp` 包（`pip install mcp`）。

| Tool | What it does |
|---|---|
| `selftest()` | Run the full self-test (9 checks) and return per-item results |
| `task_submit(source)` | Async-dispatch a long task ('prod' built-in or a task file path); returns the task id immediately |
| `task_status(task_id)` | Query progress (n/7), current step, elapsed time, asset refs and errors |
| `task_resume(task_id, force)` | Resume from the last checkpoint; completed steps are never re-run |
| `memory_events(since_ts, until_ts)` | Read the event stream (read-only) — the source of truth |
| `memory_replay(at_ts)` | Replay events up to a timestamp into a state snapshot (read-only) |
| `memory_snapshot()` | Latest state fingerprint + retention report (read-only) |

Client configuration (stdio):

```json
{
  "mcpServers": {
    "jiushu-governance": {
      "command": "python3",
      "args": ["-m", "mcp_server"],
      "env": {
        "JIUSHU_HOME": "/full/path/to/jiushu-governance"
      }
    }
  }
}
```

Safety boundary: only task dispatch/query/resume and read-only memory operations
are exposed. No ledger deletion, overwrite, or gate bypass is available through MCP.

## 道统映射表（命名即种子）

九枢——九根枢纽柱，撑起治理殿的顶。命名即架构：治理不是一道墙，而是多根柱子各承其力、缺一即倾：

| 道统 | 仓内落位 |
|---|---|
| 命名即种子：九枢＝九柱承顶，闸口分层各司其职 | L1 准入闸／L2 过程监控闸／L3 终审闸三柱立殿 |
| 裁决有链：每条裁决落哈希台账，不丢、可校验、篡改即现形 | `AxiomGateLite裁决内核.HashLedger` 链式台账＋`verify()` |
| 判决标准不归调用方：自带阈值即协议违规，内核只认冻结配置 | `DecisionRequest` 协议拒收＋`KernelConfig` 只读锁 |
| 无证据不结案：终审每条结论必须携带证据引用 | `九枢一期试点.adjudicate` 缺证据响亮失败 |
| 断点可续：长任务逐步落检查点，失联如实标注不静默 | `九枢长任务.Checkpoint`＋`reap_stale`＋`resume` |
| 令牌防冒用：HMAC 签发，过期与篡改皆验出 | `AxiomGateL1准入闸.issue_token/verify_token` |
| 回归铁律：改动后历史用例必须全绿 | `tests/test_jiushu_governance.py` 21 例五域回归＋`tests/regression.md` |

## License

Apache License 2.0，见 [LICENSE](LICENSE)。
