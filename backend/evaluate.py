"""
评估脚本（Evaluation Harness）：回答"这个 Agent 到底好不好"。

做法：加载 evaluation/testset.json 的测试用例，逐条真实调用 Agent 循环
（run_agent：真 LLM + 真工具），对每次运行做两个维度的打分：

1. 工具调用准确率：期望的工具集合是否全部被调用（考察"会不会查"）
2. 结论要点命中率：最终回答是否覆盖期望结论要点（正则匹配，考察"说得对不对"）

产出：
- 控制台汇总（总通过率 / 分类明细 / 平均耗时）
- evaluation_report.md（可放进面试材料）
- evaluation_results.json（逐条明细，供 bad case 分析）

用法（在 backend 目录）：
    python evaluate.py              # 跑全部
    python evaluate.py --limit 5    # 只跑前 5 条（调试用）
"""
import os
import re
import json
import time
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = HERE
EVAL_DIR = os.path.join(HERE, "evaluation")
TESTSET = os.path.join(EVAL_DIR, "testset.json")

CONCLUSION_THRESHOLD = 0.6  # 结论要点命中率及格线


def load_cases():
    with open(TESTSET, encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"], data["version"]


def run_case(case):
    """跑单条用例，返回 {工具命中, 结论命中, 耗时, 工具序列, 回答}。"""
    # 延迟导入：保证 --help 等参数解析不依赖 .env / openai
    import sys
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BACKEND, ".env"))  # 评估也走真实 API Key
    sys.path.insert(0, BACKEND)
    from agent import run_agent

    start = time.time()
    result = run_agent(case["input"])
    elapsed = round(time.time() - start, 1)

    answer = result.get("answer", "") or ""
    called = [s["tool"] for s in result.get("steps", [])]
    required = case["required_tools"]
    tool_hit = all(t in called for t in required)

    hits, misses = [], []
    for kw in case["expect_keywords"]:
        (hits if re.search(kw, answer, re.IGNORECASE) else misses).append(kw)
    conclusion_ratio = round(len(hits) / len(case["expect_keywords"]), 2) \
        if case["expect_keywords"] else 1.0

    return {
        "id": case["id"],
        "category": case["category"],
        "input": case["input"],
        "required_tools": required,
        "called_tools": called,
        "tool_hit": tool_hit,
        "expect_keywords": case["expect_keywords"],
        "keyword_hits": hits,
        "keyword_misses": misses,
        "conclusion_ratio": conclusion_ratio,
        "pass": tool_hit and conclusion_ratio >= CONCLUSION_THRESHOLD,
        "elapsed": elapsed,
        "turns": len(called),
        "answer_excerpt": answer[:400],
    }


def summarize(results, version):
    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    tool_acc = round(sum(r["tool_hit"] for r in results) / total * 100, 1)
    concl_avg = round(sum(r["conclusion_ratio"] for r in results) / total * 100, 1)
    avg_elapsed = round(sum(r["elapsed"] for r in results) / total, 1)

    cats = {}
    for r in results:
        c = cats.setdefault(r["category"], {"total": 0, "pass": 0})
        c["total"] += 1
        c["pass"] += 1 if r["pass"] else 0

    return {
        "version": version,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total * 100, 1),
        "tool_accuracy": tool_acc,
        "conclusion_avg": concl_avg,
        "avg_elapsed": avg_elapsed,
        "by_category": cats,
        "failures": [r for r in results if not r["pass"]],
    }


def write_report(summary, results, out_md):
    s = summary
    lines = [
        "# Ops Agent 排查效果评估报告",
        "",
        f"- 测试集版本：{s['version']} ｜ 用例数：{s['total']} ｜ 日期：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- **综合通过率：{s['pass_rate']}%（{s['passed']}/{s['total']}）**",
        f"- 工具调用准确率（期望工具全部命中）：**{s['tool_accuracy']}%**",
        f"- 结论要点平均命中率：**{s['conclusion_avg']}%**",
        f"- 单例平均耗时：{s['avg_elapsed']}s",
        "",
        "## 分类明细",
        "",
        "| 分类 | 通过/总数 | 通过率 |",
        "|---|---|---|",
    ]
    for cat, c in s["by_category"].items():
        lines.append(f"| {cat} | {c['pass']}/{c['total']} | {round(c['pass']/c['total']*100)}% |")

    lines += ["", "## 未通过用例（bad case）", ""]
    if not s["failures"]:
        lines.append("无。")
    for r in s["failures"]:
        lines += [
            f"### {r['id']}（{r['category']}）",
            f"- 输入：{r['input']}",
            f"- 工具：期望 {r['required_tools']}，实际 {r['called_tools']}",
            f"- 结论命中 {r['conclusion_ratio']}：命中 {r['keyword_hits']}，未中 {r['keyword_misses']}",
            f"- 回答摘录：{r['answer_excerpt'][:200]}",
            "",
        ]
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试）")
    args = ap.parse_args()

    cases, version = load_cases()
    if args.limit:
        cases = cases[: args.limit]

    print(f"开始评估：{len(cases)} 条用例（真实调用 LLM + 工具，预计 {len(cases)*20}s 左右）")
    results = []
    for i, case in enumerate(cases, 1):
        try:
            r = run_case(case)
        except Exception as e:
            r = {"id": case["id"], "category": case["category"],
                 "input": case["input"], "required_tools": case["required_tools"],
                 "called_tools": [], "tool_hit": False,
                 "expect_keywords": case["expect_keywords"], "keyword_hits": [],
                 "keyword_misses": case["expect_keywords"], "conclusion_ratio": 0,
                 "pass": False, "elapsed": 0, "turns": 0,
                 "answer_excerpt": f"执行异常: {e}"}
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"[{i}/{len(cases)}] {r['id']} {mark} "
              f"工具={'全中' if r['tool_hit'] else r['called_tools']} "
              f"结论={r['conclusion_ratio']} {r['elapsed']}s")
        results.append(r)

    summary = summarize(results, version)
    out_json = os.path.join(EVAL_DIR, "evaluation_results.json")
    out_md = os.path.join(EVAL_DIR, "evaluation_report.md")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=2)
    write_report(summary, results, out_md)

    print("\n========== 汇总 ==========")
    print(f"通过率 {summary['pass_rate']}% ({summary['passed']}/{summary['total']}) ｜ "
          f"工具准确率 {summary['tool_accuracy']}% ｜ 结论命中 {summary['conclusion_avg']}% ｜ "
          f"均耗时 {summary['avg_elapsed']}s")
    for cat, c in summary["by_category"].items():
        print(f"  {cat}: {c['pass']}/{c['total']}")
    print(f"\n报告已写入: {out_md}")


if __name__ == "__main__":
    main()
