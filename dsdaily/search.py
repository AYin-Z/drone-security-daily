"""检索层：RSS 聚合 / 行业站点直抓 / 新闻搜索（Bing/Google News RSS）/ Tavily API / 微信公众号。

每个数据源独立 try/except：单源失败不中断整体流程，错误进入 trace。
微信公众号三条路径：
  1) manual_urls：人工维护文章 URL 清单（文章页可直接抓正文）
  2) auto_from_search：搜索结果中的 mp.weixin.qq.com 链接自动作为公众号文章抓取
  3) api_url：对接 wechat-download-api 等工具的通用 JSON 接口（GET ?keyword=&days=2）
"""
from __future__ import annotations

import hashlib
import json
import re
import time as _t
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .tracelog import TraceLog

USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

NEWS_ENGINES = {
    "bing": "https://www.bing.com/news/search?q={q}&format=rss",
    "google": "https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
}


@dataclass
class Article:
    title: str
    url: str
    source: str
    published: Optional[str] = None      # ISO 字符串，可为空（未知日期）
    excerpt: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def url_key(self) -> str:
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()


class SearchLayer:
    def __init__(self, cfg: dict, trace: TraceLog):
        self.cfg = cfg
        self.trace = trace

    # ------------------------------------------------------------------ 入口
    def collect(self, keywords: list[str]) -> list[Article]:
        """跑所有已启用数据源，返回候选文章（不筛时间）。keywords 为当日检索词。"""
        articles: list[Article] = []
        kw = keywords or self.cfg.get("keywords", [])

        # 1) RSS 聚合
        for feed in self.cfg.get("rss_feeds", []):
            name, url = feed.get("name", "?"), feed.get("url", "")
            if not url:
                continue
            ua = feed.get("ua", "")
            got = self._step("rss_fetch", name, lambda u=url, _ua=ua: self._fetch_rss(u, _ua))
            articles.extend(got)
        # 2) 行业站点直抓
        for site in self.cfg.get("sites", []):
            name = site.get("name", "?")
            got = self._step("site_fetch", name, lambda s=site: self._fetch_site(s))
            articles.extend(got)
        # 3) 新闻搜索（Bing/Google News RSS，免费，覆盖新闻媒体/政务资讯转载）
        news_cfg = self.cfg.get("news_rss_search", {})
        if news_cfg.get("enabled", True):
            top = int(news_cfg.get("keywords_top", 6))
            for eng in news_cfg.get("engines", ["bing"]):
                label = "Bing新闻" if eng == "bing" else "Google新闻"
                for q in kw[:top]:
                    got = self._step(f"news_search_{eng}", f"{label}:{q}",
                                     lambda e=eng, query=q: self._news_rss_search(e, query, label))
                    articles.extend(got)
        # 4) Tavily 搜索（可选）
        tavily_key = (self.cfg.get("tavily_api_key") or "").strip()
        if tavily_key:
            for q in kw[:5]:
                got = self._step("tavily_search", q, lambda query=q: self._tavily_search(tavily_key, query))
                articles.extend(got)
        else:
            self.trace.step(stage="search", action="skip", tool="tavily_search",
                            input_summary="未配置 tavily_api_key", output_summary="跳过")
        # 4b) CSDN 技术文章搜索（免登录 JSON API，技术社区源）
        csdn_cfg = self.cfg.get("csdn", {})
        if csdn_cfg.get("enabled", True):
            for q in kw[: int(csdn_cfg.get("keywords_top", 3))]:
                got = self._step("csdn_search", q, lambda query=q: self._csdn_search(query))
                articles.extend(got)
        # 4c) 博查 Bocha 中文 AI 搜索（可选，需 api_key；freshness=oneDay 适配日报）
        bocha_cfg = self.cfg.get("bocha", {})
        bocha_key = (bocha_cfg.get("api_key") or "").strip()
        if bocha_cfg.get("enabled") and bocha_key:
            for q in kw[: int(bocha_cfg.get("keywords_top", 3))]:
                got = self._step("bocha_search", q, lambda query=q: self._bocha_search(bocha_key, query))
                articles.extend(got)
        # 4d) 百度/360 搜索页（可选，默认关：反爬波动大；开启后结果中的微信链接会被自动提取）
        baidu_cfg = self.cfg.get("baidu", {})
        if baidu_cfg.get("enabled"):
            for q in kw[: int(baidu_cfg.get("keywords_top", 3))]:
                got = self._step("baidu_search", q, lambda query=q: self._baidu_search(query))
                articles.extend(got)
        qihu_cfg = self.cfg.get("qihu360", {})
        if qihu_cfg.get("enabled"):
            for q in kw[: int(qihu_cfg.get("keywords_top", 3))]:
                got = self._step("qihu360_search", q, lambda query=q: self._qihu360_search(query))
                articles.extend(got)
        # 5) 微信公众号
        wechat_cfg = self.cfg.get("wechat", {})
        for url in wechat_cfg.get("manual_urls", []):
            got = self._step("wechat_fetch", url, lambda u=url: self._fetch_wechat_article(u))
            articles.extend(got)
        api_url = (wechat_cfg.get("api_url") or "").strip()
        if api_url:
            for q in kw[:5]:
                got = self._step("wechat_api", q,
                                 lambda query=q: self._wechat_api(api_url, query, wechat_cfg.get("api_key", "")))
                articles.extend(got)
        # 搜狗微信搜索（微信公众号核心检索路径，替代原腾讯 WorkBuddy 生态能力）
        sogou_cfg = wechat_cfg.get("sogou", {})
        if sogou_cfg.get("enabled", True):
            got = self._step("sogou_wechat", f"{sogou_cfg.get('keywords_top', 6)} 词",
                             lambda: self._sogou_search_all(kw, wechat_cfg, sogou_cfg))
            articles.extend(got)
        # 6) 自动提取搜索结果中的公众号文章（auto_from_search）
        if wechat_cfg.get("auto_from_search", True):
            articles = self._extract_weixin(articles)
        return articles

    def _step(self, tool: str, label: str, fn) -> list[Article]:
        t0 = _t.time()
        try:
            got = fn()
            self.trace.step(stage="search", action="tool_call", tool=tool,
                            input_summary=label, output_summary=f"获得 {len(got)} 条",
                            duration_ms=(_t.time() - t0) * 1000, status="ok")
            return got
        except Exception as e:  # noqa: BLE001
            self.trace.step(stage="search", action="tool_call", tool=tool,
                            input_summary=label, output_summary="失败",
                            duration_ms=(_t.time() - t0) * 1000, status="error",
                            error={"type": type(e).__name__, "msg": str(e)[:200]})
            return []

    # ------------------------------------------------------------------ 数据源
    def _fetch_rss(self, url: str, ua: str = "") -> list[Article]:
        if ua == "googlebot":
            ua_str = ("Mozilla/5.0 (compatible; Googlebot/2.1; "
                      "+http://www.google.com/bot.html)")
        else:
            ua_str = USER_AGENT
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": ua_str}) as client:
            resp = client.get(url)
            resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        out = []
        for entry in feed.entries[:30]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            out.append(Article(
                title=title, url=link, source=feed.feed.get("title") or url,
                published=_fmt_struct_time(entry.get("published_parsed")),
                excerpt=_clean_html(entry.get("summary") or entry.get("description") or "")[:500],
            ))
        return out

    def _news_rss_search(self, engine: str, query: str, label: str) -> list[Article]:
        url = NEWS_ENGINES[engine].format(q=quote(query))
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(url)
            resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        out = []
        for entry in feed.entries[:12]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            out.append(Article(
                title=title, url=link, source=label,
                published=_fmt_struct_time(entry.get("published_parsed")),
                excerpt=_clean_html(entry.get("summary") or entry.get("description") or "")[:500],
            ))
        return out

    def _fetch_site(self, site: dict) -> list[Article]:
        url = site["list_url"]
        regex = site.get("link_regex", "")
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(url)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        out = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            title = (a.get_text() or "").strip()
            if len(title) < 8 or len(title) > 120:
                continue
            href = urljoin(url, href)          # 先解析相对链接再匹配正则
            if regex and not re.search(regex, href):
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append(Article(title=title, url=href, source=site.get("name", "站点"),
                               published=None, excerpt=""))
        return out[:30]

    def _tavily_search(self, api_key: str, query: str) -> list[Article]:
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": int(self.cfg.get("tavily_max_results", 10)),
            "topic": "news",
            "days": max(1, int(self.cfg.get("window_hours", 24)) // 24),
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post("https://api.tavily.com/search", json=payload)
            resp.raise_for_status()
        data = resp.json()
        out = []
        for r in data.get("results", []):
            out.append(Article(
                title=(r.get("title") or "").strip(),
                url=(r.get("url") or "").strip(),
                source="Tavily",
                published=r.get("published_date") or "",
                excerpt=(r.get("content") or "")[:500],
            ))
        return out

    def _csdn_search(self, query: str) -> list[Article]:
        """CSDN 搜索 API（免登录 JSON，技术社区源；tm=7 近 7 天）。"""
        params = {"q": query, "t": "blog", "p": 1, "s": 0, "tm": 7}
        with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get("https://so.csdn.net/api/v3/search", params=params)
            resp.raise_for_status()
        data = resp.json()
        vos = data.get("result_vos") or data.get("resultVos") or []
        out = []
        for v in vos:
            title = _clean_html(v.get("title") or "")
            url = (v.get("url") or "").strip()
            if not title or not url:
                continue
            out.append(Article(
                title=title, url=url, source="CSDN",
                published=None,
                excerpt=_clean_html(v.get("description") or "")[:500],
            ))
        return out[:15]

    def _bocha_search(self, api_key: str, query: str) -> list[Article]:
        """博查 BochaAI 中文 AI 搜索（https://open.bochaai.com，付费 API）。
        freshness=oneDay 仅返回当天内容，适配日报；响应为 Bing 兼容格式。"""
        payload = {
            "query": query,
            "freshness": "oneDay",
            "summary": True,
            "count": 10,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=30, headers=headers) as client:
            resp = client.post("https://api.bochaai.com/v1/web-search", json=payload)
            resp.raise_for_status()
        data = resp.json()
        out = []
        pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
        for p in pages:
            title = (p.get("name") or "").strip()
            url = (p.get("url") or "").strip()
            if not title or not url:
                continue
            out.append(Article(
                title=title, url=url, source="博查搜索",
                published=p.get("datePublished") or "",
                excerpt=(p.get("snippet") or "")[:500],
            ))
        return out[:10]

    def _baidu_search(self, query: str) -> list[Article]:
        """百度网页搜索（可选源）。真实 URL 在 mu=\"...\" 属性；命中安全验证/壳页时抛错降级。
        结果中的 mp.weixin.qq.com 链接会经 auto_from_search 自动转为公众号文章。"""
        url = "https://www.baidu.com/s?wd=" + quote(query) + "&rn=20"
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": _DESKTOP_UA}) as client:
            resp = client.get(url)
            resp.raise_for_status()
        page = resp.text
        if len(page) < 2000 or "安全验证" in page:
            raise RuntimeError("百度返回安全验证/壳页（反爬波动），本轮跳过")
        soup = BeautifulSoup(page, "html.parser")
        out = []
        for h3 in soup.find_all("h3"):
            a = h3.find("a")
            if not a:
                continue
            real = a.get("mu") or ""
            title = (a.get_text() or "").strip()
            if not real or not real.startswith("http") or len(title) < 8:
                continue
            out.append(Article(title=title, url=real, source="百度搜索",
                               published=None, excerpt=""))
        return out[:15]

    def _qihu360_search(self, query: str) -> list[Article]:
        """360 搜索（可选源）。真实 URL 在 data-mdurl 属性。"""
        url = "https://www.so.com/s?q=" + quote(query)
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": _DESKTOP_UA}) as client:
            resp = client.get(url)
            resp.raise_for_status()
        page = resp.text
        if len(page) < 2000 or "验证码" in page:
            raise RuntimeError("360 返回验证码/壳页，本轮跳过")
        soup = BeautifulSoup(page, "html.parser")
        out = []
        for h3 in soup.find_all("h3", class_="res-title"):
            a = h3.find("a")
            if not a:
                continue
            real = a.get("data-mdurl") or ""
            title = (a.get_text() or "").strip()
            if not real or not real.startswith("http") or len(title) < 8:
                continue
            out.append(Article(title=title, url=real, source="360搜索",
                               published=None, excerpt=""))
        return out[:15]

    # ------------------------------------------------------------------ 微信公众号
    def _extract_weixin(self, articles: list[Article]) -> list[Article]:
        """搜索结果中的 mp.weixin.qq.com 链接 → 抓正文，标记为微信公众号。"""
        out = []
        fetched = 0
        for a in articles:
            host = (urlparse(a.url).netloc or "").lower()
            if host.endswith("mp.weixin.qq.com") and not a.raw.get("from_weixin"):
                try:
                    we = self._fetch_wechat_article(a.url)[0]
                    we.raw["from_weixin"] = True
                    out.append(we)
                    fetched += 1
                    continue
                except Exception as e:  # noqa: BLE001
                    self.trace.step(stage="search", action="tool_call", tool="wechat_fetch",
                                    input_summary=a.url, output_summary="抓取失败",
                                    status="error",
                                    error={"type": type(e).__name__, "msg": str(e)[:200]})
            out.append(a)
        if fetched:
            self.trace.step(stage="search", action="process", tool="wechat_extract",
                            input_summary=f"扫描 {len(articles)} 条",
                            output_summary=f"提取公众号文章 {fetched} 条")
        return out

    def _fetch_wechat_article(self, url: str) -> list[Article]:
        with httpx.Client(follow_redirects=True, timeout=30,
                          headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(url)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        title = (soup.title.get_text(strip=True) if soup.title else "") or "微信公众号文章"
        para = soup.find("div", id="js_content") or soup.find("div", class_="rich_media_content") or soup
        text = (para.get_text(" ", strip=True) if para else "")[:500]
        return [Article(title=title, url=url, source="微信公众号", published=None, excerpt=text)]

    def _wechat_api(self, api_url: str, keyword: str, api_key: str = "") -> list[Article]:
        """对接 wechat-download-api 等工具的通用 JSON 接口。
        期望返回 JSON 数组（或 {data|articles|items: [...]}），每项含 title/url，可选 published/content。"""
        headers = {"User-Agent": USER_AGENT}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=30, headers=headers) as client:
            resp = client.get(api_url, params={"keyword": keyword, "days": 2})
            resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data") or data.get("articles") or data.get("items") or data.get("list") or []
        out = []
        for it in (data or []):
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            url = (it.get("url") or it.get("link") or "").strip()
            if not title or not url:
                continue
            out.append(Article(
                title=title, url=url, source="微信公众号",
                published=it.get("published") or it.get("publish_time") or "",
                excerpt=(it.get("content") or it.get("summary") or "")[:500],
            ))
        return out

    # ------------------------------------------------------------------ 搜狗微信搜索
    def _sogou_search_all(self, keywords: list[str], wechat_cfg: dict, sogou_cfg: dict) -> list[Article]:
        """搜狗微信搜索：关键词 → 列表页 → 解析(标题/账号/日期/摘要) → 解真实文章链接。
        低频使用（每日一次、查询间隔 delay_seconds），Cookie 命中验证码时记录错误并提示刷新。"""
        import time as _t
        top = int(sogou_cfg.get("keywords_top", 6))
        delay = float(sogou_cfg.get("delay_seconds", 3))
        max_arts = int(sogou_cfg.get("max_articles", 20))
        cookie = (sogou_cfg.get("cookie") or "").strip()

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            "Referer": "https://weixin.sogou.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if cookie:
            headers["Cookie"] = cookie
        out: list[Article] = []
        seen_urls: set[str] = set()
        with httpx.Client(follow_redirects=True, timeout=30, headers=headers) as client:
            for i, q in enumerate(keywords[:top]):
                if i > 0:
                    _t.sleep(delay)
                url = "https://weixin.sogou.com/weixin?type=2&query=" + quote(q)
                resp = client.get(url)
                resp.raise_for_status()
                page = resp.text
                if _is_sogou_captcha(page):
                    self.trace.step(stage="search", action="tool_call", tool="sogou_wechat",
                                    input_summary=q, output_summary="触发验证码/风控，跳过（见 README 刷新 Cookie）",
                                    status="error", error={"type": "sogou_captcha", "msg": "需刷新 sogou.cookie"})
                    continue
                results = _parse_sogou_results(page, q)
                if not results:
                    self.trace.step(stage="search", action="tool_call", tool="sogou_wechat",
                                    input_summary=q, output_summary="0 条结果")
                    continue
                for title, link_href, account, ts, snippet in results:
                    try:
                        real = self._resolve_sogou_link(client, link_href)
                    except Exception as e:  # noqa: BLE001
                        real = ""
                    if not real or real in seen_urls:
                        continue
                    seen_urls.add(real)
                    out.append(Article(
                        title=title, url=real,
                        source=f"微信公众号·{account}" if account else "微信公众号",
                        published=_epoch_iso(ts),
                        excerpt=snippet,
                        raw={"from_weixin": True},
                    ))
                    if len(out) >= max_arts:
                        break
                if len(out) >= max_arts:
                    break
        # 对标题命中关键词的公众号文章抓正文（补全摘要），其余保留列表页摘要
        hits = 0
        for a in out:
            if a.raw.get("body_fetched"):
                continue
            if not _title_hit(a.title):
                continue
            try:
                body = self._fetch_wechat_article(a.url)[0].excerpt
                if body:
                    a.excerpt = body
                    a.raw["body_fetched"] = True
                    hits += 1
            except Exception:  # noqa: BLE001
                pass
        if hits:
            self.trace.step(stage="search", action="process", tool="sogou_wechat_body",
                            input_summary=f"{len(out)} 条", output_summary=f"抓取正文 {hits} 条")
        return out

    def _resolve_sogou_link(self, client: httpx.Client, link_href: str) -> str:
        """/link?url=... 页面内嵌真实 mp.weixin.qq.com 地址（JS 字符串分片），提取拼接。"""
        resp = client.get("https://weixin.sogou.com" + link_href)
        resp.raise_for_status()
        frags = re.findall(r"url\s*\+=\s*'([^']*)'", resp.text)
        if not frags:
            return ""
        real = "".join(frags).replace("@", "")
        if not real.startswith("http"):
            return ""
        return real


# ------------------------------------------------------------------ 工具函数
def _fmt_struct_time(st) -> Optional[str]:
    if not st:
        return None
    try:
        return datetime(*st[:6], tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return None


def _clean_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- 搜狗微信辅助
SOGOU_CAPTCHA_MARKS = ["antispider", "请输入验证码", "访问过于频繁", "请完成验证", "验证码错误"]


def _is_sogou_captcha(page: str) -> bool:
    low = page.lower()
    return any(m in low for m in SOGOU_CAPTCHA_MARKS)


def _parse_sogou_results(page: str, query: str) -> list[tuple[str, str, str, Optional[int], str]]:
    """解析搜狗微信搜索结果页。
    返回 [(标题, /link href, 账号名, epoch秒, 摘要)]；失败/空页返回 []。"""
    out: list[tuple[str, str, str, Optional[int], str]] = []
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]*href="(/link\?url=[^"]+)"[^>]*>(.*?)</a>', page, re.S):
        link_href, title_html = m.group(1), m.group(2)
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        if not title:
            continue
        seg = page[m.start(): m.start() + 2500]
        acct_m = re.search(r'class="all-time-y2"[^>]*>(.*?)</span>', seg, re.S)
        account = re.sub(r"<[^>]+>", "", acct_m.group(1)).strip() if acct_m else ""
        ts_m = re.search(r"timeConvert\('(\d+)'\)", seg)
        ts = int(ts_m.group(1)) if ts_m else None
        snip_m = re.search(r'class="txt-info"[^>]*>(.*?)</p>', seg, re.S)
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip() if snip_m else ""
        out.append((title, link_href, account, ts, snippet[:500]))
    return out


def _epoch_iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (ValueError, OSError):
        return None


def _title_hit(title: str) -> bool:
    hits = ["无人机", "反制", "探测", "低空", "蜂群", "黑飞", "干扰", "诱骗", "激光", "频谱",
            "雷达", "光电", "counter", "drone", "uas", "cuas", "anti-drone"]
    low = title.lower()
    return any(h in low for h in hits)


def dedupe(articles: list[Article], trace: TraceLog, seen: set[str]) -> list[Article]:
    """URL + 标题相似度去重；seen 为历史已收录 URL（跨天去重）。"""
    out: list[Article] = []
    seen_titles: list[str] = []
    dropped = 0
    for a in articles:
        if a.url_key in seen:
            dropped += 1
            continue
        norm = re.sub(r"[\s\u3000\-—–:：,，。.]+", "", a.title.lower())
        if not norm:
            dropped += 1
            continue
        if any(_sim(norm, t) > 0.86 for t in seen_titles):
            dropped += 1
            continue
        seen_titles.append(norm)
        out.append(a)
    trace.step(stage="filter", action="process", tool="dedupe",
               input_summary=f"候选 {len(articles)} 条", output_summary=f"去重后 {len(out)} 条（剔除 {dropped}）")
    return out


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    m = len(short)
    best = 0
    for i in range(m):
        for j in range(i + best + 1, m + 1):
            if short[i:j] in long_:
                best = j - i
            else:
                break
    return best / len(long_)


def filter_by_time(articles: list[Article], window_hours: int,
                   lookback_hours: int, now: datetime) -> tuple[list[Article], bool]:
    """按时间窗过滤。返回 (保留文章, 是否启用了回溯)。
    无发布日期的文章保留（来源多不标注日期），但排到列表尾部由 LLM 判定相关性。"""
    cutoff = now - timedelta(hours=window_hours)
    cutoff_back = now - timedelta(hours=lookback_hours)
    fresh, older, unknown = [], [], []
    for a in articles:
        if not a.published:
            unknown.append(a)
            continue
        try:
            dt = datetime.fromisoformat(a.published)
        except ValueError:
            unknown.append(a)
            continue
        if dt >= cutoff:
            fresh.append(a)
        elif dt >= cutoff_back:
            older.append(a)
    if len(fresh) >= 6:
        return fresh + unknown, False
    return fresh + older + unknown, bool(older)
