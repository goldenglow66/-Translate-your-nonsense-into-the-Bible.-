# -*- coding: utf-8 -*-
"""
检索引擎：中文 n-gram 倒排索引 + BM25 + 概念同义扩展。

为什么不用向量检索？
  全本 31,102 节在其语义检索上，最稳的做法是「召回」与「排序」分离：
  本地 BM25 负责高召回（快、可解释、无需联网），
  可选的 LLM 负责真正的语义重排（见 llm.py）。
  这样即使没有配置任何 API Key，网站也是完整可用的。
"""
from __future__ import annotations

import json
import math
import re
import time
from array import array
from pathlib import Path

from concepts import CONCEPTS, QUERY_STOP

PUNCT = re.compile(r"[^\u4e00-\u9fffA-Za-z]+")
SENT_SPLIT = re.compile(r"[，。；：！？、\n,.!?;:]+")

K1 = 1.35
B = 0.72
W_ORIG = 0.62        # 用户原句 n-gram 权重
W_CONCEPT = 1.58     # 概念扩展词权重
W_CHAR = 0.10        # 单字权重（仅短查询兜底）
W_PHRASE = 3.2       # 完整短语命中加分

# 纯语法功能字：任何含有这些字的 bigram 都没有检索价值
FUNC_CHARS = set("的了是我你他她它们在和就也都太会能说着过这一 个么吗呢吧啊来去到对从被把让给还又再才只更最但而所以为于之与或向")

# 口语套语 bigram：字面都有实义，组合起来却是无信息量的填充
STOP_BIGRAMS = set("""
知道 不知 什么 怎么 怎样 这样 那样 时候 事情 东西 一个 这个 那个 我们 你们 他们
她们 自己 可以 应该 需要 没有 不是 就是 因为 如果 虽然 觉得 感觉 想要 希望 一点
一些 真的 非常 特别 比较 有点 一直 总是 经常 已经 现在 今天 明天 昨天 最近 问题
情况 方面 样子 一样 起来 出来 下去 进去 不过 而且 然后 于是 因此 其实 当然 只是
还有 以及 或者 而是 凡是 所有 每个 任何 的话 有些 好多 感觉 好像 似乎 是否 到底
""".split())


def clean(text: str) -> str:
    """去掉标点与空白，只留下可用于 n-gram 的字符。"""
    return PUNCT.sub("", text)


def bigrams(text: str):
    """返回去重后的 bigram 列表（保持顺序）。"""
    seen = set()
    out = []
    for i in range(len(text) - 1):
        g = text[i:i + 2]
        if g in seen:
            continue
        seen.add(g)
        out.append(g)
    return out


def split_sentences(text: str):
    """把一节经文切成小句；过短的碎片并入相邻句。"""
    parts = [p.strip() for p in SENT_SPLIT.split(text) if p and p.strip()]
    merged = []
    buf = ""
    for p in parts:
        if len(p) < 5:
            buf += p
            if len(buf) >= 8:
                merged.append(buf)
                buf = ""
        else:
            if buf:
                p = buf + p
                buf = ""
            merged.append(p)
    if buf:
        if merged:
            merged[-1] += buf
        else:
            merged.append(buf)
    return merged or [text]


class VerseIndex:
    def __init__(self, verses_path: Path):
        t0 = time.perf_counter()
        raw = json.loads(Path(verses_path).read_text(encoding="utf-8"))
        self.meta_version = raw.get("version", "和合本")
        self.books = raw["books"]
        self.book_ids = raw.get("book_ids") or list(range(len(self.books)))
        self.verses = raw["verses"]          # [[b, c, v, text], ...]

        # ---- 建立小句级倒排索引 ----
        self.sent_text = []
        self.sent_verse = []                 # 小句 -> 经文下标
        self.post = {}                       # term -> (array(docid), array(tf))
        self.df = {}

        tmp = {}
        sums = 0
        for vi, row in enumerate(self.verses):
            text = row[3]
            for s in split_sentences(text):
                cs = clean(s)
                if len(cs) < 2:
                    continue
                did = len(self.sent_text)
                self.sent_text.append(s)
                self.sent_verse.append(vi)
                sums += len(cs)
                grams = bigrams(cs)
                for g in grams:
                    lst = tmp.get(g)
                    if lst is None:
                        tmp[g] = lst = []
                    lst.append(did)
                # 单字（仅用于 1 字查询兜底）
                for ch in set(cs):
                    tmp.setdefault("\x01" + ch, []).append(did)

        n = len(self.sent_text)
        self.n_docs = n
        self.avgdl = (sums / n) if n else 1.0

        # 把 list 转成 (docid array, tf array)，同时统计 df
        for term, docs in tmp.items():
            docs.sort()
            ids = array("i")
            tfs = array("B")
            last = -1
            cnt = 0
            for d in docs:
                if d == last:
                    cnt += 1
                    if cnt < 255:
                        tfs[-1] = cnt
                else:
                    ids.append(d)
                    tfs.append(1)
                    last = d
                    cnt = 1
            self.post[term] = (ids, tfs)
            self.df[term] = len(ids)

        # 预计算 idf
        self.idf = {}
        for term, d in self.df.items():
            self.idf[term] = math.log((n - d + 0.5) / (d + 0.5) + 1.0)

        # 句子长度（用 clean 后长度）
        self.sent_len = array("H", [min(65535, len(clean(s))) for s in self.sent_text])

        # ---- 概念表预处理 ----
        self.concepts = []
        for c in CONCEPTS:
            self.concepts.append({
                "k": c["k"],
                "t": [x for x in c["t"] if x],
                "x": [x for x in c["x"] if len(clean(x)) >= 2],
            })

        self.build_ms = (time.perf_counter() - t0) * 1000

    # ------------------------------------------------------------------
    def _expand(self, q: str):
        """根据查询命中情况，返回 (命中的概念名列表, [(扩展词, 位置权重)])。

        扩展词按概念表里的书写顺序即重要性排序，越靠前越核心
        （如「焦虑」的 x 以「忧虑、挂虑」领头），因此顺序即权重。
        """
        hits = []
        extra = []
        seen = set()
        qc = clean(q)
        for c in self.concepts:
            if any(t in qc for t in c["t"]):
                hits.append(c["k"])
                for i, w in enumerate(c["x"]):
                    cw = clean(w)
                    if len(cw) < 2 or cw in seen:
                        continue
                    seen.add(cw)
                    pos = 1.0 - 0.05 * min(i, 7)
                    extra.append((w, pos))
        return hits, extra

    def expand_words(self, q: str, n: int = 12):
        """查询命中的概念扩展词（按重要性排序），供前端做高亮用。"""
        _, extra = self._expand(q)
        seen, out = set(), []
        for w, _pos in extra:
            cw = clean(w)
            if len(cw) < 2 or cw in seen:
                continue
            seen.add(cw)
            out.append(cw)
            if len(out) >= n:
                break
        return out

    def _query_terms(self, q: str):
        """构造 {term: weight}。

        核心思路：命中概念时，可以确信用户表达的是「某种处境/心境」，
        此时和合本的对应译法（忧虑/挂虑/饶恕…）才是真正的检索目标，
        原句里的口语 bigram 只作辅助，权重下调。
        """
        weights = {}
        qc = clean(q)
        concepts, extra = self._expand(q)

        def add(term, w):
            if not term:
                return
            if term in weights:
                weights[term] = max(weights[term], w)
            else:
                weights[term] = w

        boosted = bool(concepts)
        w_orig = W_ORIG * (0.68 if boosted else 1.0)
        c_boost = 1.22 if boosted else 1.0
        n = self.n_docs or 1
        df_tab = self.df

        # 1) 原句 bigram，滤掉语法组合、口语套语，以及「哪里都有」的泛词
        for g in bigrams(qc):
            if g in STOP_BIGRAMS:
                continue
            if g[0] in FUNC_CHARS or g[1] in FUNC_CHARS:
                continue
            df = df_tab.get(g, 0)
            if not df:
                continue
            ratio = df / n
            if ratio > 0.025:            # 超过 2.5% 的小句都含它 → 没有区分度
                continue
            add(g, w_orig * max(0.25, 1.0 - ratio * 34.0))

        # 2) 单字兜底：只对很短的查询启用
        if len(qc) <= 5:
            for ch in set(qc):
                if ch in FUNC_CHARS or ch in QUERY_STOP:
                    continue
                add("\x01" + ch, W_CHAR)

        # 3) 概念扩展词（真正的主力），按概念表内的顺序衰减
        for w, pos in extra:
            cw = clean(w)
            if len(cw) < 2:
                continue
            wt = W_CONCEPT * c_boost * pos
            if len(cw) == 2:
                r2 = df_tab.get(cw, 0) / n
                if r2 > 0.012:           # 太常见的概念词轻罚
                    wt *= max(0.5, 1.0 - (r2 - 0.012) * 9.0)
                add(cw, wt)
            else:
                for g in bigrams(cw):
                    if g[0] in FUNC_CHARS or g[1] in FUNC_CHARS:
                        continue
                    add(g, wt * 0.78)
                add(cw, wt * 1.05)

        return weights, concepts, extra

    # ------------------------------------------------------------------
    def search(self, q: str, topk: int = 20, boost_verses=None):
        """返回打分后的经文列表。"""
        t0 = time.perf_counter()
        q = (q or "").strip()
        if not q:
            return [], [], 0.0

        weights, concepts, extra = self._query_terms(q)
        qc = clean(q)
        scores = {}
        n = self.n_docs
        avgdl = self.avgdl
        post = self.post
        idf = self.idf
        slen = self.sent_len

        for term, w in weights.items():
            entry = post.get(term)
            if entry is None:
                continue
            ids, tfs = entry
            ti = idf[term] * w
            if ti <= 0:
                continue
            for i in range(len(ids)):
                d = ids[i]
                tf = tfs[i]
                dl = slen[d]
                denom = tf + K1 * (1 - B + B * dl / avgdl)
                scores[d] = scores.get(d, 0.0) + ti * (tf * (K1 + 1)) / denom

        # 完整短语命中：子串匹配比 n-gram 更精确，直接把概念词顶上
        phrases = []
        for w, pos in extra:
            cw = clean(w)
            if len(cw) >= 3:
                phrases.append((cw, W_PHRASE * 0.95 * pos))
            elif len(cw) == 2:
                phrases.append((cw, W_PHRASE * 0.40 * pos))
        if phrases and scores:
            for d in scores:
                t = clean(self.sent_text[d])
                bonus = 0.0
                hits_n = 0
                for p, wt in phrases:
                    if p in t:
                        bonus += wt * (1.0 + 0.22 * (len(p) - 2))
                        hits_n += 1
                if hits_n >= 2:                 # 同一小句命中多个概念词，是主题句
                    bonus *= 1.0 + 0.10 * (hits_n - 1)
                if bonus:
                    scores[d] += bonus

        # 长度惩罚：极短小句容易误命中
        for d in list(scores.keys()):
            L = slen[d]
            if L <= 4:
                scores[d] *= 0.55
            elif L <= 7:
                scores[d] *= 0.82

        # ---- 小句分数聚合到「节」 ----
        best = {}
        for d, s in scores.items():
            vi = self.sent_verse[d]
            cur = best.get(vi)
            if cur is None or s > cur[0]:
                prev = cur[1] if cur else []
                best[vi] = (s, [(s, d)] + prev[:2])
            else:
                if len(cur[1]) < 3:
                    cur[1].append((s, d))

        ranked = []
        for vi, (s, parts) in best.items():
            total = s + 0.18 * sum(x[0] for x in parts[1:])
            ranked.append((total, vi, parts))
        ranked.sort(key=lambda x: -x[0])

        if boost_verses:
            bump = {}
            for vi in boost_verses:
                bump[vi] = bump.get(vi, 0) + 1
            ranked.sort(key=lambda x: -(x[0] * (1 + 0.35 * bump.get(x[1], 0))))

        results = []
        for total, vi, parts in ranked[:topk]:
            row = self.verses[vi]
            b = row[0]
            book = self.books[b] if b < len(self.books) else f"卷{b}"
            bid = self.book_ids[b] if b < len(self.book_ids) else b
            results.append({
                "id": vi,
                "book": book,
                "bookId": bid,
                "chapter": row[1],
                "verse": row[2],
                "ref": f"{book} {row[1]}:{row[2]}",
                "text": row[3],
                "score": total,
                "snippets": [p[1] for p in parts[:3]],
            })

        # 归一化到 0-100
        if results:
            hi = results[0]["score"] or 1.0
            for r in results:
                r["rel"] = round(100 * (r["score"] / hi) ** 0.62, 1)
                r.pop("score", None)

        took = (time.perf_counter() - t0) * 1000
        return results, concepts, took

    # ------------------------------------------------------------------
    def get(self, book_id: int, chapter: int, verse: int):
        for vi, row in enumerate(self.verses):
            b = self.book_ids[row[0]] if row[0] < len(self.book_ids) else row[0]
            if b == book_id and row[1] == chapter and row[2] == verse:
                book = self.books[row[0]]
                return {
                    "book": book, "bookId": b, "chapter": chapter, "verse": verse,
                    "ref": f"{book} {chapter}:{verse}", "text": row[3],
                }
        return None

    def chapter(self, book_id: int, chapter: int):
        out = []
        for row in self.verses:
            b = self.book_ids[row[0]] if row[0] < len(self.book_ids) else row[0]
            if b == book_id and row[1] == chapter:
                out.append({"verse": row[2], "text": row[3]})
        return out

    def stats(self):
        return {
            "verses": len(self.verses),
            "books": len(self.books),
            "sentences": self.n_docs,
            "terms": len(self.post),
            "buildMs": round(self.build_ms),
            "version": self.meta_version,
        }
