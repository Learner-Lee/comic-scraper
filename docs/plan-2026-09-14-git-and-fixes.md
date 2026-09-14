# Plan：接入 Git 并修复已知缺陷

**日期**：2026-09-14
**目标**：把本项目纳入 Git 版本管理并推送到 GitHub，随后修复代码审查中发现的 P1/P2 级问题。

---

## 一、背景

项目已在两部漫画（83 章 / 86 章，共约 926MB）上跑通完整流水线，但一直没有版本管理。
代码审查发现 2 个并发缺陷（P1）和 3 个体验缺陷（P2），均非致命，但与代码自身的注释承诺不符。

---

## 二、阶段一：建立 Git 仓库

| 步骤 | 动作 | 验收 |
|---|---|---|
| 1.1 | `git init`，确认默认分支为 `main` | `git branch` 显示 main |
| 1.2 | 核对 `.gitignore` 是否挡住 `downloads/`、`venv/`、`.idea/`、`.DS_Store` | `git status` 中不出现这些路径 |
| 1.3 | 首次提交全部源码与文档 | 提交内仅含 4 个源文件 + README + docs |
| 1.4 | 关联远程 `git@github.com:Learner-Lee/comic-scraper.git` 并推送 | 远程 main 分支可见 |

**前置确认**：SSH 认证已通过（身份 `Learner-Lee`，与仓库 owner 一致）；
`git ls-remote` 返回空，说明远程是**空仓库**，推送不会覆盖任何已有内容。

---

## 三、阶段二：修复缺陷

### P1-1　任务槽的 TOCTOU 竞态（app.py）

**问题**：`JOB.busy()` 检查与 `_start()` 置位之间没有原子性，两个并发请求可同时通过检查，各起一个后台线程互相踩同一份 JOB 状态。

**第一性原理**：「检查条件」与「依据条件改状态」必须在同一临界区内完成，否则条件在两者之间会失效。

**修法**：在 `Job` 上新增 `try_begin(kind)`，在**同一把锁内**完成「判空闲 → reset → 置 running」，返回是否抢到任务槽。路由改为依据其返回值决定 409。

### P1-2　cancel 标志未受锁保护（app.py）

**问题**：`JOB.cancel` 的读写完全不走 `_lock`，与 `Job` 类注释「所有字段读写都在 _lock 保护下」自相矛盾。

**修法**：换成 `threading.Event`，它本身即线程安全，语义也更贴切（一次性信号）。对外暴露 `cancel()` / `cancelled()` 两个方法。

### P2-1　CLI 的章节范围解析会吐 traceback（scraper.py）

**问题**：`parse_chapter_spec` 内的 `int()` 抛裸 `ValueError`，CLI 未捕获，输入 `-c abc` 直接显示堆栈。

**修法**：在函数内把解析失败转成带示例说明的 `ValueError`；顺带校验序号 ≥ 1 与区间不得反向。CLI 入口补捕 `ValueError`。
`app.py` 原本就捕获 `ValueError`，保持向后兼容。

### P2-2　WebUI 缺少 cbz / workers / delay 选项（app.py + app.html）

**问题**：命令行支持 `--cbz`，WebUI 没有；并发数和间隔只能靠环境变量，页面上看不见当前值。

**修法**：打包区加 `.cbz` 勾选框；下载区加并发数、间隔两个可选输入框（留空用默认值），并在占位符里显示当前生效的默认值。

### 不修的项（记录取舍理由）

- **取消只在章节边界生效**：当前章会下完才停。要做到图片级中断需把 cancel 传进线程池，复杂度不值当，且 UI 文案已明确告知。
- **`JOB.zip_path` 字段名误导**（实际存的是漫画目录）：改名需同步动前端，收益低，保持现状。

---

## 四、验收标准

1. GitHub 远程 main 分支包含全部源码，且不含 `downloads/`、`venv/`、`.idea/`。
2. `python scraper.py check downloads/<漫画名>` 对两部已打包漫画输出正常。
3. `python scraper.py download <url> -c abc` 输出一行友好错误，无 traceback。
4. WebUI 可正常启动，四步流程 UI 元素齐全，新增选项能传到后端。
5. 每完成一个阶段补一份 markdown 记录文档。
