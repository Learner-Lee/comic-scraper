# 漫画爬虫

抓取 manwame.com 的漫画，支持命令行和 Web 界面两种用法。

**流程：** 获取章节 → 下载到文件夹 → **人工检查** → 按章节打包 ZIP

下载完成后不会自动打包，留给你核对无误后手动触发。打包是**每章一个 zip**，漫画目录本身仍是目录：

```
downloads/漫画名/001 第1话/0001.webp   →   downloads/漫画名/001 第1话.zip
downloads/漫画名/002 第2话/0001.webp   →   downloads/漫画名/002 第2话.zip
```

每章 zip 校验通过（条目数比对 + 逐条 CRC）后才删除对应源目录，空间正是这样省下来的。

## 安装

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 命令行

```bash
python scraper.py list <URL>                      # 列出章节
python scraper.py download <URL>                  # 下载全部
python scraper.py download <URL> -c 1-10,15       # 只下指定章节
python scraper.py check downloads/<漫画名>         # 核对每章图片数
python scraper.py pack downloads/<漫画名>          # 确认后按章节打包
```

`pack` 参数：

| 参数 | 说明 |
|---|---|
| `--keep` | 打包后保留源目录（这样不省空间，仅用于核对） |
| `--cbz` | 用 `.cbz` 扩展名，便于 Tachiyomi / Komga 等阅读器识别 |

**可重复运行**：已有 zip 的章节会跳过，中途失败或中断后重跑同一命令即可续做。`check` 在打包前后都能用，会区分显示 `[目录]` 和 `[zip]`。

`download` 常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o, --out` | `downloads` | 输出根目录 |
| `-w, --workers` | `4` | 每章并发下载数 |
| `-d, --delay` | `0.15` | 每张图后的间隔秒数 |
| `--proxy` | 无 | 代理地址，默认直连 |

**断点续传**：已存在的图片会跳过。下载中断或发现某章缺图，重跑同一条 `download` 命令即可只补缺失部分。

## Web 界面

```bash
python app.py                   # http://127.0.0.1:60001
```

页面分四步，对应上面的流程。下载在后台线程执行，请求立即返回，进度实时轮询显示。
并发数、间隔秒、`.cbz` 扩展名都能在页面上直接填，留空即用默认值。

可用环境变量覆盖默认值：`COMIC_OUT`、`COMIC_WORKERS`、`COMIC_DELAY`、`COMIC_PROXY`。
页面上填的值只对本次任务生效，不会改动环境变量。

同一时间只跑一个任务：已有任务在跑时再投递会收到 409，不会互相踩。

> **安全提示**：Web 界面没有登录认证，因此默认只监听 `127.0.0.1`。如需外网访问，请在前面套一层反向代理并自行加认证，不要直接用 `--host 0.0.0.0` 暴露到公网。

## 文件说明

| 文件 | 作用 |
|---|---|
| `scraper.py` | 核心库 + 命令行入口 |
| `app.py` | Flask Web 界面 |
| `templates/app.html` | 前端单页 |

## 工作原理

1. **章节目录**是服务端直出的 HTML，用 `[data-chapter-list] a` 选择器即可解析，不需要浏览器内核。
2. **章节图片**不在 HTML 里，而是 AES-128-CBC 加密后放在页面的 `params` 变量中。IV 是 base64 解码后的前 16 字节，密文是其余部分，解密得到含 `chapter_images`（图片 token）和 `images_hosts`（CDN 域名）的 JSON。
3. **图片 CDN 强制校验 Referer**，不带会返回 403（页面 HTML 里的 `referrerpolicy="no-referrer"` 是误导）。

## 关于「省空间」

实测结论，别抱不切实际的期待：

- **压缩省不了空间**。webp 本身已是压缩格式，实测 ZIP_DEFLATED 反而比原文件大 0.1%。所以打包统一用 `ZIP_STORED`（只归档不压缩），不浪费 CPU。
- **省的是这两块**：一是不再产生「源目录 + 整部压缩包」的双份副本；二是文件数从数千降到几十，NTFS 4K 簇的碎片浪费随之减少（实测约 1.7%）。
- **附带好处**：文件数量级下降，复制、移动、备份到别的盘会快很多。

## 维护提示

AES 密钥是从站点前端 `cms.js` 提取的硬编码值，写在 `scraper.py` 顶部。站点若更换密钥，所有章节会同时解密失败，程序会明确报错提示。

重新提取方法：打开任意章节页，在浏览器控制台执行 `CMS.chapter.decrypt(params)` 对照，或用 Node 加载 `cms.js` 后钩住 `CryptoJS.AES.decrypt` 打印密钥。

站点若改版导致目录解析失败，程序会报 `目录页没找到 [data-chapter-list]`，届时更新选择器即可。
