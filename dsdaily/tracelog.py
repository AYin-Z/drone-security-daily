"""逐步执行日志（Trace 去黑箱化核心）。

设计（依据调研结论，字段清单见 02-Trace去黑箱化方案报告.md §3.2）：
- 每步执行（LLM 调用 / 工具调用 / 阶段决策 / 错误 / 完成）追加一行 JSON（JSONL，append-only）；
- 完整内容（搜索结果全文等）不写入日志行，以 input_summary/output_summary 摘要 + artifact 引用；
- 结束写 action=done 汇总行；
- 提供 render_txt() 把 JSONL 渲染为人类可读的「步骤序号·时间·动作→结果」清单，
  该 txt 作为邮件附件随日报发送（去黑箱化交付物）。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class TraceLog:
    def __init__(self, run_id: str, jsonl_path: Path):
        self.run_id = run_id
        self.jsonl_path = Path(jsonl_path)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.jsonl_path.open("a", encoding="utf-8")
        self._step_no = 0
        self._started = time.time()
        self._error_count = 0
        self._total_tokens = {"input": 0, "output": 0}

    # ------------------------------------------------------------------ 记录
    def step(
        self,
        stage: str,
        action: str,
        tool: Optional[str] = None,
        input_summary: str = "",
        output_summary: str = "",
        duration_ms: Optional[float] = None,
        tokens: Optional[dict] = None,
        cost_usd: Optional[float] = None,
        status: str = "ok",
        error: Optional[dict] = None,
        artifact: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """追加一行步骤记录，返回步骤序号。"""
        self._step_no += 1
        if status == "error":
            self._error_count += 1
        if tokens:
            self._total_tokens["input"] += tokens.get("input", 0)
            self._total_tokens["output"] += tokens.get("output", 0)
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "step_no": self._step_no,
            "stage": stage,
            "action": action,
            "tool": tool,
            "input_summary": _truncate(input_summary),
            "output_summary": _truncate(output_summary),
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "tokens": tokens,
            "cost_usd": cost_usd,
            "status": status,
            "error": error,
            "artifact": artifact,
            "metadata": metadata,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        return self._step_no

    def done(self, result: str = "success") -> int:
        """收尾汇总行。"""
        steps_before = self._step_no
        return self.step(
            stage="done",
            action="done",
            output_summary=f"运行结束，结果={result}，"
            f"总步骤={steps_before}，总耗时={time.time() - self._started:.1f}s，"
            f"总token={self._total_tokens['input'] + self._total_tokens['output']}",
            duration_ms=(time.time() - self._started) * 1000,
            metadata={"error_count": self._error_count, "total_tokens": self._total_tokens},
        )

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ 读取
    @staticmethod
    def read_events(jsonl_path: Path) -> list[dict]:
        events = []
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return events

    # ------------------------------------------------------------------ 渲染
    def render_txt(self, extra_header: str = "") -> str:
        """渲染为人类可读的逐步执行日志（邮件附件交付物）。"""
        return self.render_txt_from(self.jsonl_path, extra_header)

    def render_html(self) -> str:
        """渲染为可视化 HTML（独立单文件、无 CDN、深色技术风）。"""
        return self.render_html_from(self.jsonl_path)

    # ------------------------------------------------------------------ 静态
    @staticmethod
    def render_html_from(jsonl_path: Path) -> str:
        events = TraceLog.read_events(jsonl_path)
        return _html_page(events)

    @staticmethod
    def render_txt_from(jsonl_path: Path, extra_header: str = "") -> str:
        events = TraceLog.read_events(jsonl_path)
        if not events:
            return "(无日志记录)"
        first = events[0]
        run_id = first.get("run_id", "?")
        start_ts = first.get("timestamp", "")
        done_ev = next((e for e in reversed(events) if e.get("action") == "done"), None)
        errs = [e for e in events if e.get("status") == "error"]

        lines = []
        lines.append("=" * 68)
        lines.append("  无人机感知与反制技术日报 —— Agent 执行日志（去黑箱化记录）")
        lines.append("=" * 68)
        lines.append(f"  运行 ID    : {run_id}")
        lines.append(f"  开始时间   : {start_ts}")
        if done_ev:
            lines.append(f"  结束时间   : {done_ev.get('timestamp', '')}")
            lines.append(f"  运行结果   : {done_ev.get('output_summary', '')}")
        lines.append(f"  错误步骤   : {len(errs)}")
        if extra_header:
            lines.append(extra_header)
        lines.append("-" * 68)
        lines.append("  步骤清单（时间 阶段 动作 → 结果摘要）")
        lines.append("-" * 68)
        for ev in events:
            no = ev.get("step_no")
            ts = _fmt_ts(ev.get("timestamp"))
            stage = ev.get("stage", "")
            action = ev.get("action", "")
            tool = ev.get("tool") or ""
            dur = ev.get("duration_ms")
            dur_s = f" ({dur / 1000:.1f}s)" if dur is not None else ""
            tok = ev.get("tokens")
            tok_s = f", tok {tok.get('input', 0) + tok.get('output', 0)}" if tok else ""
            status = ev.get("status", "ok")
            marker = "⚠️ " if status == "error" else ""
            label = tool if tool else action
            io = ""
            if ev.get("input_summary"):
                io += f" 输入: {ev['input_summary']}"
            if ev.get("output_summary"):
                io += f" → {ev['output_summary']}"
            if ev.get("artifact"):
                io += f" [产出: {ev['artifact']}]"
            err = ""
            if ev.get("error"):
                err = f" 错误: {json.dumps(ev['error'], ensure_ascii=False)}"
            lines.append(f"[{no:02d}] {ts} {marker}{stage:<8} {label}{dur_s}{tok_s}{io}{err}")
        lines.append("-" * 68)
        if errs:
            lines.append("  错误明细：")
            for e in errs:
                lines.append(f"    - 步骤 {e.get('step_no')} [{e.get('stage')}/{e.get('tool') or e.get('action')}]: "
                             f"{json.dumps(e.get('error'), ensure_ascii=False)}")
        else:
            lines.append("  错误: 无")
        lines.append("=" * 68)
        lines.append("  本日志由 Agent 系统自动生成，完整 JSONL 见同目录 .jsonl 文件")
        return "\n".join(lines)


def _fmt_ts(iso: Optional[str]) -> str:
    if not iso:
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return iso[:19]


def _truncate(s: str, limit: int = 400) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + f"...(截断,共{len(s)}字符)"


def new_run_id(day_str: str) -> str:
    return f"run_{day_str.replace('-', '')}_{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- HTML 渲染
STAGE_COLORS = {
    "prepare": ("#EFF6FF", "#1E3A5F", "准备"),
    "search": ("#ECFDF5", "#065F46", "检索"),
    "filter": ("#FEF3C7", "#92400E", "筛选"),
    "llm": ("#F3E8FF", "#6B21A8", "LLM"),
    "render": ("#EBF4FF", "#1E40AF", "渲染"),
    "email": ("#FFF7ED", "#9A3412", "邮件"),
    "done": ("#F1F5F9", "#334155", "完成"),
    "fatal": ("#FEF2F2", "#991B1B", "致命"),
}


def _html_page(events: list[dict]) -> str:
    if not events:
        return "<html><body><h2>（无日志记录）</h2></body></html>"
    first = events[0]
    run_id = first.get("run_id", "?")
    done_ev = next((e for e in reversed(events) if e.get("action") == "done"), None)
    errs = [e for e in events if e.get("status") == "error"]
    # 结果以 done 汇总行为准（部分步骤降级出错 ≠ 运行失败）
    done_result = ""
    if done_ev:
        m = re.search(r"结果=(\w+)", done_ev.get("output_summary") or "")
        done_result = m.group(1) if m else ""
    ok = done_result == "success"
    result_txt = "成功" if ok else ("失败" if errs else "运行中")
    start_ts = (first.get("timestamp") or "")[:19].replace("T", " ")
    end_ts = (done_ev.get("timestamp") or "")[:19].replace("T", " ") if done_ev else ""
    total_tok = sum((e.get("tokens") or {}).get("input", 0) + (e.get("tokens") or {}).get("output", 0)
                    for e in events if e.get("tokens"))
    total_dur = sum(e.get("duration_ms") or 0 for e in events) / 1000.0

    stage_set = sorted({e.get("stage", "?") for e in events})
    stage_btns = "".join(
        f'<button class="fbtn" data-f="stage-{_esc(s)}">{_esc(STAGE_COLORS.get(s, ("#eee", "#333", s))[2])}</button>'
        for s in stage_set)

    rows = []
    for ev in events:
        rows.append(_html_step_row(ev))
    steps_html = "\n".join(rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent 执行日志 · {_esc(run_id)}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         background:#f0f2f5; color:#1a202c; font-size:14px; line-height:1.6; }}
  .header {{ background:linear-gradient(135deg,#1a365d,#2b4c7e); color:#fff; padding:26px 20px; }}
  .header h1 {{ font-size:20px; letter-spacing:1px; }}
  .meta {{ font-size:13px; opacity:.85; margin-top:8px; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
  .chip {{ background:rgba(255,255,255,.12); border-radius:14px; padding:4px 12px; font-size:12px; }}
  .chip.err {{ background:rgba(220,38,38,.35); }}
  .badge {{ display:inline-block; padding:2px 12px; border-radius:12px; font-size:12px; }}
  .badge.ok {{ background:#22c55e; color:#fff; }}
  .badge.fail {{ background:#dc2626; color:#fff; }}
  .container {{ max-width:980px; margin:0 auto; padding:14px; }}
  .toolbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }}
  .fbtn {{ border:1px solid #d8dde6; background:#fff; color:#42526b; border-radius:14px;
          padding:4px 12px; font-size:12px; cursor:pointer; }}
  .fbtn.active {{ background:#1a365d; border-color:#1a365d; color:#fff; }}
  .search {{ flex:1; min-width:160px; border:1px solid #d8dde6; border-radius:14px;
            padding:5px 12px; font-size:13px; }}
  .step {{ background:#fff; border-radius:10px; margin:10px 0; padding:12px 16px;
          box-shadow:0 1px 3px rgba(16,32,64,.08); cursor:pointer; }}
  .step:hover {{ box-shadow:0 3px 10px rgba(16,32,64,.14); }}
  .step.err {{ border-left:3px solid #dc2626; }}
  .stephead {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
  .no {{ font-weight:700; color:#1a365d; width:34px; }}
  .time {{ color:#8492a6; font-size:12px; }}
  .stag {{ padding:1px 10px; border-radius:10px; font-size:12px; }}
  .tool {{ font-weight:600; color:#334155; font-size:13px; }}
  .dur {{ color:#8492a6; font-size:12px; }}
  .dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; }}
  .dot.ok {{ background:#22c55e; }} .dot.err {{ background:#dc2626; }} .dot.skip {{ background:#94a3b8; }}
  .io {{ margin-top:8px; font-size:13px; color:#3d4a5d; }}
  .io b {{ color:#1a365d; }}
  .io .in {{ color:#475569; }} .io .out {{ color:#0f766e; }}
  .detail {{ display:none; margin-top:10px; border-top:1px dashed #e2e8f0; padding-top:8px; }}
  .detail pre {{ background:#0f172a; color:#e2e8f0; border-radius:8px; padding:10px;
                font-size:12px; overflow-x:auto; max-height:320px; white-space:pre-wrap; word-break:break-all; }}
  .errdetail {{ background:#fef2f2; border:1px solid #fecaca; border-radius:8px; padding:8px 12px;
               color:#991b1b; font-size:13px; margin-top:8px; }}
  .empty {{ text-align:center; color:#8492a6; padding:30px 0; }}
  .footer {{ text-align:center; color:#8492a6; font-size:12px; padding:16px; }}
  @media (max-width:600px) {{ body {{ font-size:13px; }} .header h1 {{ font-size:17px; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>🤖 Agent 执行日志（去黑箱化可视化）</h1>
  <div class="meta">运行 ID：{_esc(run_id)} ｜ 开始 {_esc(start_ts)} ｜ 结束 {_esc(end_ts)}</div>
  <div class="chips">
    <span class="chip">结果：<span class="badge {'ok' if ok else 'fail'}">{result_txt}</span></span>
    <span class="chip">步骤：{len(events)}</span>
    <span class="chip">总耗时：{total_dur:.1f}s</span>
    <span class="chip">总 token：{total_tok}</span>
    <span class="chip {'err' if errs else ''}">错误：{len(errs)}</span>
  </div>
</div>
<div class="container">
  <div class="toolbar">
    <button class="fbtn active" data-f="all">全部</button>
    {stage_btns}
    <button class="fbtn" data-f="err">仅错误</button>
    <input class="search" id="q" placeholder="搜索：工具名 / 动作 / 摘要关键字…">
  </div>
  <div id="steps">{steps_html}</div>
  <div class="empty" id="empty" style="display:none">无匹配步骤</div>
</div>
<div class="footer">本日志由 Agent 系统自动生成 · 完整 JSONL 见同目录 .jsonl 文件</div>
<script>
(function(){{
  var btns = document.querySelectorAll('.fbtn');
  var steps = document.querySelectorAll('.step');
  var q = document.getElementById('q');
  var current = 'all';
  function apply(){{
    var kw = (q.value || '').toLowerCase();
    var visible = 0;
    steps.forEach(function(s){{
      var show = true;
      if (current === 'err' && !s.classList.contains('err')) show = false;
      if (current.startsWith('stage-') && s.getAttribute('data-stage') !== current.slice(6)) show = false;
      if (show && kw) {{
        show = (s.getAttribute('data-text') || '').toLowerCase().indexOf(kw) !== -1;
      }}
      s.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    document.getElementById('empty').style.display = visible ? 'none' : '';
  }}
  btns.forEach(function(b){{
    b.addEventListener('click', function(){{
      btns.forEach(function(x){{ x.classList.remove('active'); }});
      b.classList.add('active');
      current = b.getAttribute('data-f');
      apply();
    }});
  }});
  q.addEventListener('input', apply);
  steps.forEach(function(s){{
    s.addEventListener('click', function(){{
      var d = s.querySelector('.detail');
      d.style.display = d.style.display === 'none' ? '' : 'none';
    }});
  }});
}})();
</script>
</body>
</html>"""


def _html_step_row(ev: dict) -> str:
    no = ev.get("step_no", "?")
    ts = (ev.get("timestamp") or "")[11:19]
    stage = ev.get("stage", "?")
    bg, fg, label = STAGE_COLORS.get(stage, ("#F1F5F9", "#334155", stage))
    tool = ev.get("tool") or ev.get("action", "")
    dur = ev.get("duration_ms")
    dur_s = f"⏱ {dur / 1000:.1f}s" if dur is not None else ""
    tok = ev.get("tokens")
    tok_s = f"tok {tok.get('input', 0) + tok.get('output', 0)}" if tok else ""
    status = ev.get("status", "ok")
    dot = "ok" if status == "ok" else ("err" if status == "error" else "skip")
    err_cls = " err" if status == "error" else ""
    in_s = _esc(ev.get("input_summary") or "")
    out_s = _esc(ev.get("output_summary") or "")
    art = ev.get("artifact") or ""
    art_html = f'<div class="io">📄 产物：{_esc(art)}</div>' if art else ""
    err_html = ""
    if ev.get("error"):
        err_html = f'<div class="errdetail">⚠️ {_esc(str(ev["error"]))}</div>'
    raw = _esc(str(ev))
    text = f"{tool} {in_s} {out_s}".lower()
    return f"""<div class="step{err_cls}" data-stage="{_esc(stage)}" data-text="{_esc(text)}">
  <div class="stephead">
    <span class="no">{no}</span>
    <span class="dot {dot}"></span>
    <span class="time">{_esc(ts)}</span>
    <span class="stag" style="background:{bg};color:{fg}">{_esc(label)}</span>
    <span class="tool">{_esc(tool)}</span>
    <span class="dur">{dur_s} {tok_s}</span>
  </div>
  <div class="io"><b>输入</b><div class="in">{in_s}</div></div>
  <div class="io"><b>结果</b><div class="out">{out_s}</div></div>
  {art_html}
  {err_html}
  <div class="detail"><pre>{raw}</pre></div>
</div>"""


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _esc_html(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _email_row(ev: dict) -> str:
    """邮件可用的内联样式步骤行（无 class/script，兼容各邮件客户端）。"""
    no = ev.get("step_no", "?")
    ts = (ev.get("timestamp") or "")[11:19]
    stage = ev.get("stage", "?")
    bg, fg, label = STAGE_COLORS.get(stage, ("#F1F5F9", "#334155", stage))
    tool = ev.get("tool") or ev.get("action", "")
    dur = ev.get("duration_ms")
    dur_s = f" · {dur / 1000:.1f}s" if dur is not None else ""
    tok = ev.get("tokens")
    tok_s = f" · tok {tok.get('input', 0) + tok.get('output', 0)}" if tok else ""
    status = ev.get("status", "ok")
    err_cls = "border-left:3px solid #dc2626;" if status == "error" else ""
    border = "border-bottom:1px solid #eef2f7;"
    in_s = _esc_html(ev.get("input_summary") or "")
    out_s = _esc_html(ev.get("output_summary") or "")
    err = _esc_html(str(ev.get("error") or ""))
    err_html = (f'<div style="color:#991B1B;font-size:12px;margin-top:4px;">⚠️ 错误：{err}</div>'
                if ev.get("error") else "")
    return (
        f'<div style="padding:8px 4px;{border}{err_cls}">'
        f'<div style="font-size:13px;">'
        f'<b style="color:#1a365d;">[{no:02d}]</b> <span style="color:#8492a6;font-size:12px;">{_esc_html(ts)}</span> '
        f'<span style="background:{bg};color:{fg};border-radius:4px;padding:1px 6px;font-size:12px;">{_esc_html(label)}</span> '
        f'<b style="color:#334155;font-size:13px;">{_esc_html(tool)}</b>'
        f'<span style="color:#8492a6;font-size:12px;">{dur_s}{tok_s}</span></div>'
        f'<div style="font-size:12px;color:#475569;margin-top:2px;">{in_s}</div>'
        f'<div style="font-size:12px;color:#0f766e;margin-top:2px;">→ {out_s}</div>'
        f'{err_html}</div>'
    )


def render_email_html_from(jsonl_path: Path) -> str:
    """邮件正文末尾追加的 trace 可视化片段（全部内联样式，无 script/class 依赖）。"""
    events = TraceLog.read_events(jsonl_path)
    if not events:
        return ""
    first = events[0]
    run_id = first.get("run_id", "?")
    done_ev = next((e for e in reversed(events) if e.get("action") == "done"), None)
    errs = [e for e in events if e.get("status") == "error"]
    ok = bool(done_ev) and "结果=success" in (done_ev.get("output_summary") or "")
    total_tok = sum((e.get("tokens") or {}).get("input", 0) + (e.get("tokens") or {}).get("output", 0)
                    for e in events if e.get("tokens"))
    total_dur = sum(e.get("duration_ms") or 0 for e in events) / 1000.0
    badge = ("background:#16a34a;color:#fff;border-radius:10px;padding:2px 10px;font-size:12px;"
             if ok else "background:#dc2626;color:#fff;border-radius:10px;padding:2px 10px;font-size:12px;")
    badge_txt = "成功" if ok else "失败"
    rows = "\n".join(_email_row(ev) for ev in events)
    return (
        f'<div style="margin:24px 0 6px;border-top:2px solid #1a365d;padding-top:14px;">'
        f'<h2 style="color:#1a365d;font-size:17px;margin:0 0 6px;">⚙️ Agent 执行日志（去黑箱化）</h2>'
        f'<div style="font-size:12px;color:#64748b;margin-bottom:8px;">'
        f'运行 ID：{_esc_html(run_id)} ｜ 结果：<span style="{badge}">{badge_txt}</span> ｜ '
        f'步骤 {len(events)} ｜ 耗时 {total_dur:.1f}s ｜ token {total_tok} ｜ 错误 {len(errs)}</div>'
        f'<div style="border:1px solid #e2e8f0;border-radius:8px;padding:2px 10px;background:#fff;">'
        f'{rows}</div>'
        f'<div style="font-size:12px;color:#8492a6;margin-top:6px;">'
        f'完整 JSONL 与可视化版见附件 agent-trace-*.jsonl / *.html</div></div>'
    )


TraceLog.render_email_html_from = staticmethod(render_email_html_from)


def _email_fragment_impl(self) -> str:
    return render_email_html_from(self.jsonl_path)


TraceLog.render_email_html = _email_fragment_impl
