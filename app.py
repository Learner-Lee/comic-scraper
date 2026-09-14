"""极简 WebUI：贴 URL -> 选章节 -> 下载 -> 人工检查 -> 打包 ZIP。

设计要点：
  * 下载在后台线程跑，HTTP 请求立即返回，前端轮询进度（老版本 /scrape 会挂几小时）
  * 下载完成后停住，不自动打包；打包是单独一个按钮
  * 同一时间只允许一个任务，够用且不会互相踩
  * 无登录，因此默认只监听 127.0.0.1；要外网访问请自己套反代加认证

启动：python app.py            (默认 http://127.0.0.1:60001)
"""
from __future__ import annotations

import os
import threading
import traceback
from collections import deque
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import scraper

app = Flask(__name__)

DOWNLOAD_ROOT = os.path.abspath(os.environ.get("COMIC_OUT", "downloads"))
DEFAULT_WORKERS = int(os.environ.get("COMIC_WORKERS", "4"))
DEFAULT_DELAY = float(os.environ.get("COMIC_DELAY", "0.15"))
PROXY = os.environ.get("COMIC_PROXY") or None  # 默认直连


class Job:
    """当前任务的状态。所有字段读写都在 _lock 保护下。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.state = "idle"          # idle | running | done | error
        self.kind = ""               # download | pack
        self.log: deque[str] = deque(maxlen=600)
        self.done = 0
        self.total = 0
        self.out_dir = ""
        self.zip_path = ""
        self.cancel = False

    def say(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"[{stamp}] {msg}")
        print(msg, flush=True)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "kind": self.kind,
                "done": self.done,
                "total": self.total,
                "out_dir": self.out_dir,
                "zip_path": self.zip_path,
                "log": list(self.log),
            }

    def busy(self) -> bool:
        with self._lock:
            return self.state == "running"


JOB = Job()


def _safe_comic_dir(name: str) -> str:
    """把前端传来的漫画名解析成 DOWNLOAD_ROOT 内的真实路径，拒绝越界。"""
    candidate = os.path.abspath(os.path.join(DOWNLOAD_ROOT, os.path.basename(name)))
    root = DOWNLOAD_ROOT + os.sep
    if not candidate.startswith(root):
        raise scraper.ScrapeError("非法的目录名")
    return candidate


# ---------------------------------------------------------------- 后台任务

def _run_download(url: str, picked: set[int] | None) -> None:
    try:
        session = scraper.make_session(PROXY)
        comic = scraper.fetch_chapters(url, session)
        todo = [c for c in comic.chapters
                if picked is None or c.index in picked]

        out_dir = os.path.join(DOWNLOAD_ROOT, comic.name)
        os.makedirs(out_dir, exist_ok=True)

        with JOB._lock:
            JOB.total = len(todo)
            JOB.out_dir = out_dir
        JOB.say(f"《{comic.name}》共 {len(comic.chapters)} 章，本次下载 {len(todo)} 章")

        for c in todo:
            if JOB.cancel:
                JOB.say("已被用户取消")
                break
            try:
                ok, total = scraper.download_chapter(
                    c, out_dir, session, DEFAULT_WORKERS, DEFAULT_DELAY)
                flag = "OK" if ok == total else "部分失败"
                JOB.say(f"[{c.index}] {c.title} — {ok}/{total} {flag}")
            except scraper.ScrapeError as exc:
                JOB.say(f"[{c.index}] {c.title} — 跳过: {exc}")
            except Exception as exc:
                JOB.say(f"[{c.index}] {c.title} — 出错: {type(exc).__name__}: {exc}")
            with JOB._lock:
                JOB.done += 1

        JOB.say("下载结束。请到文件夹人工检查，确认无误后再点『打包 ZIP』")
        with JOB._lock:
            JOB.state = "done"
    except Exception as exc:
        JOB.say(f"任务失败: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        with JOB._lock:
            JOB.state = "error"


def _run_pack(comic_dir: str, keep_source: bool) -> None:
    try:
        stat = scraper.pack_chapters(comic_dir, delete_source=not keep_source,
                                     on_progress=JOB.say)
        with JOB._lock:
            JOB.zip_path = comic_dir
            JOB.state = "error" if stat["failed"] else "done"
    except Exception as exc:
        JOB.say(f"打包失败: {type(exc).__name__}: {exc}")
        with JOB._lock:
            JOB.state = "error"


def _start(kind: str, target, *args) -> None:
    JOB.reset()
    with JOB._lock:
        JOB.state = "running"
        JOB.kind = kind
    threading.Thread(target=target, args=args, daemon=True).start()


# ---------------------------------------------------------------- 路由

@app.route("/")
def index():
    return render_template("app.html", out_root=DOWNLOAD_ROOT)


@app.post("/api/chapters")
def api_chapters():
    """第一步：抓目录，返回章节列表给前端勾选。"""
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "请填写漫画目录页 URL"}), 400
    try:
        comic = scraper.fetch_chapters(url, scraper.make_session(PROXY))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502
    return jsonify({
        "name": comic.name,
        "chapters": [{"index": c.index, "title": c.title} for c in comic.chapters],
    })


@app.post("/api/download")
def api_download():
    """第二步：投递后台下载任务，立即返回。"""
    if JOB.busy():
        return jsonify({"error": "已有任务在跑，请等它结束或先取消"}), 409
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "请填写漫画目录页 URL"}), 400
    try:
        picked = scraper.parse_chapter_spec(data.get("chapters"))
    except ValueError:
        return jsonify({"error": "章节范围格式不对，示例：1-10,15"}), 400
    _start("download", _run_download, url, picked)
    return jsonify({"ok": True})


@app.get("/api/progress")
def api_progress():
    return jsonify(JOB.snapshot())


@app.post("/api/cancel")
def api_cancel():
    JOB.cancel = True
    return jsonify({"ok": True})


@app.get("/api/check")
def api_check():
    """第三步：列出已下载章节和图片数，供人工核对。"""
    name = request.args.get("name", "")
    try:
        comic_dir = _safe_comic_dir(name)
    except scraper.ScrapeError as exc:
        return jsonify({"error": str(exc)}), 400
    rows = scraper.scan_downloaded(comic_dir)
    return jsonify({
        "dir": comic_dir,
        "chapters": [{"name": n, "images": c, "packed": p} for n, c, p in rows],
        "total_images": sum(c for _, c, _ in rows),
        "unpacked": sum(1 for _, _, p in rows if not p),
    })


@app.post("/api/pack")
def api_pack():
    """第四步：人工确认后打包。源目录保留。"""
    if JOB.busy():
        return jsonify({"error": "已有任务在跑"}), 409
    data = request.get_json(silent=True) or {}
    try:
        comic_dir = _safe_comic_dir(data.get("name", ""))
    except scraper.ScrapeError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(comic_dir):
        return jsonify({"error": f"目录不存在: {comic_dir}"}), 404
    _start("pack", _run_pack, comic_dir, bool(data.get("keep")))
    return jsonify({"ok": True})


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。无登录保护，暴露到 0.0.0.0 前请自行加认证")
    ap.add_argument("--port", type=int, default=60001)
    ap.add_argument("--debug", action="store_true", help="仅本地调试用")
    args = ap.parse_args()

    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
    print(f"输出目录: {DOWNLOAD_ROOT}")
    print(f"代理: {PROXY or '直连'}")
    print(f"打开 http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
