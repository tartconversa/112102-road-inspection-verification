#!/usr/bin/env python3
"""端到端可复跑演示：基线快照 -> 第2轮事件重放 -> 运行后快照 -> 报告差异 -> 线索追踪。

在隔离模式（python -I）下运行；只依赖标准库。
所有输出写入独立 runtime 目录，来源台账 sources/ 永不修改。

用法：
  python3 -I app/demo.py
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
RUNTIME = ROOT / "runtime"
SCENARIO = ROOT / "scenarios" / "round2.json"


def run(args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "run.py",
         "--sources", str(ROOT / "sources"),
         "--runtime", str(RUNTIME), *args],
        cwd=APP, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(args)}\n{proc.stderr or proc.stdout}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"raw": proc.stdout}


def main() -> None:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True)

    print("== 1. 基线只读分析（seq=0） ==")
    analysis_dir = RUNTIME / "analysis"
    run(["analyze", "--out", str(analysis_dir)])
    baseline = run(["snapshot", "--baseline",
                    "--report-id", "R1-baseline",
                    "--title", "第1轮巡查方式比较报告",
                    "--out", str(RUNTIME / "reports")])
    print("   基线事件序号：", baseline["event_seq"])

    print("== 2. 重放第2轮值班/抽查事件 ==")
    replayed = run(["replay", "--scenario", str(SCENARIO)])
    print("   重放事件数：", replayed["replayed"])

    print("== 3. 生成运行后不可变快照 ==")
    round2 = run(["snapshot",
                  "--report-id", "R2-round2",
                  "--title", "第2轮运行后比较快照",
                  "--out", str(RUNTIME / "reports")])
    print("   快照事件序号：", round2["event_seq"])

    print("== 4. 报告差异（可复核已使用报告的后续变化） ==")
    diff = run(["report-diff", "--left", "R1-baseline", "--right", "R2-round2"])
    print("   变化字段数：", len(diff["changed_fields"]))

    print("== 5. 线索/抽查去向追踪 ==")
    for key in ("P4", "P40", "V11", "R2-R-01-D1", "P28"):
        trace = run(["trace", "--key", key])
        print(f"   {key}: {trace['classification']['unexecuted_reason']}"
              f"（随机发现={trace['classification']['is_random_discovery']}）")

    print("== 6. 现行维修待办 ==")
    backlog = run(["backlog"])
    print("   待核实紧急线索：",
          [l["report"] for l in backlog["pending_urgent_leads"]])
    print("   关闭路段：", [c["segment"] for c in backlog["closures"]])

    print("\n输出目录：", RUNTIME)
    print("报告：", sorted(p.name for p in (RUNTIME / "reports").iterdir()))


if __name__ == "__main__":
    main()
