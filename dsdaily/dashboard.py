"""部署后管理面板（Dashboard）—— 纯标准库实现，无第三方依赖。

功能：
  · 自定义发件邮箱 / 收件邮箱 / 发件人名称 / dry-run
  · 自定义 LLM（base_url / api_key / model）
  · 自定义定时（cron 表达式 + 一键安装/移除 crontab）
  · 自定义数据源渠道（RSS / 站点直抓 / 微信公众号 / 关键词 / 新闻引擎）
  · 手动触发一次日报运行 + 实时查看日志
  · 查看/下载历史日报与执行日志（txt / 可视化 html / jsonl）

启动：python3 -m dsdaily.dashboard   （配置见 config.yaml 的 dashboard 段）
安全：默认绑定 127.0.0.1；设置了 dashboard.password 后需登录。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .config import load_config

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA = ROOT / "data"

_tokens: set[str] = set()
_runs: dict[str, dict] = {}          # run_id -> {pid, log, start}
_lock = threading.Lock()


# ------------------------------------------------------------------ 配置读写
def _load() -> dict:
    cfg = load_config(CONFIG_PATH)
    return cfg.data


def _save(data: dict):
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _redact(data: dict) -> dict:
    """向前端返回脱敏配置（key/密码只显示尾部）。"""
    import copy
    d = copy.deepcopy(data)
    llm = d.get("llm", {})
    if llm.get("api_key"):
        k = llm["api_key"]
        llm["api_key"] = "sk-***" + k[-4:] if len(k) > 8 else "***"
    smtp = d.get("smtp", {})
    if smtp.get("password"):
        smtp["password"] = "***" + str(smtp["password"])[-4:]
    return d


def _restore_secrets(prev: dict, new: dict) -> dict:
    """保存时恢复未修改的密钥（前端传回的是脱敏占位）。"""
    for sec, key in (("llm", "api_key"), ("smtp", "password")):
        old = (prev.get(sec) or {}).get(key)
        val = (new.get(sec) or {}).get(key)
        if val and ("***" in str(val) or str(val).startswith("sk-***")):
            if old:
                new.setdefault(sec, {})[key] = old
    return new


# ------------------------------------------------------------------ crontab
def _cron_installed() -> bool:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        return "run_daily.py" in out.stdout
    except Exception:  # noqa: BLE001
        return False


def _cron_apply(data: dict, action: str) -> str:
    cron = (data.get("schedule") or {}).get("cron", "").strip()
    if action == "install" and not cron:
        return "错误: schedule.cron 为空"
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout or ""
        lines = [ln for ln in cur.splitlines() if "run_daily.py" not in ln]
        if action == "install":
            line = (f"{cron} cd {ROOT} && /usr/bin/env bash -c "
                    f"'source .venv/bin/activate && python3 run_daily.py >> logs/cron.log 2>&1'  # drone-security-daily")
            lines.append(line)
            new = "\n".join(lines) + "\n"
        else:
            new = "\n".join(lines)
            if lines:
                new += "\n"
        subprocess.run(["crontab", "-"], input=new, text=True, timeout=10, check=True)
        return f"定时已{'安装' if action == 'install' else '移除'}: {cron}"
    except Exception as e:  # noqa: BLE001
        return f"错误: {e}"


# ------------------------------------------------------------------ 手动运行
def _start_run(mock: bool) -> str:
    run_id = f"manual_{secrets.token_hex(3)}"
    log_path = ROOT / "logs" / f"manual-{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "run_daily.py"]
    if mock:
        args.append("--mock-llm")
    with open(log_path, "w", encoding="utf-8") as f:
        p = subprocess.Popen(args, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT)
    _runs[run_id] = {"pid": p.pid, "log": str(log_path), "start": run_id}
    return run_id


# ------------------------------------------------------------------ HTTP 处理
class Handler(BaseHTTPRequestHandler):
    server_version = "DSHDailyDash/1.0"

    # -- 工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        cfg = _load().get("dashboard", {})
        pw = str(cfg.get("password") or "")
        if not pw:
            return True
        cookie = self.headers.get("Cookie") or ""
        return any(f"dsdash={t}" in cookie and t in _tokens for t in _tokens)

    def _body(self) -> dict:
        ln = int(self.headers.get("Content-Length") or 0)
        if not ln:
            return {}
        raw = self.rfile.read(ln)
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}

    def _send_page(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, rel: str):
        p = (DATA / rel).resolve()
        if not str(p).startswith(str(DATA.resolve())) or not p.is_file():
            self._json({"error": "文件不存在"}, 404)
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'inline; filename="{p.name}"')
        self.end_headers()
        self.wfile.write(body)

    # -- 路由
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path == "/":
            self._send_page(_PAGE)
            return
        if not self._auth():
            self._json({"error": "未授权"}, 401)
            return
        if path == "/api/state":
            data = _load()
            state = {
                "config": _redact(data),
                "cron": _cron_installed(),
                "llm_mode": "real" if (data.get("llm") or {}).get("api_key") else "mock",
                "artifacts": self._artifacts(),
                "runs": {k: v for k, v in _runs.items()},
            }
            self._json(state)
        elif path == "/api/artifacts":
            self._json(self._artifacts())
        elif path == "/api/file":
            self._serve_file(qs.get("path", [""])[0])
        elif path == "/api/log":
            name = qs.get("name", [""])[0]
            lp = (ROOT / "logs" / name).resolve()
            if not str(lp).startswith(str((ROOT / "logs").resolve())) or not lp.is_file():
                self._json({"error": "日志不存在"}, 404)
                return
            lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
            self._json({"log": "\n".join(lines)})
        else:
            self._json({"error": "not found"}, 404)

    def _artifacts(self) -> dict:
        def scan(sub, ext):
            d = DATA / sub
            if not d.exists():
                return []
            out = []
            for f in sorted(d.glob(f"*{ext}"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
                out.append({"name": f.name, "path": f"{sub}/{f.name}",
                            "size": f.stat().st_size,
                            "mtime": __import__("datetime").datetime.fromtimestamp(
                                f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
            return out
        return {"reports": scan("reports", ".html"), "traces": scan("traces", ".*"),
                "emails": scan("emails", ".eml")}

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        if path == "/api/login":
            body = self._body()
            cfg = _load().get("dashboard", {})
            if str(body.get("password", "")) == str(cfg.get("password") or ""):
                tok = secrets.token_hex(16)
                _tokens.add(tok)
                self.send_response(200)
                self.send_header("Set-Cookie", f"dsdash={tok}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self._json({"error": "密码错误"}, 401)
            return
        if not self._auth():
            self._json({"error": "未授权"}, 401)
            return
        if path == "/api/save":
            body = self._body()
            section = body.get("section", "")
            data = body.get("data", {})
            prev = _load()
            if section in ("smtp", "llm", "search", "runtime", "schedule", "dashboard"):
                merged = _restore_secrets(prev, {section: data})
                prev[section] = merged[section]
                _save(prev)
                self._json({"ok": True, "config": _redact(_load())})
            else:
                self._json({"error": f"未知配置段 {section}"}, 400)
        elif path == "/api/sources":
            body = self._body()
            kind, action, item = body.get("kind"), body.get("action"), body.get("item", {})
            data = _load()
            s = data.setdefault("search", {})
            if kind == "rss":
                lst = s.setdefault("rss_feeds", [])
            elif kind == "site":
                lst = s.setdefault("sites", [])
            elif kind == "keyword":
                lst = s.setdefault("keywords", [])
            elif kind == "wechat_manual":
                lst = s.setdefault("wechat", {}).setdefault("manual_urls", [])
            else:
                self._json({"error": f"未知渠道类型 {kind}"}, 400)
                return
            if action == "add":
                if item not in lst:
                    lst.append(item)
            elif action == "remove":
                if item in lst:
                    lst.remove(item)
                else:
                    lst[:] = [x for x in lst if not (isinstance(x, dict) and x.get("url") == item.get("url"))]
            else:
                self._json({"error": "action 必须为 add/remove"}, 400)
                return
            _save(data)
            self._json({"ok": True, "config": _redact(_load())})
        elif path == "/api/cron":
            body = self._body()
            msg = _cron_apply(_load(), body.get("action", "install"))
            self._json({"ok": "错误" not in msg, "msg": msg})
        elif path == "/api/run":
            body = self._body()
            run_id = _start_run(bool(body.get("mock")))
            self._json({"ok": True, "run_id": run_id, "log": f"manual-{run_id}.log"})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def main():
    cfg = load_config(CONFIG_PATH).data.get("dashboard", {})
    if not cfg.get("enabled", True):
        print("面板已禁用（config.yaml dashboard.enabled=false）")
        return
    host, port = str(cfg.get("host", "127.0.0.1")), int(cfg.get("port", 8787))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"✅ 管理面板已启动: http://{host}:{port}")
    print(f"   配置: {CONFIG_PATH} | 产物: {DATA}")
    if cfg.get("password"):
        print("   已启用密码登录")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>日报 Agent 管理面板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#f0f2f5;color:#1a202c;font-size:14px}
.side{position:fixed;left:0;top:0;bottom:0;width:180px;background:linear-gradient(180deg,#1a365d,#274b79);color:#fff;padding:20px 0}
.side h1{font-size:15px;padding:0 16px 16px;letter-spacing:1px}
.side button{display:block;width:100%;text-align:left;background:none;border:none;color:#cbd5e1;padding:10px 16px;cursor:pointer;font-size:14px}
.side button:hover,.side button.active{background:rgba(255,255,255,.12);color:#fff}
.main{margin-left:180px;padding:20px;max-width:900px}
.card{background:#fff;border-radius:10px;padding:18px 20px;margin-bottom:14px;box-shadow:0 1px 3px rgba(16,32,64,.08)}
.card h3{font-size:15px;margin-bottom:12px;color:#1a365d}
label{display:block;font-size:12px;color:#64748b;margin:8px 0 4px}
input,select{width:100%;border:1px solid #d8dde6;border-radius:8px;padding:7px 10px;font-size:14px;margin-bottom:4px}
.row{display:flex;gap:10px}.row>div{flex:1}
.btn{background:#1a365d;color:#fff;border:none;border-radius:8px;padding:8px 18px;font-size:14px;cursor:pointer;margin-top:8px}
.btn.ghost{background:#fff;color:#1a365d;border:1px solid #1a365d}
.btn.danger{background:#dc2626}
.btn:disabled{opacity:.5;cursor:not-allowed}
.hint{font-size:12px;color:#8492a6;margin-top:6px}
.item{display:flex;justify-content:space-between;align-items:center;border:1px solid #e2e8f0;border-radius:8px;padding:8px 12px;margin:6px 0;font-size:13px}
details.card summary{cursor:pointer;font-size:15px;color:#1a365d;margin-bottom:10px;list-style:none}
details.card summary::before{content:'▸ ';color:#1a365d}
details.card[open] summary::before{content:'▾ '}
.item .del{color:#dc2626;cursor:pointer;background:none;border:none;font-size:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eef2f7}
th{color:#64748b;font-weight:600}
a{color:#1a365d}
.ok{color:#16a34a}.bad{color:#dc2626}
pre{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:12px;font-size:12px;max-height:400px;overflow:auto;white-space:pre-wrap}
#toast{position:fixed;right:20px;top:20px;background:#1a365d;color:#fff;padding:10px 18px;border-radius:8px;display:none;z-index:9}
</style>
</head>
<body>
<div class="side">
  <h1>日报 Agent 面板</h1>
  <button data-tab="overview" class="active">概览</button>
  <button data-tab="smtp">邮件设置</button>
  <button data-tab="llm">LLM 设置</button>
  <button data-tab="sources">数据源渠道</button>
  <button data-tab="schedule">定时</button>
  <button data-tab="artifacts">产物</button>
</div>
<div class="main">
  <div id="tab-overview" class="tab"></div>
  <div id="tab-smtp" class="tab" style="display:none"></div>
  <div id="tab-llm" class="tab" style="display:none"></div>
  <div id="tab-sources" class="tab" style="display:none"></div>
  <div id="tab-schedule" class="tab" style="display:none"></div>
  <div id="tab-artifacts" class="tab" style="display:none"></div>
</div>
<div id="toast"></div>
<script>
let STATE=null;
const $=s=>document.querySelector(s);
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2500)}
async function api(path,opts){const r=await fetch(path,opts);const j=await r.json();if(!r.ok&&r.status===401){location.reload();throw new Error('未授权')}if(j.error)throw new Error(j.error);return j}
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function field(label,key,val,ph=''){return `<label>${label}</label><input data-k="${key}" value="${esc(val)}" placeholder="${esc(ph)}">`}
function tab(name){document.querySelectorAll('.tab').forEach(t=>t.style.display='none');$('#tab-'+name).style.display='';document.querySelectorAll('.side button').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));render(name)}
async function render(name){
  if(!STATE)STATE=await api('/api/state');
  const c=STATE.config;
  if(name==='overview'){
    $('#tab-overview').innerHTML=`
      <div class="card"><h3>系统状态</h3>
        <table><tr><td>LLM 模式</td><td><b class="${STATE.llm_mode==='real'?'ok':'bad'}">${STATE.llm_mode==='real'?'真实 API':'mock（未配 llm.api_key）'}</b></td></tr>
        <tr><td>定时任务</td><td>${STATE.cron?'<b class="ok">已安装</b>':'<b class="bad">未安装</b>'}（${esc(c.schedule.cron)}）</td></tr>
        <tr><td>发件邮箱</td><td>${esc(c.smtp.user||'未配置')}（${c.smtp.dry_run?'dry-run':'真实发送'}）</td></tr>
        <tr><td>收件人</td><td>${esc((c.smtp.to||[]).join(', ')||'未配置')}</td></tr>
        <tr><td>数据源</td><td>RSS ${(c.search.rss_feeds||[]).length} · 站点 ${(c.search.sites||[]).length} · 关键词 ${(c.search.keywords||[]).length} · 新闻引擎 ${(c.search.news_rss_search.engines||[]).join('/')}</td></tr></table>
        <h3 style="margin-top:14px">手动运行（测试）</h3>
        <div class="row"><div><label>模式</label><select id="run-mode"><option value="real">真实 LLM</option><option value="mock">Mock（不耗 API）</option></select></div><div style="display:flex;align-items:flex-end"><button class="btn" id="run-btn">立即运行一次</button></div></div>
        <div id="run-out" style="margin-top:10px"></div>
      </div>`;
    $('#run-btn').onclick=async()=>{const b=$('#run-btn');b.disabled=true;b.textContent='运行中…';try{const r=await api('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mock:$('#run-mode').value==='mock'})});$('#run-out').innerHTML=`<div class="hint">已启动（run_id=${esc(r.run_id)}），输出见下方日志；<button class="btn ghost" onclick="pollLog('${esc(r.log)}')">刷新日志</button></div><pre id="run-log">（点击刷新查看）</pre>`;}catch(e){toast(e.message)}finally{b.disabled=false;b.textContent='立即运行一次'}};
  }
  if(name==='smtp'){
    const s=c.smtp;
    $('#tab-smtp').innerHTML=`<div class="card"><h3>邮件设置</h3>
      <div class="row"><div>${field('SMTP 服务器','host',s.host)}</div><div>${field('端口(SSL)','port',s.port)}</div></div>
      ${field('发件邮箱账号','user',s.user)}
      ${field('SMTP 授权码（留空不修改）','password',s.password)}
      ${field('发件人名称','from_name',s.from_name)}
      <label>收件人（每行一个）</label><textarea data-k="to" rows="3" style="width:100%;border:1px solid #d8dde6;border-radius:8px;padding:7px">${esc((s.to||[]).join('\\n'))}</textarea>
      <div class="row"><div><label>dry_run（仅生成 .eml）</label><select data-k="dry_run"><option value="true" ${s.dry_run?'selected':''}>是</option><option value="false" ${!s.dry_run?'selected':''}>否（真实发送）</option></select></div>
      <div><label>附带日报 html 附件</label><select data-k="attach_report_html"><option value="true" ${s.attach_report_html?'selected':''}>是</option><option value="false" ${!s.attach_report_html?'selected':''}>否</option></select></div>
      <div><label>附带 trace html 附件</label><select data-k="attach_trace_html"><option value="true" ${s.attach_trace_html!==false?'selected':''}>是</option><option value="false" ${s.attach_trace_html===false?'selected':''}>否</option></select></div></div>
      <button class="btn" onclick="saveSection('smtp')">保存邮件设置</button></div>`;
  }
  if(name==='llm'){
    const l=c.llm;
    $('#tab-llm').innerHTML=`<div class="card"><h3>LLM 设置（OpenAI 兼容 API）</h3>
      ${field('Base URL','base_url',l.base_url)}
      <div class="row"><div>${field('Model','model',l.model)}</div><div>${field('Temperature','temperature',l.temperature)}</div></div>
      ${field('API Key（留空不修改）','api_key',l.api_key)}
      <div class="hint">未配置 key 时系统以 mock 模式运行（规则引擎，仅测试用）</div>
      <button class="btn" onclick="saveSection('llm')">保存 LLM 设置</button></div>`;
  }
  if(name==='schedule'){
    const sc=c.schedule;
    const parts=(sc.cron||'0 8 * * *').trim().split(/\s+/);
    const curMin=String(parts[0]||'0').padStart(2,'0'), curHour=String(parts[1]||'8').padStart(2,'0'), curDow=parts[4]||'*';
    const freq=curDow==='1-5'?'weekday':'daily';
    const hours=Array.from({length:24},(_,i)=>String(i).padStart(2,'0'));
    const mins=Array.from({length:60},(_,i)=>String(i).padStart(2,'0'));
    $('#tab-schedule').innerHTML=`<div class="card"><h3>定时设置</h3>
      <label>执行频率</label>
      <select id="sc-freq">
        <option value="daily" ${freq==='daily'?'selected':''}>每天</option>
        <option value="weekday" ${freq==='weekday'?'selected':''}>工作日（周一 ~ 周五）</option>
      </select>
      <div class="row">
        <div><label>小时（24 小时制）</label><select id="sc-hour">${hours.map(h=>`<option ${h===curHour?'selected':''}>${h}</option>`).join('')}</select></div>
        <div><label>分钟</label><select id="sc-min">${mins.map(m=>`<option ${m===curMin?'selected':''}>${m}</option>`).join('')}</select></div>
      </div>
      <div class="hint">将执行于：<b><span id="sc-preview"></span></b>（自动生成 cron）</div>
      <button class="btn" onclick="saveSchedule()">保存时间</button>
      <button class="btn ghost" onclick="cronAct('install')">安装到 crontab</button>
      <button class="btn ghost danger" onclick="cronAct('remove')">移除 crontab</button>
      <div class="hint" id="cron-msg"></div></div>`;
    scPreview();
    ['sc-hour','sc-min','sc-freq'].forEach(id=>document.getElementById(id).addEventListener('change',scPreview));
  }

  if(name==='sources'){
    const s=c.search;
    $('#tab-sources').innerHTML=`
      <details class="card" open><summary>RSS 源（${s.rss_feeds.length}）</summary>
        <div id="rss-list">${s.rss_feeds.map(f=>`<div class="item"><span>${esc(f.name)} — <a href="${esc(f.url)}" target="_blank">${esc(f.url)}</a></span><button class="del" onclick="rmSource('rss',${JSON.stringify(f).replace(/"/g,'&quot;')})">✕</button></div>`).join('')||'<div class="hint">暂无</div>'}</div>
        <div class="row"><div>${field('名称','', '')}</div><div>${field('RSS URL','','')}</div></div>
        <button class="btn" onclick="addRss()">添加 RSS 源</button></details>
      <details class="card"><summary>行业站点直抓（${s.sites.length}）</summary>
        <div id="site-list">${s.sites.map(f=>`<div class="item"><span>${esc(f.name)} — ${esc(f.list_url)} <span class="hint">regex: ${esc(f.link_regex||'')}</span></span><button class="del" onclick="rmSource('site',${JSON.stringify(f).replace(/"/g,'&quot;')})">✕</button></div>`).join('')||'<div class="hint">暂无</div>'}</div>
        <div class="row"><div>${field('站点名','','')}</div><div>${field('列表页 URL','','')}</div><div>${field('链接正则(可选)','','')}</div></div>
        <button class="btn" onclick="addSite()">添加站点</button></details>
      <details class="card"><summary>微信公众号（搜狗搜索默认开；辅助路径）</summary>
        <label>人工文章 URL 清单（每行一个）</label><textarea id="wx-manual" rows="3" style="width:100%;border:1px solid #d8dde6;border-radius:8px;padding:7px">${esc((s.wechat.manual_urls||[]).join('\\n'))}</textarea>
        <label>wechat-download-api JSON 接口（可选）</label><input id="wx-api" value="${esc(s.wechat.api_url||'')}" style="width:100%">
        <button class="btn" onclick="saveWechat()">保存公众号配置</button></details>
      <details class="card" open><summary>检索关键词（${s.keywords.length}）</summary>
        <textarea id="kw" rows="8" style="width:100%;border:1px solid #d8dde6;border-radius:8px;padding:7px">${esc(s.keywords.join('\\n'))}</textarea>
        <button class="btn" onclick="saveKeywords()">保存关键词</button></details>
      <details class="card"><summary>新闻搜索引擎</summary>
        <label>启用（google 为真实 RSS）</label><select id="news-enabled"><option value="true" ${s.news_rss_search.enabled?'selected':''}>启用</option><option value="false" ${!s.news_rss_search.enabled?'selected':''}>禁用</option></select>
        <button class="btn" onclick="saveNews()">保存新闻搜索设置</button></details>`;
  }
  if(name==='artifacts'){
    const a=STATE.artifacts;
    const rows=(list,path)=>(list||[]).map(f=>`<tr><td><a href="/api/file?path=${encodeURIComponent(f.path)}" target="_blank">${esc(f.name)}</a></td><td>${esc(f.mtime)}</td><td>${(f.size/1024).toFixed(1)}KB</td></tr>`).join('')||'<tr><td colspan=3 class="hint">暂无</td></tr>';
    $('#tab-artifacts').innerHTML=`
      <div class="card"><h3>日报（${a.reports.length}）</h3><table>${rows(a.reports)}</table></div>
      <div class="card"><h3>执行日志（${a.traces.length}）</h3><table>${rows(a.traces)}</table></div>
      <div class="card"><h3>邮件草稿（${a.emails.length}）</h3><table>${rows(a.emails)}</table></div>`;
  }
}
async function saveSection(section){
  const el=$('#tab-'+section);const data={};
  el.querySelectorAll('[data-k]').forEach(i=>{const v=i.value.trim();data[i.dataset.k]=i.dataset.k==='to'?(v?[v]:[]):(i.dataset.k==='port'||i.dataset.k==='temperature'?Number(v)||v:((i.tagName==='SELECT'&&(v==='true'||v==='false'))?v==='true':v))});
  try{await api('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({section,data})});STATE=await api('/api/state');toast('已保存');render(section)}catch(e){toast(e.message)}
}
async function cronAct(action){try{const r=await api('/api/cron',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});$('#cron-msg').textContent=r.msg;STATE=await api('/api/state');toast(r.msg)}catch(e){toast(e.message)}}
function scPreview(){
  const h=$('#sc-hour').value, m=$('#sc-min').value, f=$('#sc-freq').value;
  $('#sc-preview').textContent=`${m} ${h} * * ${f==='weekday'?'1-5':'*'}`;
}
async function saveSchedule(){
  const h=$('#sc-hour').value, m=$('#sc-min').value, f=$('#sc-freq').value;
  const cron=`${m} ${h} * * ${f==='weekday'?'1-5':'*'}`;
  try{
    await api('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({section:'schedule',data:{cron,note:'由面板生成'}})});
    STATE=await api('/api/state');toast('已保存：每天 '+h+':'+m+(f==='weekday'?'（工作日）':''));
  }catch(e){toast(e.message)}
}
async function rmSource(kind,item){try{await api('/api/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,action:'remove',item})});STATE=await api('/api/state');render('sources');toast('已删除')}catch(e){toast(e.message)}}
async function addRss(){const t=$('#tab-sources');const inputs=t.querySelectorAll('#tab-sources input');const vals=[...t.querySelectorAll('.card:first-of-type input')].map(i=>i.value.trim());if(!vals[0]||!vals[1])return toast('请填写名称与 URL');await api('/api/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'rss',action:'add',item:{name:vals[0],url:vals[1]}})});STATE=await api('/api/state');render('sources');toast('已添加')}
async function addSite(){const t=$('#tab-sources');const cards=t.querySelectorAll('.card');const inputs=[...cards[1].querySelectorAll('input')].map(i=>i.value.trim());if(!inputs[0]||!inputs[1])return toast('请填写站点名与 URL');await api('/api/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'site',action:'add',item:{name:inputs[0],list_url:inputs[1],link_regex:inputs[2]||''}})});STATE=await api('/api/state');render('sources');toast('已添加')}
async function saveWechat(){const t=$('#tab-sources');const manual=t.querySelector('#wx-manual').value.split('\\n').map(s=>s.trim()).filter(Boolean);const apiUrl=t.querySelector('#wx-api').value.trim();try{const cfg=STATE.config.search;cfg.wechat.manual_urls=manual;cfg.wechat.api_url=apiUrl;await api('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({section:'search',data:cfg})});STATE=await api('/api/state');toast('已保存')}catch(e){toast(e.message)}}
async function saveKeywords(){const kws=$('#kw').value.split('\\n').map(s=>s.trim()).filter(Boolean);try{const cfg=STATE.config.search;cfg.keywords=kws;await api('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({section:'search',data:cfg})});STATE=await api('/api/state');toast('已保存')}catch(e){toast(e.message)}}
async function saveNews(){const en=$('#news-enabled').value==='true';try{const cfg=STATE.config.search;cfg.news_rss_search.enabled=en;await api('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({section:'search',data:cfg})});STATE=await api('/api/state');toast('已保存')}catch(e){toast(e.message)}}
async function pollLog(name){const r=await api('/api/log?name='+encodeURIComponent(name));const el=$('#run-log');if(el)el.textContent=r.log.slice(-8000)}
document.querySelectorAll('.side button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
(async()=>{try{await api('/api/state');tab('overview')}catch(e){document.body.innerHTML=`<div style="padding:60px;text-align:center"><h2>需要登录</h2><input id="pw" type="password" placeholder="面板密码" style="width:240px;margin:20px auto;display:block"><button class="btn" onclick="login()">登录</button></div>`;window.login=async()=>{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});if(r.ok)location.reload();else alert('密码错误')}}})();
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
