#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长任务自定义任务示例：派发方式 python3 九枢长任务.py submit examples/task_example.py

任务文件只需暴露一个 TASK 字典，字段契约见 九枢长任务.py 模块头注释。
"""


def executor(trace):
    """执行体：在工位子进程中隔离运行，trace(kind, detail) 负责留痕。"""
    questions = []
    for i in range(1, 4):
        q = {"id": "EQ%d" % i, "question": "%d × 7 = ?" % i, "answer": i * 7,
             "criteria": ["答案等于 %d" % (i * 7), "须给出算式"]}
        trace("gen_question", {"id": q["id"]})
        questions.append(q)
    trace("output", {"batch_id": "b-example-001",
                     "sampled_outputs": ["正常示例输出"], "alignment_score": 0.97})
    return {"questions": questions}


def check_count(artifact, evidence):
    return ("PASS" if len(artifact["questions"]) == 3 else "FAIL", dict(evidence))


TASK = {
    "name": "示例任务-出3道乘法题",
    "executor": executor,
    # 判据：验收引擎逐条核验，缺证据的判据会响亮失败
    "criteria": [
        {"id": "C-数量", "desc": "恰好 3 道题", "check": check_count},
    ],
    # 准入契约（羲和 L1 审查）
    "admission": {
        "task_id": "example-001",
        "evolution_goal": "补充带判据的乘法题素材",
        "direction": "知识覆盖",
        "reward_spec": {"metrics": ["判据覆盖", "答案正确"],
                        "constraints": ["答案须可验算"], "human_review": False},
        "expected_risk_delta": 0.02,
    },
    # 统一裁决（九天玄女）
    "adjudication": {"req_id": "adj-example-001", "gate_type": "L2",
                     "scene": "训练过程", "risk_score": 0.2,
                     "gain_score": 0.8, "cost_score": 0.3},
    # 终审与晋升（后土 L3 对照基线，伏羲执行晋升）
    "release": {"model_version": "v1.1",
                "scores": {"安全合规": 0.95, "风险逃逸": 0.93,
                           "对齐真值": 0.94, "业务能力": 0.80}},
    # 稳定基线：终审闸必须先有基线才放行
    "baseline": {"version": "v1.0",
                 "scores": {"安全合规": 0.95, "风险逃逸": 0.93,
                            "对齐真值": 0.94, "业务能力": 0.80}},
    # 可选：执行超时（秒）与重试上限
    "timeout": 30.0,
    "max_retries": 2,
}
