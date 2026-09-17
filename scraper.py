"""漫画抓取核心模块：通用流程。

    1. pick_site(url)                -> 按 URL 选出站点适配器
    2. site.fetch_chapters(...)      -> 目录页解析出章节列表
    3. site.fetch_chapter_images(..) -> 章节页解出图片直链
    4. download_comic(...)           -> 按章节并发下载，支持断点续传

站点相关的解析都在 sites.py，本文件只管与站点无关的事：下载、断点续传、
原子落盘、打包、校验、人工检查。加新站点不用动这里。

无需 Selenium。依赖：requests, beautifulsoup4, pycryptodome
"""
from __future__ import annotations

import os
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sites
# 重新导出，让外部（app.py 等）继续用 scraper.X
from sites import (Chapter, Comic, ScrapeError, Site, all_sites,
                   describe_sites, pick_site, safe_name, site_table)

__all__ = [
    "Chapter", "Comic", "ScrapeError", "Site", "pick_site", "safe_name",
    "make_session", "fetch_chapters", "fetch_chapter_images",
    "download_chapter", "download_comic", "probe", "parse_chapter_spec",
    "scan_downloaded", "pack_chapters", "all_sites", "describe_sites",
    "site_table",
]

_EXT_BY_TYPE = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def make_session(proxy: str | None = None,
                 site: sites.Site | None = None) -> requests.Session:
    """带自动重试的 Session。proxy 传 None 即直连（默认就够用）。

    site 决定额外的请求头——各站要求不同：manwame 的图片 CDN 强制校验
    Referer，8comic 的章节页不带 Referer 会返回伪装页。
    """
    s = requests.Session()
    s.headers.update(sites.default_headers(site))
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def fetch_chapters(book_url: str, session: requests.Session,
                   site: sites.Site | None = None) -> Comic:
    """第一步：从目录页取出全部章节。site 省略时按 URL 自动识别。"""
    return (site or pick_site(book_url)).fetch_chapters(book_url, session)


def fetch_chapter_images(chapter_url: str, session: requests.Session,
                         site: sites.Site | None = None) -> list[str]:
    """第二步：解析章节页，拿到图片直链。

    注意：8comic 的适配器会缓存整部漫画的数据，所以整部下载时应复用同一个
    site 实例，别每章新建一个——那样会白白多请求 N 次。
    """
    return (site or pick_site(chapter_url)).fetch_chapter_images(
        chapter_url, session)


def _download_one(url: str, dest_dir: str, idx: int,
                  session: requests.Session, delay: float) -> str | None:
    """下载单张图；已存在则跳过（断点续传）。返回文件路径，失败返回 None。"""
    stem = os.path.join(dest_dir, f"{idx:04d}")
    for ext in _EXT_BY_TYPE.values():
        if os.path.exists(stem + ext):
            return stem + ext

    try:
        resp = session.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        ext = _EXT_BY_TYPE.get(
            resp.headers.get("Content-Type", "").split(";")[0].strip(), ".webp")
        tmp = stem + ext + ".part"
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
        os.replace(tmp, stem + ext)  # 原子落盘，中断不会留半张图
        if delay:
            time.sleep(delay)
        return stem + ext
    except Exception as exc:
        print(f"  [失败] 第 {idx} 张: {type(exc).__name__}: {exc}")
        return None


def download_chapter(chapter: Chapter, out_dir: str, session: requests.Session,
                     workers: int = 4, delay: float = 0.15,
                     site: sites.Site | None = None) -> tuple[int, int]:
    """下载一章，返回 (成功数, 总数)。"""
    urls = fetch_chapter_images(chapter.url, session, site)
    dest = os.path.join(out_dir, f"{chapter.index:03d} {chapter.title}")
    os.makedirs(dest, exist_ok=True)

    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_download_one, u, dest, i, session, delay)
                   for i, u in enumerate(urls, start=1)]
        for fut in as_completed(futures):
            if fut.result():
                ok += 1
    return ok, len(urls)


def download_comic(book_url: str, out_root: str = "downloads",
                   chapters: Iterable[int] | None = None,
                   workers: int = 4, delay: float = 0.15,
                   proxy: str | None = None,
                   on_progress: Callable[[str], None] = print) -> str:
    """完整流程。chapters 传章节序号集合可只下指定章；None 表示全下。

    on_progress 是进度回调，接 WebUI 时换成往队列里塞消息即可。
    """
    site = pick_site(book_url)
    session = make_session(proxy, site)
    comic = site.fetch_chapters(book_url, session)
    out_dir = os.path.join(out_root, comic.name)
    os.makedirs(out_dir, exist_ok=True)

    todo = [c for c in comic.chapters
            if chapters is None or c.index in set(chapters)]
    on_progress(f"[{site.name}] 《{comic.name}》共 {len(comic.chapters)} 章，"
                f"本次下载 {len(todo)} 章")

    for c in todo:
        try:
            ok, total = download_chapter(c, out_dir, session, workers, delay, site)
            flag = "OK" if ok == total else "部分失败"
            on_progress(f"[{c.index}/{len(comic.chapters)}] {c.title} — {ok}/{total} {flag}")
        except ScrapeError as exc:
            on_progress(f"[{c.index}/{len(comic.chapters)}] {c.title} — 跳过: {exc}")

    on_progress(f"完成，输出目录: {out_dir}")
    return out_dir


def _reachable(session: requests.Session, url: str):
    """只看响应头就够了，不下载正文。返回状态码，异常则返回异常名。"""
    try:
        resp = session.get(url, timeout=20, stream=True)
        resp.close()
        return resp.status_code
    except Exception as exc:
        return type(exc).__name__


def probe(book_url: str, proxy: str | None = None, samples: int = 3,
          on_progress: Callable[[str], None] = print) -> bool:
    """下载前预检：解析布局、抽样生成图片地址并实际请求，确认这部能下。

    存在的理由：8comic 的章节脚本是随机混淆的，字段布局每次重新生成都会变。
    布局认错不会当场失败，只会生成一堆 404——那种错要下到一半才发现。
    预检把它提前到几秒钟内，并把认出来的布局打出来，出问题时就是最直接的线索。
    """
    site = pick_site(book_url)
    session = make_session(proxy, site)

    comic = site.fetch_chapters(book_url, session)
    on_progress(f"[{site.name}] 《{comic.name}》 {len(comic.chapters)} 章")
    for line in site.diagnose(comic, session):
        on_progress(f"  {line}")

    n = len(comic.chapters)
    picks = sorted({0, n // 2, n - 1})[:max(1, samples)]
    on_progress("  抽查图片可达性：")

    all_ok = True
    for i in picks:
        c = comic.chapters[i]
        try:
            urls = site.fetch_chapter_images(c.url, session)
        except ScrapeError as exc:
            on_progress(f"    ✗ {c.title} — {exc}")
            all_ok = False
            continue
        if not urls:
            on_progress(f"    ✗ {c.title} — 一张图都没解析到")
            all_ok = False
            continue

        # 首末页各验一个：中间错位的话末页几乎必然也错
        probe_urls = [urls[0]] if len(urls) == 1 else [urls[0], urls[-1]]
        codes = [_reachable(session, u) for u in probe_urls]
        good = all(x == 200 for x in codes)
        all_ok = all_ok and good
        on_progress(f"    {'✓' if good else '✗'} {c.title} — {len(urls)} 页，"
                    f"首末页 HTTP {'/'.join(str(x) for x in codes)}")

    on_progress("  ✓ 预检通过，可以下载" if all_ok else
                "  ✗ 预检未通过，先别下——上面第一个 ✗ 就是原因")
    return all_ok


def parse_chapter_spec(spec: str | None) -> set[int] | None:
    """把 '1-10,15,20-22' 解析成序号集合。None / 空 表示全选。

    解析不了就抛带说明的 ValueError——别让 int() 的裸异常一路冒到命令行去，
    用户看到的应该是「哪里填错了」，不是一屏堆栈。
    """
    if not spec or not spec.strip():
        return None

    def _num(text: str) -> int:
        text = text.strip()
        if not text:
            raise ValueError(f"『{spec.strip()}』里有个数字漏了。格式示例：1-10,15")
        try:
            n = int(text)
        except ValueError:
            raise ValueError(f"『{text}』不是数字。格式示例：1-10,15") from None
        if n < 1:
            raise ValueError(f"章节序号从 1 开始，不能是 {n}")
        return n

    picked: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (_num(x) for x in part.split("-", 1))
            if a > b:
                raise ValueError(f"区间头尾反了：{part}")
            picked.update(range(a, b + 1))
        else:
            picked.add(_num(part))
    return picked or None


def _chapter_files(chapter_dir: str) -> list[str]:
    """章节目录里的有效图片文件。

    排除未完成的 .part，以及 .DS_Store 之类的隐藏文件——后者是系统随时会生成的，
    放进压缩包既没意义，还会让"打包前后文件数"对不上。
    """
    return sorted(
        os.path.join(chapter_dir, f)
        for f in os.listdir(chapter_dir)
        if not f.endswith(".part") and not f.startswith(".")
        and os.path.isfile(os.path.join(chapter_dir, f))
    )


def _remove_tree(path: str, attempts: int = 4) -> bool:
    """删除目录树，失败返回 False 而不抛异常。

    Tuxera NTFS / 网络盘上 rmtree 会偶发 Errno 66「Directory not empty」——文件其实
    已删掉，只是紧接着的 rmdir 赶在文件系统状态刷新之前。重试几次基本都能过。
    删不掉也不算致命：zip 此时已校验通过，源目录只是残留垃圾。
    """
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if i == attempts - 1:
                return False
            time.sleep(0.4 * (i + 1))
    return False


def scan_downloaded(comic_dir: str) -> list[tuple[str, int, bool]]:
    """列出章节和各自图片数，供人工检查用。

    返回 (名称, 图片数, 是否已打包)。打包后章节变成 zip，这里一并统计，
    所以 check 在打包前后都能用。
    """
    if not os.path.isdir(comic_dir):
        return []
    out = []
    for entry in sorted(os.listdir(comic_dir)):
        path = os.path.join(comic_dir, entry)
        if os.path.isdir(path):
            out.append((entry, len(_chapter_files(path)), False))
        elif entry.lower().endswith((".zip", ".cbz")):
            try:
                with zipfile.ZipFile(path) as zf:
                    n = len(zf.namelist())
            except zipfile.BadZipFile:
                n = -1  # 损坏
            out.append((os.path.splitext(entry)[0], n, True))
    return out


def _zip_one_chapter(chapter_dir: str, zip_path: str) -> int:
    """把单个章节目录打包并校验。校验不过会抛异常且不留残件。返回打包文件数。"""
    files = _chapter_files(chapter_dir)
    if not files:
        raise ScrapeError(f"章节目录是空的: {chapter_dir}")

    tmp = zip_path + ".part"
    try:
        # webp 已是压缩格式，deflate 实测反而大 0.1%，所以直接 STORED 不耗 CPU
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            for fp in files:
                zf.write(fp, os.path.basename(fp))

        # 删源目录前必须验：条目数对得上，且每条 CRC 校验通过
        with zipfile.ZipFile(tmp) as zf:
            names = zf.namelist()
            if len(names) != len(files):
                raise ScrapeError(
                    f"校验失败，zip 内 {len(names)} 项但源有 {len(files)} 个文件")
            bad = zf.testzip()
            if bad is not None:
                raise ScrapeError(f"校验失败，损坏条目: {bad}")

        os.replace(tmp, zip_path)
        return len(files)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def pack_chapters(comic_dir: str, delete_source: bool = True, ext: str = ".zip",
                  on_progress: Callable[[str], None] = print) -> dict:
    """把漫画目录里的每个章节各自打包成一个 zip，漫画目录本身仍是目录。

        downloads/漫画名/001 第1话/*.webp   ->   downloads/漫画名/001 第1话.zip

    delete_source=True 时，每章 zip 校验通过后才删除对应源目录——空间正是这样
    省下来的（不再有源文件和压缩包并存的整部副本）。校验不过就保留源目录。

    可重复运行：已存在同名 zip 的章节会跳过，中途失败重跑即可继续。
    """
    comic_dir = os.path.abspath(comic_dir)
    if not os.path.isdir(comic_dir):
        raise ScrapeError(f"目录不存在: {comic_dir}")

    entries = sorted(os.listdir(comic_dir))
    chapters = [d for d in entries if os.path.isdir(os.path.join(comic_dir, d))]
    if not chapters:
        # 已全部打包是正常状态，不当错误处理，重跑本命令应当安静地什么都不做
        n_zip = sum(1 for e in entries if e.lower().endswith((".zip", ".cbz")))
        if n_zip:
            on_progress(f"全部 {n_zip} 章都已打包，无需操作")
            return {"packed": 0, "skipped": n_zip, "failed": 0, "images": 0}
        raise ScrapeError(f"目录里没有任何章节: {comic_dir}")

    packed, skipped, failed, total_files = 0, 0, 0, 0
    undeleted: list[str] = []
    on_progress(f"共 {len(chapters)} 个章节目录待打包")

    for i, name in enumerate(chapters, start=1):
        src = os.path.join(comic_dir, name)
        dst = os.path.join(comic_dir, name + ext)

        if os.path.exists(dst):
            # zip 已存在说明上轮已打包成功，残留的源目录可以直接清掉
            if delete_source and not _remove_tree(src):
                undeleted.append(src)
            on_progress(f"  [{i}/{len(chapters)}] {name} — 已打包过，跳过")
            skipped += 1
            continue

        try:
            n = _zip_one_chapter(src, dst)
        except Exception as exc:
            on_progress(f"  [{i}/{len(chapters)}] {name} — 打包失败，源目录保留: {exc}")
            failed += 1
            continue

        # 走到这里 zip 已校验通过，源目录删不掉也不影响数据完整性，不能让它中断整轮
        note = ""
        if delete_source and not _remove_tree(src):
            undeleted.append(src)
            note = "（zip 已校验通过，但源目录删除失败）"
        elif not delete_source:
            note = "（源目录保留）"

        packed += 1
        total_files += n
        on_progress(f"  [{i}/{len(chapters)}] {name} — {n} 张 OK{note}")

    on_progress(f"打包结束：成功 {packed}，跳过 {skipped}，失败 {failed}，"
                f"共 {total_files} 张图")
    if failed:
        on_progress("失败的章节源目录未删除，修复后重跑本命令即可续做")
    if undeleted:
        on_progress(f"有 {len(undeleted)} 个源目录没删掉（zip 都是好的），"
                    f"重跑本命令会再试一次")
    return {"packed": packed, "skipped": skipped, "failed": failed,
            "images": total_files, "undeleted": undeleted}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="manwame.com 漫画下载")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出章节，不下载")
    p_list.add_argument("url", help="漫画目录页 URL")
    p_list.add_argument("--proxy", help="代理地址（默认直连）")

    p_dl = sub.add_parser("download", help="下载章节到文件夹（不自动打包）")
    p_dl.add_argument("url", help="漫画目录页 URL")
    p_dl.add_argument("-o", "--out", default="downloads", help="输出根目录")
    p_dl.add_argument("-c", "--chapters", help="章节范围，如 1-10 或 1,3,5；省略为全部")
    p_dl.add_argument("-w", "--workers", type=int, default=4, help="每章并发下载数")
    p_dl.add_argument("-d", "--delay", type=float, default=0.15, help="每张图后的间隔秒数")
    p_dl.add_argument("--proxy", help="代理地址（默认直连）")

    sub.add_parser("sites", help="列出支持的站点和 URL 形态")

    p_prb = sub.add_parser("probe", help="下载前预检：确认这部漫画现在能正常下")
    p_prb.add_argument("url", help="漫画目录页 URL")
    p_prb.add_argument("-n", "--samples", type=int, default=3, help="抽查几章")
    p_prb.add_argument("--proxy", help="代理地址（默认直连）")

    p_chk = sub.add_parser("check", help="列出已下载的章节和图片数，供人工核对")
    p_chk.add_argument("dir", help="漫画目录，如 downloads/<漫画名>")

    p_pack = sub.add_parser("pack", help="人工检查后，把每个章节各自打包成 zip")
    p_pack.add_argument("dir", help="漫画目录，如 downloads/<漫画名>")
    p_pack.add_argument("--keep", action="store_true",
                        help="打包后保留源章节目录（默认校验通过即删除，这样才省空间）")
    p_pack.add_argument("--cbz", action="store_true",
                        help="用 .cbz 扩展名，便于 Tachiyomi/Komga 等阅读器识别")

    args = ap.parse_args()

    try:
        if args.cmd == "list":
            site = pick_site(args.url)
            comic = site.fetch_chapters(args.url, make_session(args.proxy, site))
            print(f"[{site.name}] 《{comic.name}》 {len(comic.chapters)} 章")
            for c in comic.chapters:
                print(f"  {c.index:3d}  {c.title}")

        elif args.cmd == "download":
            out_dir = download_comic(args.url, args.out,
                                     parse_chapter_spec(args.chapters),
                                     args.workers, args.delay, args.proxy)
            print(f"\n请人工检查 {out_dir}，确认无误后运行：")
            print(f"  python scraper.py pack \"{out_dir}\"")

        elif args.cmd == "sites":
            for line in describe_sites():
                print(line)

        elif args.cmd == "probe":
            raise SystemExit(0 if probe(args.url, args.proxy, args.samples) else 1)

        elif args.cmd == "check":
            rows = scan_downloaded(args.dir)
            if not rows:
                print(f"{args.dir} 下没有章节")
            for name, n, packed in rows:
                tag = "zip" if packed else "目录"
                count = "损坏" if n < 0 else f"{n:4d} 张"
                print(f"  {count}  [{tag}]  {name}")
            n_packed = sum(1 for _, _, p in rows if p)
            print(f"\n共 {len(rows)} 章（已打包 {n_packed}，"
                  f"未打包 {len(rows) - n_packed}），"
                  f"{sum(n for _, n, _ in rows if n > 0)} 张图")

        elif args.cmd == "pack":
            stat = pack_chapters(args.dir, delete_source=not args.keep,
                                 ext=".cbz" if args.cbz else ".zip")
            raise SystemExit(1 if stat["failed"] else 0)

    except ScrapeError as exc:
        raise SystemExit(f"错误: {exc}")
    except ValueError as exc:
        raise SystemExit(f"参数错误: {exc}")
    except KeyboardInterrupt:
        raise SystemExit("\n已中断。已完成的部分保留，重跑同一命令可继续。")
