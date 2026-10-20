# -*- coding: utf-8 -*-
"""
可选的大模型增强层（OpenAI 兼容接口）。

本地 BM25 负责「召回」，大模型负责「理解」与「重排」：
  1. expand : 把用户的口语表达理解成处境 + 主题 + 和合本常用检索词
  2. rerank : 对本地召回的候选经文做真正的语义排序，并逐条说明理由

API Key 只从请求体传入，服务器不落盘、不记日志。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class LLMError(Exception):
    pass


def _endpoint(base: str) -> str:
    base = (base or DEFAULT_BASE).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def chat(cfg: dict, messages, json_mode=True, timeout=90, max_tokens=900):
    base = cfg.get("baseUrl") or DEFAULT_BASE
    key = (cfg.get("apiKey") or "").strip()
    model = cfg.get("model") or DEFAULT_MODEL
    if not key:
        raise LLMError("未配置 API Key")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.25)),
        "max_tokens": int(cfg.get("maxTokens") or max_tokens),
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    def _do(body):
        req = urllib.request.Request(
            _endpoint(base),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "User-Agent": "jingyi/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    try:
        try:
            data = _do(payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            # 有些兼容端点不支持 response_format，去掉后重试一次
            if e.code in (400, 404, 422) and "response_format" in json.dumps(payload):
                payload.pop("response_format", None)
                data = _do(payload)
            else:
                raise LLMError(f"接口返回 {e.code}：{body}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"无法连接模型接口：{e.reason}") from None
    except TimeoutError:
        raise LLMError("模型接口超时") from None
    except json.JSONDecodeError:
        raise LLMError("模型接口返回的不是合法 JSON") from None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"接口返回结构异常：{str(data)[:200]}") from None
    return content or ""


def parse_json(text: str) -> dict:
    """从模型输出里尽量抠出 JSON 对象。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型没有返回内容")
    # 去掉 ```json 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        frag = text[i:j + 1]
        try:
            return json.loads(frag)
        except json.JSONDecodeError:
            frag = re.sub(r",\s*([}\]])", r"\1", frag)
            try:
                return json.loads(frag)
            except json.JSONDecodeError:
                pass
    raise LLMError("模型返回内容无法解析为 JSON")


# ---------------------------------------------------------------- expand
EXPAND_SYS = (
    "你是一位熟悉中文圣经（和合本）的检索助手。"
    "用户会用日常口语说出自己的处境、心情或想找的道理，你要把它翻译成圣经里会用的表达。"
    "只输出 JSON，不要任何解释文字。"
)

EXPAND_TMPL = """用户输入：「{q}」

请完成三件事：
1. analysis：用一句不超过 40 字的话概括用户真正在寻找什么（处境 + 核心诉求）。
2. themes：提炼 2-3 个主题词（如「忧虑」「饶恕」「盼望」）。
3. keywords：给出 6-8 个用于在**和合本正文**中做全文检索的词或短语。
   - 必须使用和合本常见的译法，例如用户说「焦虑」要用「忧虑/挂虑」，
     「原谅」要用「饶恕/赦免」，「孤单」要用「独居/撇下」，「很累」要用「劳苦/担重担/安息」。
   - 不要出现书名、章节号。
   - 每个词 2-6 个字，尽量是经文里原样出现的片段。

输出 JSON：
{{"analysis": "...", "themes": ["..."], "keywords": ["..."]}}"""


def expand(cfg: dict, q: str) -> dict:
    text = chat(cfg, [
        {"role": "system", "content": EXPAND_SYS},
        {"role": "user", "content": EXPAND_TMPL.format(q=q)},
    ], max_tokens=500)
    data = parse_json(text)
    kws = data.get("keywords") or []
    if not isinstance(kws, list):
        kws = [str(kws)]
    clean_kws = [str(k).strip() for k in kws if str(k).strip()][:10]
    themes = data.get("themes") or []
    if not isinstance(themes, list):
        themes = [str(themes)]
    return {
        "analysis": str(data.get("analysis") or "").strip(),
        "themes": [str(t).strip() for t in themes if str(t).strip()][:4],
        "keywords": clean_kws,
    }


# ---------------------------------------------------------------- rerank
RERANK_SYS = (
    "你是一位严谨的圣经经文匹配专家。"
    "你要判断给定的圣经经文与用户表达的含义是否真的相近——"
    "看的是**语义与处境**，不是字面重合。"
    "只输出 JSON，不要解释。"
)

RERANK_TMPL = """用户输入：「{q}」

候选经文（编号 + 出处 + 正文）：
{cands}

请从候选中挑出与用户输入**含义最相近**的经文（最多 {keep} 条），按相关度从高到低排序。

要求：
- score 为 0-1 的小数，表示语义相关度，要能区分强弱，不要都给同一个数。
- reason 用不超过 30 个汉字说明「为什么这节经文回应用户的处境」，要具体，不要套话。
- 只能使用上面出现过的编号，不要编造经文。
- 如果某条只是字面沾边而含义无关，不要选它。

输出 JSON：
{{"ranking": [{{"i": 3, "score": 0.93, "reason": "..."}}]}}"""


def rerank(cfg: dict, q: str, candidates, keep=8) -> list:
    lines = []
    for idx, c in enumerate(candidates, start=1):
        t = c["text"]
        if len(t) > 90:
            t = t[:90] + "…"
        lines.append(f"[{idx}] {c['ref']} — {t}")
    cands = "\n".join(lines)

    text = chat(cfg, [
        {"role": "system", "content": RERANK_SYS},
        {"role": "user", "content": RERANK_TMPL.format(q=q, cands=cands, keep=keep)},
    ], max_tokens=1200)
    data = parse_json(text)

    out = []
    for item in data.get("ranking") or []:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= len(candidates)):
            continue
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            score = 0.5
        score = max(0.0, min(1.0, score))
        out.append({
            "index": i - 1,
            "score": score,
            "reason": str(item.get("reason") or "").strip()[:80],
        })
    out.sort(key=lambda x: -x["score"])
    return out[:keep]


# --------------------------------------------------------------- explain
EXPLAIN_SYS = (
    "你是一位温和、严谨的圣经讲解者，面向普通读者。"
    "用平实的现代汉语解释经文，不空喊口号，不做人身论断。"
)

EXPLAIN_TMPL = """经文：{ref}
「{text}」

用户的处境是：「{q}」

请用 3 个小节回答，每节 1-3 句：
1. 这节经文在说什么（结合上下文与关键词）
2. 它为什么与用户的处境相关
3. 可以怎样落实到今天的生活

用纯文本输出，小标题用「一、二、三、」开头。总字数控制在 300 字以内。"""


def explain(cfg: dict, ref: str, text: str, q: str = "") -> str:
    return chat(cfg, [
        {"role": "system", "content": EXPLAIN_SYS},
        {"role": "user", "content": EXPLAIN_TMPL.format(
            ref=ref, text=text, q=q or "（未提供，请聚焦经文本身）")},
    ], json_mode=False, max_tokens=700, timeout=90).strip()


# ------------------------------------------------------------------ ping
def ping(cfg: dict) -> str:
    """用最小的请求验证接口地址 / Key / 模型名是否可用。"""
    content = chat(cfg, [
        {"role": "user", "content": "只回复两个字：可用"},
    ], json_mode=False, max_tokens=16, timeout=30)
    return (content or "").strip()[:60]
