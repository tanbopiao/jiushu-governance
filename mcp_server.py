#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jiushu-governance MCP 服务壳

把九枢治理运行时的长任务能力（异步派发/断点续跑/进度可查）与记忆机
（事件流正本/快照对账/回放）以 MCP 工具形式对外暴露，供任意 MCP 客户端
（Claude、Cursor 等）调用。运行时本体零依赖纯标准库；本壳仅依赖 mcp 包。

启动配置（stdio）：
    {
      "mcpServers": {
        "jiushu-governance": {
          "command": "python3",
          "args": ["-m", "mcp_server"],
          "env": {
            "JIUSHU_HOME": "/path/to/jiushu-governance"
          }
        }
      }
    }

工具一览：
    selftest          自测：跑九枢长任务全量自测并返回结果
    task_submit       派任务：异步派发内建生产件或任务文件，立取任务号
    task_status       进度可查：查全部或指定任务的进度（n/7）、当前步与耗时
    task_resume       断点续跑：从检查点续跑，已完成步骤不重跑
    memory_events     事件流读取：按时间窗读取记忆机事件（只读）
    memory_replay     状态回放：按时刻重放事件流得到状态快照（只读）
    memory_snapshot   快照对账：取最新快照并返回留存报告（只读）

安全边界：只暴露任务派发/查询/续跑与记忆机只读操作；
不暴露任何删除、覆写台账或绕过闸口的写操作。
"""
import json
import os
import sys

from mcp.server.fastmcp import FastMCP

JIUSHU_HOME = os.environ.get("JIUSHU_HOME") or os.path.dirname(os.path.abspath(__file__))
if JIUSHU_HOME not in sys.path:
    sys.path.insert(0, JIUSHU_HOME)
os.chdir(JIUSHU_HOME)

from 九枢长任务 import _selftest, status, submit, resume  # noqa: E402
sys.path.insert(0, os.path.join(JIUSHU_HOME, "memory-machine"))
import memory_machine as mm                              # noqa: E402

mcp = FastMCP("jiushu-governance")


@mcp.tool()
def selftest() -> str:
    """跑九枢长任务全量自测（9 项检查：派发/进度/验收/断点/并发等），
    返回逐项结果。沙箱等受限环境中 fork 并发检查可能 FAIL（环境限制，非代码缺陷）。"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        passed = _selftest()
    return json.dumps({"all_passed": passed, "output": buf.getvalue()},
                      ensure_ascii=False)


@mcp.tool()
def task_submit(source: str = "prod") -> str:
    """异步派发一件长任务，立取任务号，工位子进程后台跑全链（七步闸口）。
    source 取值：'prod'=内建生产件；或任务文件绝对路径（模块须定义 TASK）。"""
    task_id = submit(source)
    return json.dumps({"submitted": task_id}, ensure_ascii=False)


@mcp.tool()
def task_status(task_id: str = "") -> str:
    """查询长任务进度：全部任务或指定任务号的进度（n/7）、当前步、耗时、
    产物号与错误信息。工位失联会被如实标注为 INTERRUPTED，可续跑。"""
    jobs = status(task_id or None)
    return json.dumps({"jobs": jobs}, ensure_ascii=False)


@mcp.tool()
def task_resume(task_id: str, force: bool = False) -> str:
    """断点续跑：从检查点续跑指定任务，已完成步骤不重跑（留痕不重复）。
    force 仅在工位已确认失联时使用；活工位强制续跑会被拒绝。"""
    result = resume(task_id, force=force)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def memory_events(since_ts: float = 0.0, until_ts: float = 0.0) -> str:
    """读取记忆机事件流（只读）：since_ts/until_ts 为 Unix 秒（0 表示不设限），
    返回时间窗内的事件列表。事件是体系状态的唯一正本。"""
    events = mm.read_events(since_ts=since_ts,
                            until_ts=until_ts if until_ts > 0 else None)
    return json.dumps({"count": len(events), "events": events}, ensure_ascii=False)


@mcp.tool()
def memory_replay(at_ts: float = 0.0) -> str:
    """按时刻回放事件流得到状态快照（只读）：at_ts 为 Unix 秒（0 表示现在），
    返回重放后的状态与事件计数，可验证「留痕可复算」。"""
    result = mm.replay(at_ts=at_ts if at_ts > 0 else None)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def memory_snapshot() -> str:
    """取最新状态快照并返回留存报告（只读）：含当前状态指纹、
    快照留存统计与保留策略情况，用于对账与巡检。"""
    state = mm.current_state()
    report = mm.retention_report()
    return json.dumps({"fingerprint": mm.state_fingerprint(state),
                       "retention": report}, ensure_ascii=False)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
