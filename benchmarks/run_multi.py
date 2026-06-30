#!/usr/bin/env python3
"""
Run benchmark N times and compute average results.

Usage:
    python benchmarks/run_multi.py [--runs N]

This script runs the benchmark suite multiple times to eliminate model variance
and provide more reliable performance metrics.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmarks.benchmark import (
    BenchmarkRunner,
    BenchmarkReport,
    print_report,
    BENCHMARK_CASES,
)


async def run_once(runner: BenchmarkRunner, cases) -> BenchmarkReport:
    """Run benchmark once."""
    return await runner.run_all(cases)


async def main():
    parser = argparse.ArgumentParser(description="Run benchmark multiple times")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs (default: 3)")
    parser.add_argument("--output", type=str, help="Output JSON file path")
    args = parser.parse_args()

    api_key = os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        print("Error: set MODEL_API_KEY (mify) or OPENAI_API_KEY (any OpenAI-compatible endpoint)")
        sys.exit(1)

    # 与 benchmark.main() 一致的端点/模型/header 解析
    if os.environ.get("MODEL_API_KEY"):
        base_url = os.environ.get("MODEL_BASE_URL", "http://model.mify.ai.srv/v1")
        model = os.environ.get("MODEL_PRIMARY", "xiaomi/mimo-v2.5-pro")
    else:
        base_url = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
        model = os.environ.get("CODING_AGENT_MODEL", "gpt-4o-mini")

    extra_headers = {}
    raw_headers = os.environ.get("MODEL_EXTRA_HEADERS")
    if raw_headers:
        try:
            extra_headers = json.loads(raw_headers)
        except json.JSONDecodeError:
            print("Warning: MODEL_EXTRA_HEADERS is not valid JSON, ignoring")

    n_runs = args.runs
    print(f"Endpoint: {base_url}  Model: {model}  Runs: {n_runs}")
    runner = BenchmarkRunner(api_key=api_key, model=model, base_url=base_url,
                             extra_headers=extra_headers)

    all_reports: list[BenchmarkReport] = []
    for i in range(n_runs):
        print(f"\n{'='*60}")
        print(f"🔄 Run {i+1}/{n_runs}")
        print(f"{'='*60}")
        report = await run_once(runner, BENCHMARK_CASES)
        all_reports.append(report)
        print_report(report)

    # Compute average
    total_pass = sum(r.passed for r in all_reports)
    total_cases = sum(r.total for r in all_reports)
    avg_pass_rate = total_pass / total_cases * 100 if total_cases else 0

    print(f"\n{'='*60}")
    print(f"📊 Average across {n_runs} runs")
    print(f"{'='*60}")
    print(f"  Average pass rate: {avg_pass_rate:.1f}%")
    for i, r in enumerate(all_reports, 1):
        print(f"  Run {i}: {r.passed}/{r.total} ({r.passed/r.total*100:.1f}%)")

    # Per-case analysis
    case_results: dict[str, dict] = {}
    for r in all_reports:
        for res in r.results:
            if res.case_id not in case_results:
                case_results[res.case_id] = {
                    "passed": 0,
                    "total": 0,
                    "category": res.category,
                    "difficulty": res.difficulty,
                }
            case_results[res.case_id]["total"] += 1
            if res.passed:
                case_results[res.case_id]["passed"] += 1

    # Show cases with <100% pass rate
    print(f"\n  Cases with variance (not always passing):")
    variance_found = False
    for case_id, stats in sorted(case_results.items()):
        if stats["passed"] < stats["total"]:
            rate = stats["passed"] / stats["total"] * 100
            print(f"    {case_id} ({stats['category']}/{stats['difficulty']}): {stats['passed']}/{stats['total']} ({rate:.0f}%)")
            variance_found = True
    if not variance_found:
        print(f"    (none)")

    # Show always-failing cases
    print(f"\n  Cases that NEVER passed:")
    never_passed = False
    for case_id, stats in sorted(case_results.items()):
        if stats["passed"] == 0:
            print(f"    {case_id} ({stats['category']}/{stats['difficulty']})")
            never_passed = True
    if not never_passed:
        print(f"    (none)")

    # Save JSON report if requested
    if args.output:
        report_data = {
            "runs": n_runs,
            "avg_pass_rate": avg_pass_rate,
            "per_run": [
                {"run": i + 1, "passed": r.passed, "total": r.total, "rate": r.passed / r.total * 100}
                for i, r in enumerate(all_reports)
            ],
            "per_case": {
                case_id: {
                    "passed": stats["passed"],
                    "total": stats["total"],
                    "rate": stats["passed"] / stats["total"] * 100,
                    "category": stats["category"],
                    "difficulty": stats["difficulty"],
                }
                for case_id, stats in case_results.items()
            },
        }
        Path(args.output).write_text(json.dumps(report_data, indent=2))
        print(f"\n  Report saved to: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
