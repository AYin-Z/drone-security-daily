"""HTML 日报渲染 —— 严格遵循需求样式规范：
深色主色 #1a365d / 背景 #f0f2f5 / 白色卡片 / 卡片间距 14px / 圆角 10px /
7 类彩色分类标签 / 纯 JS 分类筛选 / 响应式 / 无任何外部 CDN（CSS/JS 全内联）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# 7 类标签色系（淡底 + 深字）
CATEGORY_STYLES = {
    "探测感知": ("#EBF4FF", "#1E40AF"),
    "干扰反制": ("#FEF2F2", "#991B1B"),
    "实战案例": ("#FEF3C7", "#92400E"),
    "政策法规": ("#ECFDF5", "#065F46"),
    "技术前沿": ("#F3E8FF", "#6B21A8"),
    "行业动态": ("#FFF7ED", "#9A3412"),
    "国际视野": ("#EFF6FF", "#1E3A5F"),
}


def weekday_cn(dt: datetime) -> str:
    return "一二三四五六日"[dt.weekday()]


def date_cn(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日 星期{weekday_cn(dt)}"


def fmt_published(pub: Optional[str], now: datetime) -> str:
    """来源时间显示：当天显示 MM-DD HH:MM，无日期显示「近期」。"""
    if not pub:
        return "近期"
    try:
        dt = datetime.fromisoformat(pub)
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return "近期"


def render_daily(
    articles: list[dict],
    now: datetime,
    generated_at: str,
    focus: Optional[dict] = None,
    email_notice: bool = False,
) -> str:
    """articles: [{title,url,source,published,tags,summary}]；focus: 今日焦点文章（可为 None）。
    email_notice=True（邮件正文场景）：追加提示条——邮件客户端不支持脚本，分类按钮需浏览器打开 HTML 文件。"""
    n = len(articles)
    cats = sorted({t for a in articles for t in a["tags"]})
    m = len(cats)
    date_line = date_cn(now)

    notice_html = ""
    if email_notice:
        notice_html = (
            '<div style="background:#FEF3C7;border:1px solid #F59E0B;border-radius:10px;'
            'padding:10px 16px;font-size:13px;color:#92400E;margin:14px 0;">'
            '📌 邮件客户端不支持脚本，分类筛选按钮在此处无法使用；'
            '如需完整交互版（可点击筛选），请打开附件中的 HTML 文件或访问 data/reports/ 目录。</div>'
        )

    focus_html = ""
    if focus:
        focus_html = _card_html(focus, focus_flag=True)
        articles = [a for a in articles if a is not focus]   # 焦点文章不重复出现在列表

    cards = "\n".join(_card_html(a) for a in articles)
    tag_buttons = "\n".join(
        f'<button class="tag-btn" data-cat="{_esc(c)}">{_esc(c)}</button>' for c in cats
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>无人机感知与反制技术日报 {date_line}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: #f0f2f5; color: #1a202c;
    font-size: 15px; line-height: 1.65;
  }}
  .header {{
    background: linear-gradient(135deg, #1a365d 0%, #2b4c7e 100%);
    color: #fff; padding: 34px 20px 30px; text-align: center;
  }}
  .header h1 {{ font-size: 22px; letter-spacing: 2px; }}
  .header .sub {{ font-size: 15px; opacity: .85; margin-top: 8px; }}
  .header .stats {{
    display: inline-block; margin-top: 14px; padding: 6px 18px;
    background: rgba(255,255,255,.12); border-radius: 20px; font-size: 14px;
  }}
  .container {{ max-width: 860px; margin: 0 auto; padding: 14px 14px 40px; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 6px; }}
  .tag-btn {{
    border: 1px solid #d8dde6; background: #fff; color: #42526b;
    border-radius: 16px; padding: 5px 14px; font-size: 13px; cursor: pointer;
    transition: all .15s; font-family: inherit;
  }}
  .tag-btn:hover {{ border-color: #1a365d; color: #1a365d; }}
  .tag-btn.active {{ background: #1a365d; border-color: #1a365d; color: #fff; }}
  .card {{
    background: #fff; border-radius: 10px; padding: 18px 20px;
    margin: 14px 0; box-shadow: 0 1px 3px rgba(16,32,64,.08);
  }}
  .card.focus {{
    border: 1px solid #1a365d; box-shadow: 0 4px 14px rgba(26,54,93,.18);
    padding: 24px 26px; margin: 18px 0;
  }}
  .card.focus h3 {{ font-size: 20px; }}
  .card-head {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .focus-badge {{
    background: #1a365d; color: #fff; font-size: 12px; padding: 2px 10px;
    border-radius: 12px; letter-spacing: 1px;
  }}
  .cat-tag {{
    display: inline-block; font-size: 12px; padding: 2px 10px;
    border-radius: 12px; margin: 2px 4px 2px 0;
  }}
  .card h3 {{ font-size: 18px; margin: 8px 0 6px; overflow-wrap: break-word; word-break: break-word; }}
  .card h3 a {{ color: #1a365d; text-decoration: none; }}
  .card h3 a:hover {{ text-decoration: underline; }}
  .meta {{ color: #8492a6; font-size: 13px; }}
  .summary {{ margin-top: 8px; color: #3d4a5d; overflow-wrap: break-word; word-break: break-word; }}
  .readmore {{
    display: inline-block; margin-top: 10px; padding: 5px 16px;
    border: 1px solid #1a365d; color: #1a365d; border-radius: 16px;
    font-size: 13px; text-decoration: none; transition: all .15s;
  }}
  .readmore:hover {{ background: #1a365d; color: #fff; }}
  .empty {{ text-align:center; color:#8492a6; padding: 40px 0; display:none; }}
  .footer {{
    max-width: 860px; margin: 10px auto 0; padding: 18px 14px 30px;
    color: #8492a6; font-size: 12px; text-align: center;
  }}
  @media (max-width: 600px) {{
    body {{ font-size: 14px; }}
    .header h1 {{ font-size: 19px; }}
    .card {{ padding: 14px 14px; }}
    .card h3 {{ font-size: 16px; }}
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>无人机感知与反制技术日报</h1>
    <div class="sub">{date_line}</div>
    <div class="stats">今日收录 {n} 篇 · 覆盖 {m} 个技术方向</div>
  </div>
  {notice_html}
  <div class="container">
    <div class="filters">
      <button class="tag-btn active" data-cat="全部">全部</button>
      {tag_buttons}
    </div>
    {focus_html}
    <div id="cards">{cards}</div>
    <div class="empty" id="empty">当前分类暂无文章</div>
  </div>
  <div class="footer">
    本页内容来自公开网络，仅作技术资讯参考，版权归原作者所有<br>
    由 AI 自动整理生成于 {generated_at}
  </div>
<script>
(function(){{
  var btns = document.querySelectorAll('.tag-btn');
  var cards = document.querySelectorAll('.card');
  btns.forEach(function(btn){{
    btn.addEventListener('click', function(){{
      btns.forEach(function(b){{ b.classList.remove('active'); }});
      btn.classList.add('active');
      var cat = btn.getAttribute('data-cat');
      var visible = 0;
      cards.forEach(function(card){{
        var show = (cat === '全部') ||
                   card.getAttribute('data-cats').indexOf(cat) !== -1;
        card.style.display = show ? '' : 'none';
        if (show) visible++;
      }});
      document.getElementById('empty').style.display = visible ? 'none' : '';
    }});
  }});
}})();
</script>
</body>
</html>"""


def _card_html(a: dict, focus_flag: bool = False) -> str:
    cats = a.get("tags", [])
    tags_html = "".join(
        f'<span class="cat-tag" style="background:{_esc(CATEGORY_STYLES.get(c, ("#E2E8F0", "#334155"))[0])};'
        f'color:{_esc(CATEGORY_STYLES.get(c, ("#E2E8F0", "#334155"))[1])}">{_esc(c)}</span>'
        for c in cats
    )
    badge = '<span class="focus-badge">今日焦点</span>' if focus_flag else ""
    cls = "card focus" if focus_flag else "card"
    return f"""<div class="{cls}" data-cats="{_esc(','.join(cats))}">
  <div class="card-head">{badge}{tags_html}</div>
  <h3><a href="{_esc(a['url'])}" target="_blank" rel="noopener noreferrer">{_esc(a['title'])}</a></h3>
  <div class="meta">{_esc(a['source'])} · {_esc(a.get('published_display', '近期'))}</div>
  <div class="summary">{_esc(a.get('summary', ''))}</div>
  <a class="readmore" href="{_esc(a['url'])}" target="_blank" rel="noopener noreferrer">阅读原文</a>
</div>"""


def _esc(s) -> str:
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))
