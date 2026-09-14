# Plan：支持 8comic.com

**日期**：2026-09-14
**目标**：让本工具同时支持 manwame.com 和 8comic.com，命令行与 WebUI 用法不变，贴哪个站的 URL 自动识别。

---

## 一、核心判断：差异只有两处

通读两站后的结论——**它们的区别只在两件事上**：

1. 怎么从目录页拿到章节列表
2. 怎么从章节页拿到图片直链

而下载、断点续传、原子落盘、打包、CRC 校验、人工检查、CLI、WebUI **全部与站点无关**。

所以正确的抽象是「站点适配器」：把上面两件事收进适配器，通用流程一行都不用改。
这也意味着将来加第三个站点时，只需再写一个适配器。

---

## 二、8comic 实地调研结论

全部经实测验证，不是推测。

### 2.1 目录页 `https://www.8comic.com/html/<id>.html`

| 项 | 取法 | 实测值 |
|---|---|---|
| 漫画名 | `<meta name="name" content="...">` | 唐三葬 |
| 漫画 id | `<meta name="url" content="...">` | 12539 |
| 章节列表 | `a.Ch` 的 onclick 中 `cview('<id>-<n>.html')` | 47 章 |
| 章节标题 | `a.Ch` 的文本 | 1話、2話… |

章节是**服务端直出**的，不需要浏览器内核。（页面里有两个 `ul.eps_list`，
第一个是空壳，真正的列表在第二个——只取第一个会一章都解析不到。）

### 2.2 章节页 `https://articles.onemoreplace.tw/online/new-<id>.html?ch=<n>`

地址来自站点 `comicview.js` 的 `cview()`：非 VIP 走 `articles.onemoreplace.tw`，
VIP 走 `/view/`。我们按非 VIP 处理。

**必须带 Referer**。这是 8comic 的反爬：不带 Referer 时服务器返回一个
完全不同的 142KB 伪装页面（WordPress 外观，无任何漫画数据），**HTTP 状态码仍是 200**。
带上 Referer 才返回 17KB 的真实章节页。这种「假装成功」最容易让爬虫静默跑空，
所以解析器必须显式检查数据串是否存在，拿不到就报错，不能当成空章节跳过。

### 2.3 图片直链的解码算法

章节数据在页面的一个长字符串里（实测 2292 字符），算法取自站点 `j.js`
的 `lc` / `su` / `nn` / `mm` 四个函数，已用 Node 执行原始 JS 得到标准答案后逐字段核对。

**每章一条记录，定长 47 字符**：

| 偏移 | 长度 | 含义 | 处理 |
|---|---|---|---|
| 0 | 2 | 章节号 | `lc()` 解码 |
| 2 | 2 | 该章页数 | `lc()` 解码 |
| 4 | 40 | code（拼文件名用） | 原样 |
| 44 | 2 | 图床号 + 目录号 | `lc()` 解码成数字，取两位 |
| 46 | 1 | part 后缀 | `"0"` 表示无 |

**尾部另有片段区**：`frag(n)` = 取 `data[len-47-n*6 : +6]` 作十六进制解码，
得到 `frag(1)="jpg"`、`frag(2)="ic."`、`frag(3)="com"`、`frag(4)="img"`。
域名被拆成碎片藏在串尾，就是为了避免被直接搜索到。

**URL 拼装**：

```
https://{frag4}{sd[0]}.8{frag3}{frag2}{frag3}/{sd[1]}/{id}/{ch}{part}/{nnn}_{code[mm(j):mm(j)+3]}.{frag1}
```

其中 `nnn` 是页码补足三位；`mm(p) = (p-1)//10 % 10 + ((p-1)%10)*3`。

实测产出 `https://img7.8comic.com/3/12539/1/001_P65.jpg`，
第 1 章全部 6 页 + 第 2 章抽查 3 页**均返回 HTTP 200**，约 150–230KB 的 JPEG。

### 2.4 一个能省大量请求的特性

**同一部漫画的任意章节页，数据串完全相同**（已比对 ch=1 与 ch=2，逐字节一致），
且含全部 47 章的记录。所以**整部漫画只需请求一次章节页**，就能算出全部章节的图片直链。

对比 manwame 每章都要请求一次并解密，这里 47 章省到 1 次。适配器内按漫画 id 缓存即可。

### 2.5 与 manwame 的差异汇总

| | manwame | 8comic |
|---|---|---|
| 章节列表 | HTML 直出 | HTML 直出 |
| 图片数据 | AES-128-CBC 解密 | 定长记录 + 十六进制片段拼接 |
| 每章请求数 | 1 次/章 | **1 次/整部** |
| 图片格式 | webp | jpg |
| 图片 CDN 校验 Referer | **是** | 否 |
| 章节页校验 Referer | 否 | **是（否则返回伪装页）** |

---

## 三、改动方案

### 3.1 新增 `sites.py`

放两个适配器，各自实现同一组接口：

```python
class Site:
    name: str
    @staticmethod
    def matches(url) -> bool          # 认不认这个 URL
    def headers(self) -> dict         # 本站需要的请求头
    def fetch_chapters(url, session) -> Comic
    def fetch_chapter_images(chapter_url, session) -> list[str]
```

- `ManwameSite`：把现有逻辑原样搬过去，行为不变
- `EightComicSite`：新实现，内部缓存数据串

`pick_site(url)` 按 `matches()` 路由，认不出就报错并列出支持的站点。

### 3.2 `scraper.py` 保持通用

- 站点相关的三个函数改为委托给适配器
- `make_session(proxy, site)` 接受站点以取得对应请求头
- `download_chapter()` 增加 `site` 参数
- 对外的 `fetch_chapters` / `fetch_chapter_images` **保留同名包装**，行为向后兼容

### 3.3 `app.py`

只需跟着传 `site`，路由和前端逻辑不动。

### 3.4 稳健性要求

- **不依赖被混淆的变量名**。数据串的变量名（实测 `wfp6_em7u4`）是站点构建时随机生成的，
  解析器按「页面中最长的那个字母数字串」定位，并校验长度能容纳声明的章节数。
- **算法常量集中定义**在文件顶部（记录长度 47、各字段偏移、片段区算式），
  与 manwame 的 AES 密钥一样注明重新提取方法。
- 拿不到数据串时**明确报错**（尤其要能认出 2.2 的伪装页），绝不静默返回空列表。

---

## 四、验收标准

1. `python scraper.py list https://www.8comic.com/html/12539.html` 列出 47 章
2. `python scraper.py download <该URL> -c 1` 下载第 1 章 6 张 jpg，文件可正常打开
3. `check` / `pack` 对 8comic 下载结果照常工作（与站点无关，应天然可用）
4. manwame 的既有行为不回归
5. 不带 Referer 的伪装页能被识别并报错，不会静默跳过
6. WebUI 贴 8comic URL 同样可用
