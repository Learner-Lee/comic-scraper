# 移除 repair 功能

**日期**：2026-10-19

---

## 为什么删

`repair` 是我对需求理解有偏差时做出来的：它联网核对**全部历史章节**的真实页数，
还会解包补齐已打包的 zip。

需求澄清后，实际要的只是——**本次下载里失败的图重下那几张**；已经进了
`downloads/` 的 zip 说明当时就下齐了，不需要再核对。那部分已由
[retry 功能](step-2026-10-19-retry-failed.md)实现，且不用联网、不碰已完成的数据。

留着一个用不上的命令是噪音：它会让人以为流程里还有一步该走，也给以后每次改
下载逻辑时多一处要照顾的地方。所以删干净。

## 删了什么

| 文件 | 移除内容 |
|---|---|
| `scraper.py` | `Gap` / `scan_gaps` / `repair_comic` / `_have_in_dir` / `_have_in_zip` / `_local_chapters` / `_fetch_missing` / `_repair_zip`、两条正则、`repair` 子命令、`import re`（只被它用） |
| `app.py` | `_run_repair`、`/api/repair` |
| `templates/app.html` | 「检查并补齐缺图」按钮、「含已打包的 zip」复选框、说明文案、JS 处理与轮询引用 |
| `README.md` | 「补齐缺失的图片」一节、命令示例 |

共删掉约 218 行核心代码。`scraper.py` 从 791 行回到 573 行。

## 怎么确保没删错

删除时用边界断言护了一道——确认被删范围内不含 `Failure` / `retry_failures` /
`download_chapter` / `probe`，避免把 retry 功能连带删掉。

删完逐项验证：

| 项 | 结果 |
|---|---|
| 四个文件语法 | ✓ |
| 子命令列表 | ✓ `{list,download,sites,probe,check,pack}`，无 repair |
| `Failure` / `retry_failures` | ✓ 仍导出 |
| `download_chapter` 的 `failures` 参数 | ✓ 仍在 |
| `repair_comic` / `Gap` | ✓ 已不存在 |
| WebUI 重试按钮 | ✓ 仍在 |
| WebUI 补齐按钮 | ✓ 已移除 |
| `/api/retry` | ✓ 仍在（空 URL 返回 400） |
| `/api/repair` | ✓ 返回 404 |
| `check` 命令 | ✓ 正常（47 章 / 484 张） |
| 全项目文本残留 | ✓ 代码与文档中搜不到 repair |

## 保留的历史

`plan-2026-10-19-repair-missing.md` 和 `step-2026-10-19-repair-missing.md` 留着，
顶部加了移除说明。那两份记录了需求理解偏差是怎么发生、又是怎么纠正的，
比直接删掉有价值。
