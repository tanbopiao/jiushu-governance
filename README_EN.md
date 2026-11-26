# Nine Pivots · Governance Runtime for Autonomous Agent Evolution (九枢)

A governance runtime for autonomous AI agent evolution, built entirely on the **Python standard library** (no third-party dependencies, Python >= 3.7). It lets an agent run evolution drills that are **gated at every step, fully audited, and resumable after interruption**.

[中文说明](README.md)

## The Problem It Solves

Autonomous agent evolution (auto-generating tasks, executing them, promoting results) fails in two familiar ways: **nobody stops it when it goes off the rails**, and **nobody can pick it up when it crashes**. Nine Pivots turns both into first-class parts of the workflow:

- **Before running**: an admission gate reviews the task (Xihe, L1); promotion is impossible without a stable baseline (Houtu, L3 final review);
- **While running**: a sidecar monitor watches outputs (Zhenwu, L2) and freezes on policy violations; a self-heal step runs before resuming (Nüwa);
- **After running**: artifacts are committed to a Merkle-chained vault (Xuangui); criteria are verified, replay fingerprints are compared, and five ledgers are cross-checked (Taiyin) — only then does a result count;
- **After a crash**: the long-task mode checkpoints every step and resumes from the last completed one, never re-running finished steps.

## Components

| File | Role |
|---|---|
| `九枢载道.py` | Charter: pivot definitions, formation, history ledger |
| `九枢执闸.py` | Formation builder: binds all nine pivots to real gates |
| `AxiomGateL1准入闸.py` | L1 admission gate: reviews tasks at entry |
| `AxiomGateL2过程监控闸.py` | L2 monitor: freezes on violating output batches |
| `AxiomGateL3终审闸.py` | L3 final review: promotion against a stable baseline |
| `AxiomGateLite裁决内核.py` | Unified adjudication kernel (with Nüwa self-heal) |
| `玄圭试点.py` | Vault: commits artifacts to a Merkle-chain store |
| `memory-machine/` | Memory machine: append-only event stream + snapshots + replay |
| `九枢一期试点.py` | Evidence-chain acceptance engine (thin orchestrator) |
| `九枢二期编排.py` | Dispatch & executor isolation (forked subprocess) |
| `九枢长任务.py` | **Long-task mode: async dispatch / checkpoint resume / progress query** |

## Quick Start

```bash
# Long-task self-test (7/7 checks passing means ready)
python3 九枢长任务.py selftest

# Dispatch the built-in slow demo task (production run)
python3 九枢长任务.py submit prod

# Query progress
python3 九枢长任务.py status

# Resume after interruption
python3 九枢长任务.py resume <task_id> [--force]
```

Every component ships with its own self-test:

```bash
python3 九枢载道.py selftest
python3 九枢执闸.py smoke
python3 玄圭试点.py selftest
python3 九枢一期试点.py selftest
python3 九枢二期编排.py selftest
```

## Long-Task Mode

`九枢长任务.py` completes three things for long-running jobs:

1. **Async dispatch**: `submit()` writes the ledger entry and returns a task ID immediately; a worker subprocess (fork + setsid) runs the full pipeline independently and commits the acceptance report back to the ledger when done;
2. **Checkpoint resume**: the seven-step pipeline (admit → execute → lock → adjudicate → release → criteria → verify) checkpoints after every step; `resume()` continues from the last completed step without re-running finished ones (audit trail is append-only);
3. **Progress query**: `status()` reports progress (n/7), current step, elapsed time and artifact references at any moment; a lost worker is honestly marked INTERRUPTED once its heartbeat goes stale — never silently.

To define your own task, write a Python file exposing a `TASK` dict (see `examples/task_example.py`):

```bash
python3 九枢长任务.py submit examples/task_example.py
```

### Honest Boundaries

- Checkpoint granularity = step: the execute step cannot resume mid-way, only re-run as a whole (retries are never silent; everything is logged);
- Single-worker serial execution is recommended (the ledger layer does not implement multi-worker scheduling yet); since v0.1.1, the shared source of truth (Xuangui lock chain) and checkpoints are protected by cross-process file locks, locking is content-hash idempotent (a resumed run never double-locks); since v0.1.2, every resume path (forced or not) verifies the old worker is gone first (zombie state counts as exited), and a live worker is always refused;
- The `test_fail_after_step` field in a task definition is a self-test hook only (simulates a mid-run kill); it is rejected for production tasks;
- The Xuangui Merkle chain is a shared source of truth: self-test records are committed to the chain too — this is by design, not a defect.

## Changelog

- **v0.1.2 (2026-11-26)**: post-release red/blue self-attack (adversarial review) confirmed four defects, all fixed — (1) P0: the long-task ledger lock was held on the ledger file itself, which is replaced by `os.replace` on every save; under concurrency the lock split across inodes, critical sections overlapped, and combined with a shared `.tmp` path the ledger could corrupt (reproduced: two concatenated JSON documents); now uses a dedicated never-replaced lock file, in-lock ID allocation, and unique temp names; (2) P1: worker-liveness check was only enforced on forced resume — after a false INTERRUPTED mark, a plain resume spawned a second worker on the same checkpoint (reproduced: doubled execution traces); now enforced on every resume path, plus a child-side idempotent pid self-registration covering the parent-crash gap; (3) P1: task IDs were allocated outside the lock (count + 1), so concurrent submits could collide; allocation moved inside the lock; (4) P2: the Xuangui rollback window only covered the audit stage — a failure between the Merkle append and the receipt write left an orphan leaf ("leaf without lock") that `verify()` could not see (reproduced: 1 leaf, 0 locks, verdict still valid); the rollback window now spans copies → Merkle → receipt → audit, and `verify()` gained a leaf↔lock one-to-one check (`orphan_leaves` field). Also fixed: admission denial reason now read from `reasons_denied` (the old `reason` key was always empty, breaking traceability). Xuangui self-test 14/14, long-task self-test 9/9 (including new regressions: concurrent registration, live-worker resume refusal, orphan-leaf detection).
- **v0.1.1 (2026-11-26)**: review-driven fixes — (1) cross-process file lock on the Xuangui shared source of truth plus on-disk asset numbering, so concurrent writers no longer collide or overwrite; (2) `verify()` now re-hashes every locked asset, exposing overwrites, losses and tampering (v1.0 was blind to overwrites); (3) locking is content-hash idempotent — a resumed run never double-locks; (4) a mid-write failure rolls back fully (Merkle leaf popped, copies removed), leaving no orphan leaves; (5) checkpoints gained a cross-process write lock, and forced resume verifies the old worker is gone. Xuangui self-test 11/11, long-task self-test 7/7.

## Design Discipline

- **Retries are never silent**: failures and retries are always logged, append-only;
- **Contract conflicts fail loudly**: evidence violating the contract is rejected, never downgraded for compatibility;
- **No evidence, no closure**: criteria lacking evidence raise errors instead of being skipped;
- **Replay verification**: before a report is issued, the run fingerprint must match the replay fingerprint from the event stream.

## License

Apache License 2.0, see [LICENSE](LICENSE).
