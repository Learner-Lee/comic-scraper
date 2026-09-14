"""站点适配器：把「怎么拿章节列表」和「怎么拿图片直链」按站点隔离。

通用流程（下载、断点续传、打包、校验）与站点无关，都留在 scraper.py 里。
加新站点只需在本文件写一个 Site 子类并挂进 _SITES，别处一行都不用改。

公共数据类型（Chapter / Comic / ScrapeError / safe_name）也定义在这里，
因为它们正是站点解析的产物；scraper.py 会重新导出，外部照旧用 scraper.X 即可。
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

# Windows 文件名非法字符 + 控制字符
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_ABSOLUTE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.I)


class ScrapeError(RuntimeError):
    """抓取过程中的可预期错误。"""


@dataclass
class Chapter:
    url: str
    title: str
    index: int  # 在目录中的序号，从 1 开始


@dataclass
class Comic:
    name: str
    url: str
    chapters: list[Chapter] = field(default_factory=list)


def html_text(resp: requests.Response) -> str:
    """拿到正确解码的 HTML。

    响应头没带 charset 时，requests 会按 RFC 默认成 ISO-8859-1，中文站点
    因此整页变乱码——8comic 就是这样（它只回 `text/html`）。这种情况下
    改信页面自报的编码。头里带了 charset 的（如 manwame）保持原样。
    """
    if "charset" not in resp.headers.get("Content-Type", "").lower():
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def safe_name(name: str, fallback: str = "untitled") -> str:
    """把章节名/漫画名转成各平台都能用的目录名。"""
    cleaned = _ILLEGAL_RE.sub("", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or fallback


class Site:
    """站点适配器基类。子类只需实现下面两个 fetch_*。"""

    name = ""
    home = ""

    @staticmethod
    def matches(url: str) -> bool:
        raise NotImplementedError

    def headers(self) -> dict:
        """本站需要的默认请求头。"""
        return {}

    def fetch_chapters(self, book_url: str, session: requests.Session) -> Comic:
        raise NotImplementedError

    def fetch_chapter_images(self, chapter_url: str,
                             session: requests.Session) -> list[str]:
        raise NotImplementedError


# ------------------------------------------------------------------ manwame

class ManwameSite(Site):
    """manwame.com：章节目录直出 HTML，图片直链 AES-128-CBC 加密在 params 里。"""

    name = "manwame"
    home = "https://manwame.com"

    # 站点前端 cms.js 里硬编码的 AES-128 密钥。若某天全站解密失败，多半是这里变了：
    # 在章节页源码找 params，用浏览器控制台跑 CMS.chapter.decrypt(params) 对照即可。
    _AES_KEY = b"5V&RoR%Jf@pJPydF"
    _PARAMS_RE = re.compile(r"params\s*=\s*'([A-Za-z0-9+/=]+)'")

    @staticmethod
    def matches(url: str) -> bool:
        return urlparse(url).netloc.lower().endswith("manwame.com")

    def headers(self) -> dict:
        # 图片 CDN 强制校验 Referer，缺了会 403
        return {"Referer": self.home + "/"}

    def decrypt_params(self, blob: str) -> dict:
        """解开章节页 params：AES-128-CBC，IV 是密文前 16 字节。"""
        try:
            raw = base64.b64decode(blob)
            plain = unpad(
                AES.new(self._AES_KEY, AES.MODE_CBC, raw[:16]).decrypt(raw[16:]),
                AES.block_size,
            )
            return json.loads(plain.decode("utf-8"))
        except Exception as exc:
            raise ScrapeError(f"params 解密失败（站点密钥可能已更换）: {exc}") from exc

    def fetch_chapters(self, book_url: str, session: requests.Session) -> Comic:
        resp = session.get(book_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(html_text(resp), "html.parser")

        title_el = soup.select_one(".profile-text h1")
        name = safe_name(title_el.get_text(strip=True) if title_el else "", "comic")

        box = soup.select_one("[data-chapter-list]")
        if box is None:
            raise ScrapeError("目录页没找到 [data-chapter-list]，站点结构可能已改版")

        chapters = [
            Chapter(url=urljoin(self.home, a["href"]),
                    title=safe_name(a.get_text(strip=True), f"chapter_{i}"),
                    index=i)
            for i, a in enumerate(box.select("a[href]"), start=1)
        ]
        if not chapters:
            raise ScrapeError("目录里一章都没解析到")
        return Comic(name=name, url=book_url, chapters=chapters)

    def fetch_chapter_images(self, chapter_url: str,
                             session: requests.Session) -> list[str]:
        resp = session.get(chapter_url, timeout=20)
        resp.raise_for_status()

        m = self._PARAMS_RE.search(html_text(resp))
        if not m:
            raise ScrapeError(f"章节页没有 params: {chapter_url}")
        params = self.decrypt_params(m.group(1))

        if params.get("comic_status") == "down":
            raise ScrapeError("该漫画已下架")

        host = (params.get("images_domain")
                or next(iter(params.get("images_hosts") or []), "")
                or params.get("cdnurl") or "")

        return [item if _ABSOLUTE_RE.match(item)
                else f"{host.rstrip('/')}/{str(item).lstrip('/')}"
                for item in params.get("chapter_images") or []]


# ------------------------------------------------------------------ 8comic

class EightComicSite(Site):
    """8comic.com（無限動漫）。

    图片直链不加密，而是把参数塞进一个长字符串：每章一条定长记录，
    域名被拆成十六进制片段藏在串尾。算法取自站点 j.js 的 lc/su/nn/mm。

    两个容易踩的坑：
      * 章节页不带 Referer 会返回一个毫无数据的伪装页，**状态码仍是 200**
      * 目录页里有两个 ul.eps_list，第一个是空壳，真正的章节在第二个
    """

    name = "8comic"
    home = "https://www.8comic.com"

    # 章节页地址取自站点 comicview.js 的 cview()：非 VIP 会被送到这个域名
    _VIEW = "https://articles.onemoreplace.tw/online/new-{cid}.html?ch={ch}"

    # ---- 以下常量全部取自站点 j.js 与章节页内联脚本。站点改版就改这里 ----
    _REC = 47              # 每章一条记录的字符数
    _F_CH = (0, 2)         # 章节号（_lc 解码）
    _F_PAGES = (2, 2)      # 该章页数（_lc 解码）
    _F_CODE = (4, 40)      # 拼文件名用的 code
    _F_SD = (44, 2)        # 图床号 + 目录号（_lc 解码成两位数字）
    _F_PART = (46, 1)      # part 后缀，"0" 表示没有
    _FRAG_BASE = 47        # 尾部片段区：frag(n) 取 data[len-47-n*6 : +6]
    _FRAG_LEN = 6

    _AZ = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    # 数据串的变量名是站点构建时随机生成的（实测 wfp6_em7u4），不能依赖它，
    # 只能按「页面里最长的那个字母数字串」来认。
    _DATA_RE = re.compile(r"var\s+[A-Za-z_$][\w$]*\s*=\s*'([A-Za-z0-9]{200,})'")
    _CHS_RE = re.compile(r"var\s+chs\s*=\s*(\d+)")
    _CVIEW_RE = re.compile(r"cview\(\s*'(\d+)-([0-9a-zA-Z]+)\.html'")

    def __init__(self) -> None:
        # 同一部漫画的任意章节页，数据串完全相同且含全部章节，
        # 所以整部只需请求一次。缓存 key 是漫画 id。
        self._cache: dict[str, tuple[str, int]] = {}

    @staticmethod
    def matches(url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return (host.endswith("8comic.com") or host.endswith("comicvip.com")
                or host.endswith("onemoreplace.tw"))

    def headers(self) -> dict:
        return {"Referer": self.home + "/"}

    # ---- 站点 j.js 的四个解码函数 ----

    @classmethod
    def _lc(cls, s: str):
        """j.js 的 lc()：两字符的 52 进制编码；长度不为 2 的原样返回。"""
        if len(s) != 2:
            return s
        a, b = s[0], s[1]
        if a == "Z":
            return 8000 + cls._AZ.find(b)
        return cls._AZ.find(a) * 52 + cls._AZ.find(b)

    @staticmethod
    def _mm(p: int) -> int:
        """j.js 的 mm()：第 p 页在 code 里的取值偏移。"""
        return (p - 1) // 10 % 10 + ((p - 1) % 10) * 3

    @classmethod
    def _frag(cls, data: str, n: int) -> str:
        """域名片段被拆成十六进制藏在数据串尾部（站点的 p6of446）。"""
        start = len(data) - cls._FRAG_BASE - n * cls._FRAG_LEN
        chunk = data[start:start + cls._FRAG_LEN]
        try:
            return bytes.fromhex(chunk).decode("latin-1")
        except ValueError as exc:
            raise ScrapeError(f"域名片段解码失败（站点算法可能已改）: {chunk!r}") from exc

    # ---- 数据串 ----

    def _load_data(self, cid: str, session: requests.Session) -> tuple[str, int]:
        """取回并缓存某部漫画的章节数据串，返回 (数据串, 章节数)。"""
        if cid in self._cache:
            return self._cache[cid]

        url = self._VIEW.format(cid=cid, ch=1)
        # 这个 Referer 是必须的，不带会拿到一个 200 的伪装页
        resp = session.get(url, timeout=20,
                           headers={"Referer": f"{self.home}/html/{cid}.html"})
        resp.raise_for_status()
        html = html_text(resp)

        cands = self._DATA_RE.findall(html)
        if not cands:
            raise ScrapeError(
                "章节页没有图片数据串。最常见的原因是请求缺少 Referer——"
                "8comic 此时会返回一个状态码 200 的伪装页；也可能是站点已改版。")
        data = max(cands, key=len)

        m = self._CHS_RE.search(html)
        chs = int(m.group(1)) if m else len(data) // self._REC
        if chs < 1 or len(data) < chs * self._REC:
            raise ScrapeError(
                f"数据串长度 {len(data)} 放不下声明的 {chs} 章，站点算法可能已改")

        self._cache[cid] = (data, chs)
        return data, chs

    def _record(self, data: str, i: int) -> dict:
        """切出第 i 条（从 0 起）章节记录。"""
        rec = data[i * self._REC:(i + 1) * self._REC]
        cut = lambda f: rec[f[0]:f[0] + f[1]]
        return {
            "ch": self._lc(cut(self._F_CH)),
            "pages": self._lc(cut(self._F_PAGES)),
            "code": cut(self._F_CODE),
            "sd": str(self._lc(cut(self._F_SD))),
            "part": cut(self._F_PART),
        }

    def _build_urls(self, data: str, cid: str, rec: dict) -> list[str]:
        """按站点内联脚本的模板拼出整章的图片直链。"""
        f1, f2, f3, f4 = (self._frag(data, n) for n in (1, 2, 3, 4))
        sd = rec["sd"]
        if len(sd) < 2:
            raise ScrapeError(f"图床号异常: {sd!r}，站点算法可能已改")

        host = f"{f4}{sd[0]}.8{f3}{f2}{f3}"          # 例：img7.8comic.com
        suffix = "" if rec["part"] == "0" else rec["part"]
        code, pages = rec["code"], rec["pages"]
        if not isinstance(pages, int) or pages < 1:
            raise ScrapeError(f"页数异常: {pages!r}，站点算法可能已改")

        return [
            f"https://{host}/{sd[1]}/{cid}/{rec['ch']}{suffix}/"
            f"{p:03d}_{code[self._mm(p):self._mm(p) + 3]}.{f1}"
            for p in range(1, pages + 1)
        ]

    # ---- 对外接口 ----

    def fetch_chapters(self, book_url: str, session: requests.Session) -> Comic:
        resp = session.get(book_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(html_text(resp), "html.parser")

        name_el = soup.select_one('meta[name="name"]')
        name = safe_name(name_el.get("content", "") if name_el else "", "comic")

        cid_el = soup.select_one('meta[name="url"]')
        cid = (cid_el.get("content", "").strip() if cid_el else "")
        if not cid.isdigit():
            m = re.search(r"/html/(\d+)", book_url)
            cid = m.group(1) if m else ""
        if not cid:
            raise ScrapeError("目录页找不到漫画 id，站点结构可能已改版")

        chapters: list[Chapter] = []
        for a in soup.select("a.Ch[onclick]"):
            m = self._CVIEW_RE.search(a.get("onclick", ""))
            if not m:
                continue
            chapters.append(Chapter(
                url=self._VIEW.format(cid=m.group(1), ch=m.group(2)),
                title=safe_name(a.get_text(strip=True),
                                f"chapter_{len(chapters) + 1}"),
                index=len(chapters) + 1))
        if not chapters:
            raise ScrapeError("目录里一章都没解析到，站点结构可能已改版")
        return Comic(name=name, url=book_url, chapters=chapters)

    def fetch_chapter_images(self, chapter_url: str,
                             session: requests.Session) -> list[str]:
        m = re.search(r"new-(\d+)\.html", chapter_url)
        ch = (parse_qs(urlparse(chapter_url).query).get("ch") or ["1"])[0]
        if not m:
            raise ScrapeError(f"认不出的章节地址: {chapter_url}")
        cid = m.group(1)

        data, chs = self._load_data(cid, session)
        for i in range(chs):
            rec = self._record(data, i)
            if str(rec["ch"]) == str(ch):
                return self._build_urls(data, cid, rec)
        raise ScrapeError(f"数据串里找不到第 {ch} 章（共 {chs} 章）")


# ------------------------------------------------------------------ 路由

_SITES: tuple[type[Site], ...] = (ManwameSite, EightComicSite)


def pick_site(url: str) -> Site:
    """按 URL 选站点适配器。"""
    for cls in _SITES:
        if cls.matches(url):
            return cls()
    supported = "、".join(c.home for c in _SITES)
    raise ScrapeError(f"不支持的站点: {url}\n目前支持：{supported}")


def default_headers(site: Site | None = None) -> dict:
    h = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if site:
        h.update(site.headers())
    return h
