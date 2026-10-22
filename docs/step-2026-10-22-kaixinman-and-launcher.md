# 阶段记录：支持 kaixinman + 双击启动脚本

**日期**：2026-10-22
**对应计划**：[plan-2026-10-22-kaixinman-and-launcher.md](plan-2026-10-22-kaixinman-and-launcher.md)
**状态**：已完成，验收标准全部通过

---

## 一、kaixinman：本次最省事的一站

研究到第三步就发现了关键线索——章节页里有 `var params = '...'`，**和 manwame
一模一样的形态**。于是直接拿 manwame 的 AES 密钥去试，一次就解开了。

两站是**同一套 CMS**：加密方式、密钥、JSON 字段结构全都一致：

```
host / comic_id / comic_name / chapter_images / images_hosts / images_base64 / cdnurl …
```

所以 `KaixinmanSite` **直接继承 `ManwameSite`**，`decrypt_params` /
`fetch_chapter_images` / `headers` 一行都没重写。只覆盖了：

| 项 | manwame | kaixinman |
|---|---|---|
| 域名匹配 | manwame.com | kaixinman.com |
| 章节列表 | `[data-chapter-list]` | `ul.chapter-list` |
| 漫画名 | `.profile-text h1` | `h1 > span`（第一个） |

`headers()` 能继承是因为它用的是 `self.home`——子类自然带上自己的 Referer，
不需要改。这是当初那样写的意外回报。

### 一个小坑

漫画名的 `h1` 里混着评分：

```html
<h1><span>史诗级领地设计师</span><span class="score">9.9</span></h1>
```

直接 `get_text()` 得到「史诗级领地设计师9.9」。取第一个 `span` 解决，
并以 `<title>` 按 ` - ` 切分作为兜底。

## 二、启动脚本

用 `.command` 格式——macOS 双击会**在 Terminal 里打开并运行**，窗口可见、
输出可读、能 Ctrl+C，正合「可以控制的打开 terminal 那种」。

做成**菜单**而不是只干一件事，每项做完回到菜单，不用反复双击。

### 几个刻意的决定

| 决定 | 为什么 |
|---|---|
| 不用 `source venv/bin/activate` | 这个 venv 是从别的机器复制来的，activate 里硬编码旧路径，激活后会报 `No module named 'Crypto'`。直接调 `./venv/bin/python` 不受影响 |
| 开头 `cd "$(dirname "$0")"` | 双击时工作目录不是项目目录 |
| 启动前先自检 venv 与依赖 | 缺了就给出确切的修复命令，而不是抛一串 traceback |
| 核对/打包用编号选漫画 | 漫画名都是中文，手敲路径容易出错 |
| 打包前二次确认 | 它会删源目录，是不可逆操作 |
| 不 `clear` 屏幕 | 保留输出历史，出问题能往上翻 |
| 记住上次的 URL | 下同一部漫画的后续章节时省事（存 `.last_url`，已加进 .gitignore） |

## 三、验收结果

### kaixinman

| 验收项 | 结果 |
|---|---|
| `sites` | ✓ 5 个站点，开心漫条目齐全 |
| `list` | ✓ 221 章，顺序正确（第1话在前） |
| 漫画名 | ✓《史诗级领地设计师》，评分 9.9 没混进来 |
| `probe` | ✓ 抽查第1/106/216话，首末页全 HTTP 200 |
| 下载 | ✓ 第216话 67/67，序号连续，有效 JPEG |
| 继承关系 | ✓ `isinstance(…, ManwameSite)` 为真，Referer 自动是 kaixinman |
| manwame 未受影响 | ✓ 仍独立路由，probe 通过 |

### 启动脚本

| 验收项 | 结果 |
|---|---|
| `bash -n` 语法 | ✓ |
| 可执行权限 | ✓ `-rwxr-xr-x` |
| 6) 站点清单 | ✓ 列出 5 站 |
| 4) 选漫画 | ✓ 列出全部 7 部，与 `downloads/` 实际目录数一致 |
| 无效输入 | ✓ 提示「没有这个选项」后回到菜单 |
| venv 缺失 | ✓ 打印确切的创建命令，不抛 traceback |

### 其余四站回归

✓ manwame / 8comic / shenqimanhua 的 probe 均通过；manhuagui 目录解析正常
（55 章）。

## 四、遗留

上一轮那个 manhuagui 19379 的预检任务，在会话结束时被中断了，没有结论。
需要的话单独再跑一次即可——manhuagui 每章都要单独请求，章节多的话会比较久。
