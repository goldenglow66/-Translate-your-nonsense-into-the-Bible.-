#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建前端/后端使用的精简经文数据。

输入: _data/ChiUn.json  (Chinese Union Version, 和合本)
输出: data/verses.json  {"version": "...", "books": [...], "verses": [[b,c,v,"text"], ...]}

零依赖，仅用标准库。
"""
import json
import re
import sys
from pathlib import Path

TT2S = None


def get_t2s():
    """惰性加载 OpenCC 繁转简转换器；不可用时返回 None。"""
    global TT2S
    if TT2S is None:
        try:
            from opencc import OpenCC  # type: ignore
            TT2S = OpenCC("t2s")
        except Exception:
            TT2S = False
    return TT2S or None

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "_data" / "ChiUn.json"
OUT = ROOT / "data" / "verses.json"

# 66 卷中文名（和合本正名），按顺序
BOOKS_ZH = [
    "创世记", "出埃及记", "利未记", "民数记", "申命记", "约书亚记", "士师记", "路得记",
    "撒母耳记上", "撒母耳记下", "列王纪上", "列王纪下", "历代志上", "历代志下",
    "以斯拉记", "尼希米记", "以斯帖记", "约伯记", "诗篇", "箴言", "传道书", "雅歌",
    "以赛亚书", "耶利米书", "耶利米哀歌", "以西结书", "但以理书", "何西阿书", "约珥书",
    "阿摩司书", "俄巴底亚书", "约拿书", "弥迦书", "那鸿书", "哈巴谷书", "西番雅书",
    "哈该书", "撒迦利亚书", "玛拉基书",
    "马太福音", "马可福音", "路加福音", "约翰福音", "使徒行传", "罗马书",
    "哥林多前书", "哥林多后书", "加拉太书", "以弗所书", "腓立比书", "歌罗西书",
    "帖撒罗尼迦前书", "帖撒罗尼迦后书", "提摩太前书", "提摩太后书", "提多书",
    "腓利门书", "希伯来书", "雅各书", "彼得前书", "彼得后书", "约翰一书",
    "约翰二书", "约翰三书", "犹大书", "启示录",
]

# scrollmapper 里使用的英文书名 -> 序号（从 0 开始）
BOOKS_EN = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua", "Judges", "Ruth",
    "1 Samuel", "2 Samuel", "1 Kings", "2 Kings", "1 Chronicles", "2 Chronicles",
    "Ezra", "Nehemiah", "Esther", "Job", "Psalms", "Proverbs", "Ecclesiastes",
    "Song of Solomon", "Isaiah", "Jeremiah", "Lamentations", "Ezekiel", "Daniel",
    "Hosea", "Joel", "Amos", "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk",
    "Zephaniah", "Haggai", "Zechariah", "Malachi",
    "Matthew", "Mark", "Luke", "John", "Acts", "Romans",
    "1 Corinthians", "2 Corinthians", "Galatians", "Ephesians", "Philippians",
    "Colossians", "1 Thessalonians", "2 Thessalonians", "1 Timothy", "2 Timothy",
    "Titus", "Philemon", "Hebrews", "James", "1 Peter", "2 Peter", "1 John",
    "2 John", "3 John", "Jude", "Revelation",
]

EN2IDX = {name.lower(): i for i, name in enumerate(BOOKS_EN)}
# 常见别名
for alias, idx in {
    "psalm": 18, "song of songs": 21, "songs": 21, "canticles": 21,
    "1 samuel": 8, "2 samuel": 9, "1 kings": 10, "2 kings": 11,
    "revelation of john": 65, "the revelation": 65, "apocalypse": 65,
    "acts of the apostles": 43, "john 1": None,
}.items():
    if idx is not None and alias not in EN2IDX:
        EN2IDX[alias] = idx

CJK = re.compile(r"[\u4e00-\u9fff]")


def book_index(raw):
    """把源数据里的书名解析成 0..65 的序号。"""
    if isinstance(raw, int):
        return raw if 0 <= raw < 66 else (raw - 1 if 1 <= raw <= 66 else -1)
    s = str(raw).strip()
    # 纯数字（1 基或 0 基）
    if s.isdigit():
        n = int(s)
        return n - 1 if n >= 1 else -1
    if s in BOOKS_ZH:
        return BOOKS_ZH.index(s)
    key = re.sub(r"\s+", " ", s.lower())
    key = key.replace("iii", "3").replace("ii", "2").replace("i ", "1 ")
    key = re.sub(r"^([1-3])\s*", r"\1 ", key)
    if key in EN2IDX:
        return EN2IDX[key]
    # 去掉 "the " 前缀再试
    if key.startswith("the ") and key[4:] in EN2IDX:
        return EN2IDX[key[4:]]
    # 前缀匹配
    for name, idx in EN2IDX.items():
        if key.startswith(name):
            return idx
    return -1


def normalize_text(t):
    """清理空白，统一标点，去掉残留的英文/数字编号。"""
    t = str(t)
    t = t.replace("\u3000", " ").replace("\xa0", " ")
    t = re.sub(r"\s+", "", t)                       # 中文正文去掉所有空白
    t = re.sub(r"^\s*\(\d+\)", "", t)               # 去掉 "(1)" 之类
    t = t.replace("“", "「").replace("”", "」")
    return t.strip()


def load_source():
    if not SRC.exists():
        sys.exit(f"[错误] 找不到源文件 {SRC}\n请先把 ChiUn.json 放到 _data/ 下。")
    raw = SRC.read_text(encoding="utf-8-sig", errors="replace")
    data = json.loads(raw)
    return data


def parse(data):
    """兼容多种结构：数组 / {books:[...]} / 嵌套 chapters / {resultset}。"""
    rows = []

    def emit(b, c, v, t):
        if isinstance(t, dict):                     # {"verse": n, "text": "..."}
            v = t.get("verse", t.get("v", v))
            t = t.get("text", t.get("t"))
        if b is None or c is None or v is None:
            return
        if t is None:
            return
        rows.append([b, int(c), int(v), str(t)])

    # 形式 A: [{book, chapter, verse, text}, ...]
    if isinstance(data, list):
        for it in data:
            if not isinstance(it, dict):
                continue
            b = it.get("book", it.get("book_name", it.get("b")))
            c = it.get("chapter", it.get("c"))
            v = it.get("verse", it.get("v"))
            t = it.get("text", it.get("t"))
            if t is None and isinstance(it.get("fields"), dict):
                t = it["fields"].get("text")
            emit(book_index(b), c, v, t)
        return rows

    if isinstance(data, dict):
        # 形式 B: {books: [{name, chapters: [[verse...]]}]}
        books = data.get("books") or data.get("bible")
        if isinstance(books, list):
            for bi, bk in enumerate(books):
                if not isinstance(bk, dict):
                    continue
                idx = book_index(bk.get("name") or bk.get("book") or bk.get("title"))
                if idx < 0:
                    idx = bi
                chs = bk.get("chapters") or []
                for ci, ch in enumerate(chs, start=1):
                    if isinstance(ch, list):
                        for vi, tx in enumerate(ch, start=1):
                            emit(idx, ci, vi, tx)
                    elif isinstance(ch, dict):
                        for vi, tx in enumerate(ch.get("verses", []), start=1):
                            emit(idx, ci, vi, tx)
            if rows:
                return rows
        # 形式 C: {"Book Chapter:Verse": "text"}
        for k, tx in data.items():
            m = re.match(r"^(.+?)\s+(\d+):(\d+)", k)
            if m:
                emit(book_index(m.group(1)), m.group(2), m.group(3), tx)
    return rows


def to_simplified_batch(rows):
    """整批繁转简：把 3 万节拼成一个字符串只转换一次，比逐节快一个数量级。"""
    cc = get_t2s()
    if cc is None:
        print("[警告] 未安装 opencc，将保留繁体原文。安装：pip install opencc-python-reimplemented")
        return rows
    sep = "\x00"
    try:
        joined = sep.join(r[3] for r in rows)
        parts = cc.convert(joined).split(sep)
        if len(parts) == len(rows):
            return [[r[0], r[1], r[2], t] for r, t in zip(rows, parts)]
        raise ValueError("split mismatch")
    except Exception:
        return [[r[0], r[1], r[2], cc.convert(r[3])] for r in rows]


def main():
    data = load_source()
    rows = parse(data)
    if not rows:
        sys.exit("[错误] 未能从源文件中解析出经文，请检查 ChiUn.json 结构。")
    rows = to_simplified_batch(rows)

    # 补齐书卷序号、去重、排序
    fixed = []
    for b, c, v, t in rows:
        if b is None or b < 0:
            continue
        t = normalize_text(t)
        if not t:
            continue
        fixed.append((b, c, v, t))
    seen = {}
    for b, c, v, t in fixed:
        seen[(b, c, v)] = t
    items = sorted(seen.items())

    verses = [[b, c, v, t] for (b, c, v), t in items]
    books_used = sorted({x[0] for x in verses})
    out = {
        "version": "和合本 (Chinese Union Version)",
        "books": [BOOKS_ZH[i] for i in books_used],
        "book_ids": books_used,
        "verses": [[b - books_used[0], c, v, t] for b, c, v, t in verses],
        "count": len(verses),
        "chapters": len({(b, c) for b, c, v, t in verses}),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    size_mb = OUT.stat().st_size / 1024 / 1024
    print(f"[完成] 共 {len(verses)} 节 / {out['chapters']} 章 / {len(books_used)} 卷")
    print(f"[完成] 输出 {OUT}  ({size_mb:.2f} MB)")
    print(f"[样例] {BOOKS_ZH[verses[0][0]]} {verses[0][1]}:{verses[0][2]}  {verses[0][3][:40]}")
    mid = verses[len(verses) // 2]
    print(f"[样例] {BOOKS_ZH[mid[0]]} {mid[1]}:{mid[2]}  {mid[3][:40]}")


if __name__ == "__main__":
    main()
