# Plan：新增 manhuagui 支持 + 站点清单命令

**日期**：2026-09-17
**目标**：
1. 支持 m.manhuagui.com（看漫画/漫画柜）
2. 新增 `sites` 命令，一眼看清目前支持哪些站点、URL 长什么样

---

## 一、manhuagui 实地调研结论

全部经实测验证。

### 1.1 目录页 `https://m.manhuagui.com/comic/<id>/`

| 项 | 取法 | 实测 |
|---|---|---|
| 漫画名 | `<h1>` | 我的男孩子气女友实在太可爱了 |
| 章节 | `.chapter-list a[href]`，标题在 `<b>` | 53 条 |

**章节是倒序的**（第49话在最前），需要反转成正序再编号。

### 1.2 章节页的图片数据：三层包装

这是三个站点里最绕的一个，但每层都是确定性的：

**第一层 · eval 被藏起来了。** 脚本写作 `window["\x65\x76\x61\x6c"](…)`，
直接 grep `eval` 是搜不到的（我第一次就这么漏掉了）。

**第二层 · 标准 Dean Edwards packer。** `function(p,a,c,k,e,d){…}` 那套，
实测 `a=52, c=52`。

**第三层 · 字典是 LZString 压缩的。** packer 的 `k` 参数通常是
`'a|b|c'.split('|')`，这里却是一个 base64 串调用 `['\x73\x70\x6c\x69\x63']('\x7c')`——
解码出来是 `splic`（不是 `split`），是站点自定义的方法，实为
**LZString.decompressFromBase64 后再 split('|')**。

解开后得到 `SMH.reader({...})`，字段：

```
bookName / chapterTitle / count / images[] / sl{e, m}
```

实测解压出 52 项字典，与 packer 的 `c=52` 正好吻合，是解对了的硬证据。

### 1.3 图片直链

```
https://i.hamreus.com + urlquote(images[i]) + ?e=<sl.e>&m=<sl.m>
```

- `images` 里的路径**含中文**（章节名），必须 URL 编码
- `sl` 是**带时效的签名**（`e` 是过期时间戳），所以数据不能长期缓存，每章现取
- **图床强制校验 Referer**，不带直接 403（与 manwame 同，与 8comic 相反）
- 格式是 webp，实测单图约 400KB

### 1.4 与已有两站的对照

| | manwame | 8comic | manhuagui |
|---|---|---|---|
| 图片数据 | AES-128-CBC | 定长记录+片段 | packer + LZString |
| 每章请求 | 1 次/章 | 1 次/整部 | 1 次/章 |
| 图床校验 Referer | 是 | 否 | **是** |
| 地址时效 | 无 | 无 | **有签名，会过期** |
| 图片格式 | webp | jpg | webp |

---

## 二、改动方案

### 2.1 新增 `lzstring.py`

只实现 `decompress_from_base64` 一个函数（约 50 行）。

**为什么不装 pip 包**：算法是公开且确定的，写一次就固定了；项目现在只有 4 个
依赖，为一个函数引入供应链风险不划算。正确性用「解压项数 == packer 的 c」校验。

### 2.2 `sites.py` 新增 `ManhuaguiSite`

沿用现有 `Site` 接口，不动通用流程。

### 2.3 站点元数据 + `sites` 命令

给 `Site` 加三个类属性，让站点自己描述自己：

```python
label = "看漫画 / 漫画柜"      # 中文名
url_hint = "https://m.manhuagui.com/comic/<id>/"
example = "https://m.manhuagui.com/comic/53656/"
```

- CLI：`python scraper.py sites` 列出全部
- 「不支持的站点」报错时也复用同一份清单（现在只印了域名，信息太少）
- WebUI：页面上列出来，贴 URL 前就能看到

---

## 三、验收标准

1. `sites` 命令列出 3 个站点及其 URL 形态与示例
2. `probe https://m.manhuagui.com/comic/53656/` 预检通过
3. 下载第 1 章成功，文件是有效 webp
4. `check` / `pack` 照常工作
5. manwame 与 8comic 均不回归（用 probe 逐个验）
6. LZString 实现正确性有硬校验（项数与 packer 的 c 相符）
7. 贴不支持的 URL 时，报错里能看到完整站点清单
