"""Agent 主编排：按需求 Prompt 的流程分步执行，每一步写入 trace。

流程（每一步都是 trace 中的一条记录）：
  准备(检索词) → 多渠道检索 → 筛选去重 → 分类标注 → 生成 HTML → 发送邮件 → 汇总
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .htmlrender import fmt_published, render_daily
from .llm import LLMClient
from .mailer import MailerError, send_daily_report
from .search import Article, SearchLayer, dedupe, filter_by_time
from .tracelog import TraceLog

log = logging.getLogger("dsdaily.agent")

FOCUS_HINTS = ["黑飞", "入侵", "拦截", "击落", "新规", "条例", "发布", "首飞", "交付",
               "测试", "演练", "实战", "重大", "破获", "查获"]


@dataclass
class DailyResult:
    day: str
    run_id: str
    keywords: list = field(default_factory=list)
    candidates: int = 0
    kept: list = field(default_factory=list)       # 最终收录文章（dict）
    focus: dict | None = None
    html_path: str = ""
    trace_path: str = ""
    trace_jsonl: str = ""
    mail: dict = field(default_factory=dict)
    extended_lookback: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "day": self.day, "run_id": self.run_id, "keywords": self.keywords,
            "candidates": self.candidates, "kept": len(self.kept),
            "focus": bool(self.focus), "html_path": self.html_path,
            "trace_path": self.trace_path, "mail": self.mail,
            "extended_lookback": self.extended_lookback, "error": self.error,
        }


class DailyAgent:
    def __init__(self, config: Config, trace: TraceLog, day: datetime):
        self.cfg = config
        self.trace = trace
        self.day = day
        s = config["search"]
        self.llm = LLMClient(config["llm"], trace)
        self.search = SearchLayer(s, trace)

    # ------------------------------------------------------------------ 主流程
    def run(self, dry_run_mail: bool | None = None,
            force_mock: bool = False, send_email: bool | None = None) -> DailyResult:
        cfg = self.cfg
        day_str = self.day.strftime("%Y-%m-%d")
        result = DailyResult(day=day_str, run_id=self.trace.run_id)

        if force_mock:
            self.llm.mock = True

        # 0) 准备目录与状态
        report_dir = cfg.path("data", "reports")
        trace_dir = cfg.path("data", "traces")
        state_dir = cfg.path("data", "state")
        email_dir = cfg.path("data", "emails")
        for d in (report_dir, trace_dir, state_dir, email_dir):
            d.mkdir(parents=True, exist_ok=True)
        seen_path = state_dir / "seen.json"
        seen: set[str] = set()
        if seen_path.exists():
            try:
                seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                seen = set()

        # 1) 检索词
        t0 = time.time()
        if self.llm.mock:
            result.keywords = self.llm.generate_keywords(cfg["search"]["keywords"], day_str)
            self.trace.step(stage="prepare", action="llm_call", tool="generate_keywords",
                            input_summary=f"基础词 {len(cfg['search']['keywords'])} 个",
                            output_summary=f"生成 {len(result.keywords)} 个（mock）",
                            duration_ms=(time.time() - t0) * 1000)
        else:
            result.keywords = self.llm.generate_keywords(cfg["search"]["keywords"], day_str)

        # 2) 多渠道检索（关键词驱动：新闻搜索/公众号 API 使用当日检索词）
        candidates = self.search.collect(result.keywords)
        result.candidates = len(candidates)
        log.info("候选文章 %d 篇", len(candidates))

        # 3) 去重
        articles = dedupe(candidates, self.trace, seen)
        # 4) 时间窗（不足补回溯）
        t0 = time.time()
        window = int(cfg["search"].get("window_hours", 24))
        lookback = int(cfg["search"].get("lookback_hours", 48))
        min_n = int(cfg["search"].get("min_articles", 6))
        n_before_window = len(articles)
        articles, extended = filter_by_time(articles, window, lookback, self.day)
        result.extended_lookback = extended
        self.trace.step(stage="filter", action="process", tool="time_window",
                        input_summary=f"{n_before_window} 条（时间窗 {window}h）",
                        output_summary=(f"{len(articles)} 条，启用 {lookback}h 回溯补充"
                                        if extended else f"{len(articles)} 条，当天文章充足"),
                        duration_ms=(time.time() - t0) * 1000)
        if extended:
            log.info("当日文章不足 %d 篇，已回溯至 %dh 前", min_n, lookback)

        # 5) 相关性筛选（LLM）。宁缺毋滥：LLM 正常返回时尊重其判定（即使全拒）；
        #    仅当 LLM 调用失败（无判定结果）时降级保留前若干篇。
        #    先做「关键词命中优先」预筛：候选远超上限时保证相关文章不被挤出。
        cap = int(cfg["search"].get("max_candidates", 120))
        if len(articles) > cap:
            kw_pool = list(result.keywords) + list(cfg["search"].get("keywords", [])) + list(FOCUS_HINTS)
            hits = [a for a in articles if any(k.lower() in a.title.lower() for k in kw_pool if len(k) >= 2)]
            articles = (hits + [a for a in articles if a not in hits])[:cap]
            self.trace.step(stage="filter", action="process", tool="prefilter",
                            input_summary=f"候选 {len(articles)} 条（关键词命中优先）",
                            output_summary=f"截取前 {cap} 条供 LLM 判定")
        articles = articles[:cap]
        if articles:
            decisions = self.llm.filter_articles([_to_llm_item(a) for a in articles])
            if decisions:
                keep_idx = {d["index"] for d in decisions if d.get("keep")}
                articles = [a for i, a in enumerate(articles) if i in keep_idx]
                if not articles:
                    self.trace.step(stage="filter", action="process", tool="llm_filter",
                                    input_summary=f"{len(decisions)} 篇判定",
                                    output_summary="LLM 判定全部不相关，按宁缺毋滥收录 0 篇")
            else:
                self.trace.step(stage="filter", action="process", tool="llm_filter",
                                input_summary="", output_summary="LLM 筛选失败，降级保留前若干篇",
                                status="error", error={"type": "llm_filter_failed", "msg": "无判定结果"})
                articles = articles[: max(min_n, 6)]
        else:
            articles = []
        log.info("筛选后 %d 篇", len(articles))

        # 6) 分类 + 摘要（LLM）
        enriched: list[dict] = []
        if articles:
            results = self.llm.classify_and_summarize([_to_llm_item(a) for a in articles])
            by_idx = {r["index"]: r for r in results}
            for i, a in enumerate(articles):
                r = by_idx.get(i, {})
                enriched.append({
                    "title": a.title, "url": a.url, "source": a.source,
                    "published": a.published or "",
                    "published_display": fmt_published(a.published, self.day),
                    "tags": r.get("tags") or [],
                    "summary": r.get("summary") or a.excerpt[:100],
                    "focus": bool(r.get("focus")),
                })
        # focus 兜底规则：无 LLM focus 时按重大关键词挑选 1 篇
        focus = next((a for a in enriched if a["focus"]), None)
        if not focus:
            for a in enriched:
                if any(h in a["title"] for h in FOCUS_HINTS):
                    focus = a
                    a["focus"] = True
                    break
        result.focus = focus
        result.kept = enriched
        for a in enriched:
            seen.add(_key_of(a["url"]))
        seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1), encoding="utf-8")

        # 7) 生成 HTML
        t0 = time.time()
        generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        html = render_daily(enriched, self.day, generated_at, focus)
        html_path = report_dir / f"drone-security-daily-{day_str}.html"
        html_path.write_text(html, encoding="utf-8")
        result.html_path = str(html_path)
        self.trace.step(stage="render", action="tool_call", tool="render_html",
                        input_summary=f"{len(enriched)} 篇文章",
                        output_summary=f"已生成 {html_path.name}",
                        duration_ms=(time.time() - t0) * 1000, artifact=str(html_path))

        # 8) 结束汇总 + 渲染 trace 文本与可视化 HTML（邮件附件交付物；done 之后再渲染，保证完整）
        #    文件名按 run_id 隔离（同日多次运行互不污染）；另写一份「当日最新」副本便于运维查看
        t0 = time.time()
        self.trace.done(result="error" if result.error else "success")
        trace_txt = self.trace.render_txt()
        trace_html = self.trace.render_html()
        run_txt_path = trace_dir / f"agent-trace-{self.trace.run_id}.txt"
        run_html_path = trace_dir / f"agent-trace-{self.trace.run_id}.html"
        run_txt_path.write_text(trace_txt, encoding="utf-8")
        run_html_path.write_text(trace_html, encoding="utf-8")
        day_txt_path = trace_dir / f"agent-trace-{day_str}.txt"
        day_html_path = trace_dir / f"agent-trace-{day_str}.html"
        day_txt_path.write_text(trace_txt, encoding="utf-8")   # 当日最新副本（每次运行覆盖）
        day_html_path.write_text(trace_html, encoding="utf-8")
        result.trace_path = str(run_txt_path)
        result.trace_jsonl = str(self.trace.jsonl_path)
        self.trace.step(stage="render", action="tool_call", tool="render_trace",
                        input_summary=str(self.trace.jsonl_path),
                        output_summary=f"已生成 {run_txt_path.name} + {run_html_path.name}",
                        duration_ms=(time.time() - t0) * 1000, artifact=str(run_html_path))

        # 9) 发送邮件（发送步骤记入 trace；发完后重渲染一次，使磁盘上的 txt/html 含发送记录）
        if send_email is None:
            send_email = bool(cfg["runtime"].get("send_email", True))
        if send_email:
            smtp = cfg["smtp"]
            subject = f"无人机感知与反制技术日报 {day_str}"
            body = html
            attachments = [("agent-trace-%s.txt" % self.trace.run_id,
                            trace_txt.encode("utf-8"), "text/plain")]
            if smtp.get("attach_trace_html", True):
                attachments.append(("agent-trace-%s.html" % self.trace.run_id,
                                    trace_html.encode("utf-8"), "text/html"))
            if smtp.get("attach_report_html"):
                attachments.append((f"drone-security-daily-{day_str}.html",
                                    html.encode("utf-8"), "text/html"))
            dry = smtp.get("dry_run", True) if dry_run_mail is None else dry_run_mail
            try:
                result.mail = send_daily_report(
                    smtp, subject, body, attachments,
                    dry_run=dry, eml_dir=email_dir, trace=self.trace)
                final_txt = self.trace.render_txt()
                final_html = self.trace.render_html()
                run_txt_path.write_text(final_txt, encoding="utf-8")
                run_html_path.write_text(final_html, encoding="utf-8")
                day_txt_path.write_text(final_txt, encoding="utf-8")
                day_html_path.write_text(final_html, encoding="utf-8")
            except MailerError as e:
                result.error = str(e)
                log.error("邮件步骤失败: %s", e)
                final_txt = self.trace.render_txt()
                final_html = self.trace.render_html()
                run_txt_path.write_text(final_txt, encoding="utf-8")
                run_html_path.write_text(final_html, encoding="utf-8")
                day_txt_path.write_text(final_txt, encoding="utf-8")
                day_html_path.write_text(final_html, encoding="utf-8")
        else:
            self.trace.step(stage="email", action="skip", tool="send_email",
                            input_summary="runtime.send_email=false", output_summary="跳过发信")
            final_txt = self.trace.render_txt()
            final_html = self.trace.render_html()
            run_txt_path.write_text(final_txt, encoding="utf-8")
            run_html_path.write_text(final_html, encoding="utf-8")
            day_txt_path.write_text(final_txt, encoding="utf-8")
            day_html_path.write_text(final_html, encoding="utf-8")

        return result


def _to_llm_item(a: Article) -> dict:
    return {"title": a.title, "url": a.url, "source": a.source,
            "published": a.published or "", "excerpt": a.excerpt}


def _key_of(url: str) -> str:
    import hashlib
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def summary_text(result: DailyResult) -> str:
    lines = [
        f"日期: {result.day} | run_id: {result.run_id}",
        f"检索词: {len(result.keywords)} 组 | 候选: {result.candidates} 篇 | 收录: {len(result.kept)} 篇",
        f"分类覆盖: {len({t for a in result.kept for t in a['tags']})} 个方向 | 今日焦点: {'是' if result.focus else '否'}",
        f"HTML: {result.html_path}",
        f"Trace: {result.trace_path}",
        f"邮件: {result.mail.get('detail', result.error or '未发送')}",
    ]
    return "\n".join(lines)
