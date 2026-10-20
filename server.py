#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
经意 · 圣经经文语义检索
======================
零依赖本地网站：静态页面 + 检索 API + 可选大模型增强。

用法:
    python server.py            # 默认 http://127.0.0.1:8765
    python server.py --port 9000 --open

"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pickle
import random
import socket
import sys
import time
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "verses.json"
PUBLIC = ROOT / "public"
CACHE = ROOT / ".cache"

sys.path.insert(0, str(ROOT))

ALLOW_REMOTE = False        # --lan 打开后允许局域网内其它设备访问
LOCAL_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

import llm as llm_mod                     # noqa: E402
from engine import VerseIndex             # noqa: E402

INDEX = None

# 每日/随机经文只从这些书卷里取，避免抽到族谱或律法细则
NICE_BOOKS = set(range(18, 22)) | set(range(39, 66))   # 诗篇~雅歌 + 新约
MIN_LEN, MAX_LEN = 14, 90

# 精选金句池（bookId 为 0 基：创世记=0，约翰福音=42）。加载索引后自动过滤掉不存在的条目。
GOLDEN = [
    (0, 1, 1), (18, 23, 1), (18, 23, 6), (18, 34, 18), (18, 37, 4), (18, 46, 1),
    (18, 46, 10), (18, 55, 22), (18, 119, 105), (18, 121, 1), (18, 139, 14),
    (19, 3, 5), (19, 3, 6), (19, 4, 23), (5, 1, 9),
    (22, 26, 3), (22, 40, 31), (22, 41, 10), (22, 43, 2),
    (23, 29, 11), (24, 3, 22), (24, 3, 23),
    (32, 6, 8), (32, 7, 18), (35, 3, 17), (20, 3, 11),
    (39, 5, 3), (39, 5, 14), (39, 5, 16), (39, 6, 33), (39, 6, 34),
    (39, 7, 7), (39, 11, 28), (39, 28, 19),
    (40, 11, 24), (41, 6, 31),
    (42, 1, 14), (42, 3, 16), (42, 8, 12), (42, 10, 10), (42, 13, 34),
    (42, 14, 6), (42, 14, 27), (42, 15, 5), (42, 16, 33),
    (43, 1, 8),
    (44, 5, 8), (44, 8, 1), (44, 8, 28), (44, 12, 2),
    (45, 10, 13), (45, 13, 4), (45, 13, 13), (46, 5, 17),
    (47, 2, 20), (47, 5, 22),
    (48, 2, 8), (48, 4, 2),
    (49, 1, 6), (49, 4, 6), (49, 4, 13),
    (50, 3, 13), (51, 5, 16),
    (54, 3, 16),
    (57, 4, 16), (57, 11, 1),
    (58, 1, 2), (58, 1, 5),
    (59, 1, 3), (59, 5, 7),
    (61, 1, 9), (61, 4, 8),
]

GOLDEN_VERSES = []


def build_golden():
    """索引就绪后把金句预解析成完整记录，避免每次请求线性扫全本。"""
    global GOLDEN_VERSES
    out = []
    for b, c, v in GOLDEN:
        got = INDEX.get(b, c, v)
        if got:
            out.append(got)
    GOLDEN_VERSES = out
    return out


# ------------------------------------------------------------------ 索引
def load_index() -> VerseIndex:
    global INDEX
    if INDEX is not None:
        return INDEX
    if not DATA.exists():
        sys.exit(
            f"[错误] 找不到 {DATA}\n"
            "请先运行:  python build_verses.py"
        )
    CACHE.mkdir(exist_ok=True)
    stamp = hashlib.md5(DATA.read_bytes()[:4096] + str(DATA.stat().st_size).encode()).hexdigest()[:12]
    cache_file = CACHE / f"index-{stamp}.pkl"
    if cache_file.exists():
        try:
            t0 = time.perf_counter()
            INDEX = pickle.loads(cache_file.read_bytes())
            print(f"[索引] 从缓存加载  {1000*(time.perf_counter()-t0):.0f} ms")
            return INDEX
        except Exception as e:      # 缓存损坏就重建
            print(f"[索引] 缓存失效（{e}），正在重建…")
    print("[索引] 正在重建倒排索引，首次启动约需几秒…")
    INDEX = VerseIndex(DATA)
    try:
        cache_file.write_bytes(pickle.dumps(INDEX, protocol=4))
    except Exception:
        pass
    print(f"[索引] 完成  {INDEX.build_ms:.0f} ms  "
          f"({INDEX.n_docs} 小句 / {len(INDEX.post)} 词条)")
    return INDEX


# ------------------------------------------------------------- 精选经文
def pick_verse(seed_text: str):
    idx = INDEX
    rnd = random.Random(seed_text)
    for _ in range(400):
        vi = rnd.randrange(len(idx.verses))
        row = idx.verses[vi]
        b = row[0]
        book_id = idx.book_ids[b] if b < len(idx.book_ids) else b
        if book_id not in NICE_BOOKS:
            continue
        t = row[3]
        if not (MIN_LEN <= len(t) <= MAX_LEN):
            continue
        book = idx.books[b]
        return {
            "book": book, "bookId": book_id, "chapter": row[1], "verse": row[2],
            "ref": f"{book} {row[1]}:{row[2]}", "text": t,
        }
    return None


# ------------------------------------------------------------------ 检索
def merge_results(batches, topk=30):
    """合并多路检索结果，命中多路的经文加分。"""
    merged = {}
    for results in batches:
        for rank, r in enumerate(results, start=1):
            key = r["id"]
            bonus = 1.0 / (1 + 0.12 * rank)
            cur = merged.get(key)
            if cur is None:
                rr = dict(r)
                rr["_acc"] = r["rel"] * bonus
                rr["_hits"] = 1
                merged[key] = rr
            else:
                cur["_acc"] = max(cur["_acc"], r["rel"] * bonus) + 0.35 * r["rel"] * bonus
                cur["_hits"] += 1
    out = list(merged.values())
    for r in out:
        r["_acc"] *= 1 + 0.18 * (r["_hits"] - 1)
    out.sort(key=lambda x: -x["_acc"])
    return out[:topk]


def do_search(q: str, mode: str, cfg, limit: int):
    idx = INDEX
    warn = None
    analysis = ""
    themes = []
    queries = [q]

    if mode == "deep" and cfg and cfg.get("apiKey"):
        try:
            ex = llm_mod.expand(cfg, q)
            analysis = ex["analysis"]
            themes = ex["themes"]
            kws = [k for k in ex["keywords"] if k and k != q]
            queries = [q] + kws[:6]
        except llm_mod.LLMError as e:
            warn = f"AI 理解失败，已回退到本地检索：{e}"
        except Exception as e:
            warn = f"AI 理解失败，已回退到本地检索：{e}"

    batches = []
    concepts_all = []
    for query in queries:
        res, concepts, _ = idx.search(query, topk=24)
        if res:
            batches.append(res)
        if len(concepts_all) < 6:
            for c in concepts:
                if c not in concepts_all:
                    concepts_all.append(c)

    if not batches:
        return {
            "query": q, "results": [], "concepts": [], "analysis": analysis,
            "themes": themes, "mode": mode, "warning": warn,
        }

    merged = merge_results(batches, topk=max(limit * 2, 26))

    # 大模型重排
    if mode == "deep" and cfg and cfg.get("apiKey") and not warn:
        cands = merged[:24]
        try:
            ranking = llm_mod.rerank(cfg, q, cands, keep=limit)
            if ranking:
                final = []
                for item in ranking:
                    r = dict(cands[item["index"]])
                    r["rel"] = round(item["score"] * 100, 1)
                    r["reason"] = item["reason"]
                    r["ai"] = True
                    final.append(r)
                return {
                    "query": q, "results": final, "concepts": concepts_all,
                    "analysis": analysis, "themes": themes,
                    "mode": "deep", "warning": None,
                    "keywords": idx.expand_words(q, 14) or queries[1:],
                }
            warn = "AI 未能给出有效排序，已回退到本地结果"
        except llm_mod.LLMError as e:
            warn = f"AI 重排失败，已回退到本地结果：{e}"
        except Exception as e:
            warn = f"AI 重排失败，已回退到本地结果：{e}"

    # 本地结果：按合并分重算相关度
    top = merged[:limit]
    hi = top[0]["_acc"] or 1.0
    for r in top:
        r["rel"] = round(100 * (r["_acc"] / hi) ** 0.6, 1)
    for r in top:
        r.pop("_acc", None)
        r.pop("_hits", None)

    return {
        "query": q, "results": top, "concepts": concepts_all,
        "analysis": analysis, "themes": themes,
        "mode": "local" if mode == "deep" else mode,
        "warning": warn, "keywords": queries[1:] or idx.expand_words(q, 14),
    }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Jingyi/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具 ----
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg, code=400, extra=None):
        payload = {"error": msg}
        if extra:
            payload.update(extra)
        self._json(payload, code)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        # 只打印 API 请求，静态资源静默
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _allow(self):
        """默认只接受本机访问；--lan 时放开给局域网。"""
        if ALLOW_REMOTE:
            return True
        ip = self.client_address[0] if self.client_address else ""
        return ip in LOCAL_ADDRS or ip.startswith("127.")

    # ---- GET ----
    def do_GET(self):
        if not self._allow():
            self.send_error(403, "Forbidden")
            return
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        try:
            if path.startswith("/api/"):
                return self.api_get(path, qs)
            return self.serve_static(path)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._err(f"服务器内部错误：{e}", 500)

    def api_get(self, path, qs):
        idx = INDEX
        if path == "/api/stats":
            st = idx.stats()
            st["indexCache"] = str(CACHE)
            return self._json(st)

        if path == "/api/random":
            if GOLDEN_VERSES:
                return self._json(random.choice(GOLDEN_VERSES))
            return self._json(pick_verse(str(random.random())) or {})

        if path == "/api/daily":
            if GOLDEN_VERSES:
                i = date.today().toordinal() % len(GOLDEN_VERSES)
                return self._json(GOLDEN_VERSES[i])
            return self._json(pick_verse(date.today().isoformat()) or {})

        if path == "/api/verse":
            try:
                b = int(qs.get("b", ["43"])[0])
                c = int(qs.get("c", ["3"])[0])
                v = int(qs.get("v", ["16"])[0])
            except ValueError:
                return self._err("参数 b/c/v 必须是整数")
            got = idx.get(b, c, v)
            if not got:
                return self._err("未找到该经文", 404)
            return self._json(got)

        if path == "/api/chapter":
            try:
                b = int(qs.get("b", ["43"])[0])
                c = int(qs.get("c", ["3"])[0])
            except ValueError:
                return self._err("参数 b/c 必须是整数")
            vs = idx.chapter(b, c)
            if not vs:
                return self._err("未找到该章", 404)
            name = ""
            for i, bid in enumerate(idx.book_ids):
                if bid == b:
                    name = idx.books[i]
                    break
            return self._json({"book": name, "bookId": b, "chapter": c, "verses": vs})

        if path == "/api/books":
            return self._json([
                {"id": bid, "name": name} for name, bid in zip(idx.books, idx.book_ids)
            ])

        return self._err("未知接口", 404)

    # ---- POST ----
    def do_POST(self):
        if not self._allow():
            self.send_error(403, "Forbidden")
            return
        u = urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/api/search":
                q = str(body.get("q") or "").strip()
                if not q:
                    return self._err("请输入要检索的语句")
                if len(q) > 600:
                    q = q[:600]
                mode = body.get("mode") or "fast"
                limit = int(body.get("limit") or 12)
                limit = max(3, min(30, limit))
                t0 = time.perf_counter()
                out = do_search(q, mode, body.get("llm") or {}, limit)
                out["took"] = round((time.perf_counter() - t0) * 1000, 1)
                return self._json(out)

            if u.path == "/api/test":
                cfg = body.get("llm") or {}
                if not cfg.get("apiKey"):
                    return self._err("请先填写 API Key", 401)
                t0 = time.perf_counter()
                reply = llm_mod.ping(cfg)
                return self._json({
                    "ok": True,
                    "model": cfg.get("model") or llm_mod.DEFAULT_MODEL,
                    "baseUrl": cfg.get("baseUrl") or llm_mod.DEFAULT_BASE,
                    "ms": round((time.perf_counter() - t0) * 1000),
                    "reply": reply,
                })

            if u.path == "/api/explain":
                cfg = body.get("llm") or {}
                if not cfg.get("apiKey"):
                    return self._err("请先在设置里填写 API Key", 401)
                ref = str(body.get("ref") or "")
                text = str(body.get("text") or "")
                q = str(body.get("q") or "")
                if not text:
                    return self._err("缺少经文内容")
                md = llm_mod.explain(cfg, ref, text, q)
                return self._json({"ref": ref, "content": md})

            return self._err("未知接口", 404)
        except llm_mod.LLMError as e:
            return self._err(str(e), 502)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._err(f"服务器内部错误：{e}", 500)

    # ---- 静态文件 ----
    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        target = (PUBLIC / rel).resolve()
        try:
            target.relative_to(PUBLIC.resolve())
        except ValueError:
            return self._send(403, b"Forbidden", "text/plain; charset=utf-8")
        if not target.is_file():
            return self._send(404, b"Not Found", "text/plain; charset=utf-8")
        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix in (".html", ".css", ".json", ".svg"):
            ctype = (ctype or "text/plain").split(";")[0] + "; charset=utf-8"
        data = target.read_bytes()
        extra = {}
        if target.suffix in (".css", ".js", ".svg", ".png", ".woff2"):
            extra["Cache-Control"] = "public, max-age=3600"
        self._send(200, data, ctype or "application/octet-stream", extra)


class DualStackServer(ThreadingHTTPServer):
    """同时监听 IPv4 与 IPv6。

    Windows 上 localhost 常常优先解析成 ::1，只绑 127.0.0.1 的话浏览器会
    直接「拒绝连接」——这是「服务明明在跑却打不开」最常见的原因。
    """
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def bind_any(port):
    """优先双栈，IPv6 不可用时退回 IPv4。"""
    try:
        return DualStackServer(("::", port), Handler)
    except OSError:
        return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def main():
    global ALLOW_REMOTE

    ap = argparse.ArgumentParser(description="经意 · 圣经经文语义检索")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--host", default="", help="留空表示同时监听 0.0.0.0 与 ::")
    ap.add_argument("--lan", action="store_true", help="允许局域网内其它设备访问")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    ALLOW_REMOTE = bool(args.lan)

    load_index()
    gold = build_golden()
    if gold:
        print(f"[金句] 预载 {len(gold)} 条精选经文")

    port = args.port
    httpd = None
    last_err = None
    for _ in range(12):
        try:
            httpd = (ThreadingHTTPServer((args.host, port), Handler) if args.host
                     else bind_any(port))
            break
        except OSError as e:
            last_err = e
            port += 1
    if httpd is None:
        sys.exit(f"[错误] 找不到可用端口：{last_err}")

    url = f"http://127.0.0.1:{port}/"
    print("")
    print("  ┌─────────────────────────────────────────┐")
    print("  │   经意 · 圣经经文语义检索                │")
    print("  └─────────────────────────────────────────┘")
    st = INDEX.stats()
    print(f"  经文   {st['verses']} 节 / {st['books']} 卷")
    print(f"  索引   {st['sentences']} 小句 / {st['terms']} 词条")
    print("")
    print(f"  请用浏览器打开   {url}")
    print(f"                  http://localhost:{port}/")
    ip = lan_ip()
    if ip and ALLOW_REMOTE:
        print(f"  局域网访问       http://{ip}:{port}/")
    elif ip:
        print(f"  局域网           {ip}:{port}（当前仅限本机，加 --lan 放开）")
    print("")
    print("  Ctrl+C 停止")
    print("")

    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.server_close()


if __name__ == "__main__":
    main()
