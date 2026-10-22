# Plan：支持 kaixinman.com + 双击启动脚本

**日期**：2026-10-22
**目标**：
1. 加入第五个站点「开心漫」
2. 做一个双击就能用的启动脚本，会打开 Terminal 窗口可见可控

---

## 一、kaixinman 调研结论：和 manwame 是同一套 CMS

这是本次最省事的发现——**用 manwame 的 AES 密钥直接解开了 kaixinman 的 `params`**。

两站的加密方式、密钥、JSON 字段结构**完全一致**：

```
host / comic_id / comic_name / chapter_images / images_hosts / images_base64 / cdnurl …
```

实测《史诗级领地设计师》第1话解出 156 张图，CDN 是 `https://s2.bzcdn.net`。

所以 `KaixinmanSite` **直接继承 `ManwameSite`**，只需覆盖三处：

| 项 | manwame | kaixinman |
|---|---|---|
| 域名匹配 | manwame.com | kaixinman.com |
| 章节列表选择器 | `[data-chapter-list]` | `ul.chapter-list` |
| 漫画名 | `.profile-text h1` | `h1 > span`（第一个） |

`decrypt_params` / `fetch_chapter_images` / `headers` 全部继承不动——`headers` 里用的是
`self.home`，继承后 Referer 自动变成 kaixinman，不用改。

### 要注意的两处

1. **漫画名里混着评分**：`<h1><span>史诗级领地设计师</span><span class="score">9.9</span></h1>`，
   直接 `get_text()` 会得到「史诗级领地设计师9.9」。取第一个 `span`，
   拿不到则从 `<title>` 按 ` - ` 切。
2. 章节是**正序**的（第1话在前），不用反转。实测 221 章。
3. 图床 `s2.bzcdn.net` **不校验 Referer**（带不带都 200，JPEG）。

## 二、双击启动脚本

### 形式：`.command` 文件

macOS 上双击 `.command` 会**在 Terminal 里打开并运行**，窗口可见、输出可读、
能 Ctrl+C 中断——正是「可以控制的打开 terminal 那种」。

### 设计成菜单而不是只干一件事

```
  1) 启动网页界面（并自动打开浏览器）
  2) 预检一部漫画
  3) 下载一部漫画
  4) 核对已下载的章节
  5) 打包成 ZIP
  6) 看支持哪些站点
  q) 退出
```

每项做完回到菜单，不用反复双击。

### 几个必须处理好的细节

| 问题 | 做法 |
|---|---|
| 双击时工作目录不是项目目录 | 脚本开头 `cd "$(dirname "$0")"` |
| venv 的 activate 曾经坏过 | **不依赖 activate**，直接用 `./venv/bin/python` |
| venv 根本不存在 | 检测到就提示创建命令并退出，不留下莫名错误 |
| 依赖没装 | 先试 import，缺了就提示 `pip install -r requirements.txt` |
| 双击后需要可执行权限 | `chmod +x` 并在 README 说明 |
| 下载/打包要选漫画 | 列出 `downloads/` 下的目录编号让用户选，不用手敲路径 |

## 三、验收标准

### kaixinman
1. `sites` 列出 5 个站点
2. `list` 正确解析 221 章，顺序正确（第1话在前），漫画名不含评分
3. `probe` 通过
4. 下载前 2 章成功，文件有效
5. 现有四站不回归

### 启动脚本
6. 语法检查通过（`bash -n`）
7. 菜单各项能正确拼出命令
8. venv 缺失时给出清晰提示而不是报错
9. 有可执行权限
