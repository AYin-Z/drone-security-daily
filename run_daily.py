#!/usr/bin/env python3
"""无人机感知与反制技术日报 Agent —— 主入口。

用法:
  python3 run_daily.py                        # 用 config.yaml，当天执行
  python3 run_daily.py --date 2026-08-18      # 指定日期（测试）
  python3 run_daily.py --mock-llm             # 强制 mock 模式（未配 API key 时自动启用）
  python3 run_daily.py --dry-run-mail         # 邮件只生成 .eml 不真实发送
  python3 run_daily.py --no-email             # 不执行邮件步骤

退出码: 0=成功（含无文章但已生成报告）, 1=致命错误
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dsdaily.agent import DailyAgent, summary_text
from dsdaily.config import load_config
from dsdaily.tracelog import TraceLog, new_run_id


def main() -> int:
    parser = argparse.ArgumentParser(description="无人机感知与反制技术日报 Agent")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--date", default=None, help="执行日期 YYYY-MM-DD（默认当天）")
    parser.add_argument("--mock-llm", action="store_true", help="强制 mock 模式（不调 LLM API）")
    parser.add_argument("--dry-run-mail", action="store_true", help="邮件 dry-run：只生成 .eml")
    parser.add_argument("--no-email", action="store_true", help="跳过邮件步骤")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.get("runtime", {}).get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=tz)
    else:
        day = now

    log_dir = cfg.path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / f"run-{day.strftime('%Y-%m-%d')}.log", encoding="utf-8"),
        ],
    )
    log = logging.getLogger("run_daily")

    day_str = day.strftime("%Y-%m-%d")
    run_id = new_run_id(day_str)
    trace_dir = cfg.path("data", "traces")
    trace_dir.mkdir(parents=True, exist_ok=True)
    # trace 文件按 run_id 隔离：同日多次运行互不追加污染（P0 修复）
    trace = TraceLog(run_id, trace_dir / f"agent-trace-{run_id}.jsonl")
    trace.step(stage="prepare", action="process", tool="init",
               input_summary=f"日期={day_str} 配置={args.config}",
               output_summary=f"run_id={run_id} mock={args.mock_llm or not cfg['llm'].get('api_key')}")

    try:
        agent = DailyAgent(cfg, trace, day)
        result = agent.run(dry_run_mail=args.dry_run_mail, force_mock=args.mock_llm,
                           send_email=False if args.no_email else None)
        print("=" * 64)
        print("  运行完成 —— 无人机感知与反制技术日报")
        print("=" * 64)
        print(summary_text(result))
        print("=" * 64)
        trace.close()
        # 邮件等关键步骤失败时非零退出，便于 cron/监控感知（P2 修复）
        return 1 if result.error else 0
    except Exception as e:  # noqa: BLE001
        log.exception("运行失败")
        trace.step(stage="fatal", action="error", tool=None,
                   input_summary="", output_summary=str(e),
                   status="error", error={"type": type(e).__name__, "msg": str(e)[:500]})
        trace.done(result="error")
        trace.close()
        print(f"运行失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
