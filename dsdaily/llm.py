"""LLM 客户端（OpenAI 兼容 API）。

- 真实模式：POST {base_url}/chat/completions，要求模型输出 JSON（结构化结果），
  内置 JSON 解析兜底（去代码围栏 / 提取首个 JSON 对象 / 失败降级规则结果）。
- Mock 模式（api_key 未配置时自动启用）：规则引擎产出确定结果，用于无密钥联调
  与自动化测试；每条调用同样计入 trace（tokens 记为 0）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx

from .tracelog import TraceLog


class LLMError(Exception):
    pass


def _extract_json(text: str) -> Optional[Any]:
    """从模型输出中稳健提取 JSON（去 ```json 围栏、截取首个 {...} 或 [...]）。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    for candidate in (text,):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # 截取首个平衡的 JSON 对象
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    start = text.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None


class LLMClient:
    def __init__(self, cfg: dict, trace: TraceLog):
        self.cfg = cfg
        self.trace = trace
        self.mock = not (cfg.get("api_key") or "").strip()

    # ------------------------------------------------------------------ 基础
    def chat(self, system: str, user: str, expect_json: bool = False,
             max_tokens: Optional[int] = None) -> tuple[str, Optional[dict]]:
        """真实模式调用 LLM，返回 (文本, token用量)。
        429/5xx/超时自动重试（最多 3 次，指数退避）。"""
        if self.mock:
            return self._mock_chat(system, user), None
        headers = {
            "Authorization": f"Bearer {self.cfg['api_key']}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(self.cfg.get("temperature", 0.3)),
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        url = self.cfg["base_url"].rstrip("/") + "/chat/completions"
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=float(self.cfg.get("timeout", 120))) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage") or {}
                tokens = {"input": usage.get("prompt_tokens", 0),
                          "output": usage.get("completion_tokens", 0)}
                return content, tokens
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                status = getattr(e, "response", None)
                status_code = status.status_code if status is not None else None
                retryable = status_code in (429, 500, 502, 503, 504) or isinstance(e, httpx.TimeoutException)
                if attempt < 2 and retryable:
                    import time as _t
                    _t.sleep(2 ** attempt)
                    continue
                raise LLMError(f"LLM 调用失败（第 {attempt + 1} 次尝试）: {e}") from e
            except Exception as e:  # noqa: BLE001
                raise LLMError(f"LLM 调用失败: {e}") from e
        raise LLMError(f"LLM 调用失败: {last_err}")

    def chat_json(self, system: str, user: str, fallback: Any,
                  max_tokens: Optional[int] = None) -> tuple[Any, Optional[dict]]:
        """请求 JSON 结果，解析失败时返回 fallback（并记录错误）。"""
        text, tokens = self.chat(system, user, expect_json=True, max_tokens=max_tokens)
        parsed = _extract_json(text)
        if parsed is None:
            self.trace.step(
                stage="llm", action="llm_call", tool="llm_json",
                input_summary=user[:200], output_summary="JSON 解析失败，使用规则降级",
                status="error", error={"type": "json_parse_failed", "msg": text[:300]},
            )
            return fallback, tokens
        return parsed, tokens

    # ------------------------------------------------------------------ 任务
    def generate_keywords(self, base_keywords: list[str], date_str: str) -> list[str]:
        system = (
            "你是无人机感知与反制领域的资讯检索策划。根据基础关键词和日期，"
            "生成 8-12 个当日检索关键词（含中文与英文，覆盖探测感知/反制/法规/事件/低空经济）。"
            "只输出 JSON 数组，如 [\"无人机反制\", ...]。"
        )
        user = f"日期: {date_str}\n基础关键词: {json.dumps(base_keywords, ensure_ascii=False)}"
        if self.mock:
            return self._mock_keywords(base_keywords)
        parsed, tokens = self.chat_json(system, user, fallback=base_keywords)
        kw = parsed if isinstance(parsed, list) else (parsed.get("keywords") if isinstance(parsed, dict) else None)
        if not isinstance(kw, list) or not kw:
            kw = base_keywords
        kw = [str(x).strip() for x in kw if str(x).strip()][:12]
        self.trace.step(
            stage="llm", action="llm_call", tool="generate_keywords",
            input_summary=f"基础词 {len(base_keywords)} 个", output_summary=f"生成 {len(kw)} 个",
            tokens=tokens,
        )
        return kw

    def filter_articles(self, articles: list[dict]) -> list[dict]:
        """批量相关性判定。articles 为 [{index,title,source,excerpt}]，返回 [{index,keep,reason}]。"""
        system = (
            "你是无人机感知与反制技术日报的编辑。判断文章是否与『无人机感知探测/反制防御技术』"
            "相关（雷达/频谱/光电/声学探测、干扰/GPS诱骗/激光/网捕/动能反制、机场低空安防、"
            "黑飞事件、无人机法规空域、反无人机新品与测试、AI+反无人机、低空经济安防等）。"
            "纯无关商业软文/娱乐内容剔除。只输出 JSON 数组，每项 {\"index\":0,\"keep\":true,\"reason\":\"...\"}。"
        )
        items = [{"index": i, "title": a["title"][:120], "source": a["source"],
                  "excerpt": a.get("excerpt", "")[:200]} for i, a in enumerate(articles)]
        user = f"共 {len(items)} 篇候选：\n" + json.dumps(items, ensure_ascii=False)
        if self.mock:
            # mock：标题含关键词的保留，其余按比例保留前 60%
            kept = []
            for it in items:
                if self._title_hit(it["title"]):
                    kept.append({"index": it["index"], "keep": True, "reason": "标题命中关键词(mock)"})
            # 保证至少一半候选进入后续（便于联调观察）
            if len(kept) < max(3, len(items) // 3):
                for it in items:
                    if all(k["index"] != it["index"] for k in kept):
                        kept.append({"index": it["index"], "keep": True, "reason": "补齐候选(mock)"})
                        if len(kept) >= max(3, len(items) // 3):
                            break
            self.trace.step(stage="llm", action="llm_call", tool="filter_articles",
                            input_summary=f"{len(items)} 篇候选",
                            output_summary=f"保留 {len(kept)} 篇（mock）")
            return kept
        parsed, tokens = self.chat_json(system, user, fallback=[])
        if not isinstance(parsed, list):
            parsed = parsed.get("results", []) if isinstance(parsed, dict) else []
        out = [{"index": int(x.get("index", -1)), "keep": bool(x.get("keep", False)),
                "reason": str(x.get("reason", ""))[:120]} for x in parsed
               if isinstance(x, dict) and str(x.get("index", "")).isdigit()]
        self.trace.step(
            stage="llm", action="llm_call", tool="filter_articles",
            input_summary=f"{len(items)} 篇候选", output_summary=f"判定 {len(out)} 篇",
            tokens=tokens,
        )
        return out

    def classify_and_summarize(self, articles: list[dict]) -> list[dict]:
        """分类+摘要。articles 为 [{index,title,url,source,published,excerpt}]。
        返回 [{index,tags:[...],summary,focus:bool}]。tags 从 7 类中选：探测感知/干扰反制/
        实战案例/政策法规/技术前沿/行业动态/国际视野。摘要 80-120 字。focus：当日重大事件至多 1 篇。"""
        system = (
            "你是无人机感知与反制技术日报的编辑。为每篇文章：①打分类标签（可多标签），"
            "类别仅限：探测感知、干扰反制、实战案例、政策法规、技术前沿、行业动态、国际视野；"
            "②写 80-120 字摘要（基于内容，不得臆造）；③focus 标记当日重大事件（黑飞事件/重大政策/"
            "重要新品发布），全部无重大事件则全为 false，至多 1 篇为 true。"
            "只输出 JSON 数组，每项 {\"index\":0,\"tags\":[\"探测感知\"],\"summary\":\"...\",\"focus\":false}。"
        )
        items = [{"index": i, "title": a["title"][:150], "source": a["source"],
                  "published": a.get("published") or "", "excerpt": a.get("excerpt", "")[:400]}
                 for i, a in enumerate(articles)]
        user = f"共 {len(items)} 篇：\n" + json.dumps(items, ensure_ascii=False)
        if self.mock:
            out = []
            tag_pool = ["探测感知", "干扰反制", "实战案例", "政策法规", "技术前沿", "行业动态", "国际视野"]
            for it in items:
                idx = it["index"]
                tags = [tag_pool[idx % 7]]
                if idx % 3 == 0 and len(tags) < 2:
                    tags.append(tag_pool[(idx + 3) % 7])
                summary = (articles[idx].get("excerpt") or articles[idx]["title"])[:100]
                out.append({"index": idx, "tags": tags, "summary": summary, "focus": idx == 0})
            self.trace.step(stage="llm", action="llm_call", tool="classify_and_summarize",
                            input_summary=f"{len(items)} 篇", output_summary=f"完成 {len(out)} 篇（mock）")
            return out
        parsed, tokens = self.chat_json(system, user, fallback=[])
        if not isinstance(parsed, list):
            parsed = parsed.get("results", []) if isinstance(parsed, dict) else []
        allowed = {"探测感知", "干扰反制", "实战案例", "政策法规", "技术前沿", "行业动态", "国际视野"}
        out = []
        for x in parsed:
            if not isinstance(x, dict):
                continue
            idx = int(x.get("index", -1))
            if idx < 0:
                continue
            tags = [str(t) for t in (x.get("tags") or []) if str(t) in allowed][:3]
            summary = str(x.get("summary", ""))[:200]
            out.append({"index": idx, "tags": tags, "summary": summary,
                        "focus": bool(x.get("focus", False))})
        self.trace.step(
            stage="llm", action="llm_call", tool="classify_and_summarize",
            input_summary=f"{len(items)} 篇", output_summary=f"完成 {len(out)} 篇",
            tokens=tokens,
        )
        return out

    # ------------------------------------------------------------------ mock
    def _mock_chat(self, system: str, user: str) -> str:
        return json.dumps({"mock": True, "note": "mock 模式：未配置 llm.api_key"})

    def _mock_keywords(self, base: list[str]) -> list[str]:
        return (base[:8] + ["counter-drone", "CUAS", "低空经济 安防", "反无人机 测试"])[:12]

    def _title_hit(self, title: str) -> bool:
        hits = ["无人机", "反制", "探测", "低空", "蜂群", "黑飞", "干扰", "诱骗", "激光", "频谱",
                "雷达", "光电", "counter", "drone", "uas", "cuas", "anti-drone"]
        return any(h in title.lower() for h in hits)
