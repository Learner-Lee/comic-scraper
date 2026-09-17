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
PROXY = os.environ.get("COMIC_PROXY") or None  # 默认跟随环境变量
# 设为 1 则对所有站点强制直连。单个站点也可以自己声明 prefer_direct
# （manhuagui 就是这样：走翻墙代理会超时，直连才通）
NO_PROXY = os.environ.get("COMIC_NO_PROXY", "").strip() in ("1", "true", "yes")


class Job:
    """当前任务的状态。

    除 _cancel（Event 自带线程安全）外，所有字段读写都在 _lock 保护下。
    抢占任务槽一律走 try_begin，别在外面自己「先查后置」——那正是竞态的来源。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()  # 一次性信号，自身线程安全，不归 _lock 管
        self.reset()

    def reset(self) -> None:
        """清回空闲态。调用方负责持锁（try_begin 就是在锁内调的）。"""
        self.state = "idle"          # idle | running | done | error
        self.kind = ""               # download | pack
        self.log: deque[str] = deque(maxlen=600)
        self.done = 0
        self.total = 0
        self.out_dir = ""
        self.zip_path = ""
        self._cancel.clear()

    def try_begin(self, kind: str) -> bool:
        """原子地抢占任务槽：空闲才清状态并置 running，返回是否抢到。

        「判空闲」和「置 running」必须在同一个临界区里完成。分成两步的话，
        两个并发请求会双双通过检查，各起一个线程去踩同一份 JOB 状态。
        """
        with self._lock:
            if self.state == "running":
                return False
            self.reset()
            self.state = "running"
            self.kind = kind
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

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


JOB = Job()


def _safe_comic_dir(name: str) -> str:
    """把前端传来的漫画名解析成 DOWNLOAD_ROOT 内的真实路径，拒绝越界。"""
    candidate = os.path.abspath(os.path.join(DOWNLOAD_ROOT, os.path.basename(name)))
    root = DOWNLOAD_ROOT + os.sep
    if not candidate.startswith(root):
        raise scraper.ScrapeError("非法的目录名")
    return candidate


# ---------------------------------------------------------------- 后台任务

def _run_download(url: str, picked: set[int] | None,
                  workers: int, delay: float) -> None:
    try:
        site = scraper.pick_site(url)
        session = scraper.make_session(PROXY, site, NO_PROXY)
        comic = site.fetch_chapters(url, session)
        todo = [c for c in comic.chapters
                if picked is None or c.index in picked]

        out_dir = os.path.join(DOWNLOAD_ROOT, comic.name)
        os.makedirs(out_dir, exist_ok=True)

        with JOB._lock:
            JOB.total = len(todo)
            JOB.out_dir = out_dir
        JOB.say(f"[{site.name}] 《{comic.name}》共 {len(comic.chapters)} 章，"
                f"本次下载 {len(todo)} 章")

        for c in todo:
            if JOB.cancelled():
                JOB.say("已被用户取消")
                break
            try:
                ok, total = scraper.download_chapter(
                    c, out_dir, session, workers, delay, site)
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


def _run_pack(comic_dir: str, keep_source: bool, ext: str) -> None:
    try:
        stat = scraper.pack_chapters(comic_dir, delete_source=not keep_source,
                                     ext=ext, on_progress=JOB.say)
        with JOB._lock:
            JOB.zip_path = comic_dir
            JOB.state = "error" if stat["failed"] else "done"
    except Exception as exc:
        JOB.say(f"打包失败: {type(exc).__name__}: {exc}")
        with JOB._lock:
            JOB.state = "error"


def _start(kind: str, target, *args) -> bool:
    """抢到任务槽才起线程。抢不到返回 False，由调用方回 409。"""
    if not JOB.try_begin(kind):
        return False
    threading.Thread(target=target, args=args, daemon=True).start()
    return True


def _opt_num(data: dict, key: str, default, cast, lo, hi):
    """取前端传来的可选数值：留空或填得不对都退回默认值，并夹进合理区间。"""
    raw = str(data.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return min(max(cast(raw), lo), hi)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 路由

@app.route("/")
def index():
    return render_template("app.html", out_root=DOWNLOAD_ROOT,
                           def_workers=DEFAULT_WORKERS, def_delay=DEFAULT_DELAY,
                           sites=scraper.site_table())


@app.get("/api/sites")
def api_sites():
    """支持的站点清单。"""
    return jsonify({"sites": scraper.site_table()})


@app.post("/api/chapters")
def api_chapters():
    """第一步：抓目录，返回章节列表给前端勾选。"""
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "请填写漫画目录页 URL"}), 400
    # 贴错站点是用户输入问题，回 400；抓取本身失败才是上游问题，回 502
    try:
        site = scraper.pick_site(url)
    except scraper.ScrapeError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        comic = site.fetch_chapters(url, scraper.make_session(PROXY, site, NO_PROXY))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502
    return jsonify({
        "name": comic.name,
        "chapters": [{"index": c.index, "title": c.title} for c in comic.chapters],
    })


@app.post("/api/download")
def api_download():
    """第二步：投递后台下载任务，立即返回。"""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "请填写漫画目录页 URL"}), 400
    try:
        picked = scraper.parse_chapter_spec(data.get("chapters"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # 参数校验必须排在抢槽之前：校验失败不该把正在跑的任务状态清掉
    workers = _opt_num(data, "workers", DEFAULT_WORKERS, int, 1, 16)
    delay = _opt_num(data, "delay", DEFAULT_DELAY, float, 0.0, 5.0)
    if not _start("download", _run_download, url, picked, workers, delay):
        return jsonify({"error": "已有任务在跑，请等它结束或先取消"}), 409
    return jsonify({"ok": True})


@app.get("/api/progress")
def api_progress():
    return jsonify(JOB.snapshot())


@app.post("/api/cancel")
def api_cancel():
    JOB.cancel()
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
    data = request.get_json(silent=True) or {}
    try:
        comic_dir = _safe_comic_dir(data.get("name", ""))
    except scraper.ScrapeError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(comic_dir):
        return jsonify({"error": f"目录不存在: {comic_dir}"}), 404

    ext = ".cbz" if data.get("cbz") else ".zip"
    if not _start("pack", _run_pack, comic_dir, bool(data.get("keep")), ext):
        return jsonify({"error": "已有任务在跑"}), 409
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
    print(f"代理: {PROXY or ('强制直连' if NO_PROXY else '跟随环境变量')}")
    print(f"打开 http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
