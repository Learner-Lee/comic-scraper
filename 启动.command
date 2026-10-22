#!/bin/bash
# 双击即可运行：macOS 会在 Terminal 里打开这个脚本，所以输出看得见、Ctrl+C 停得下。
#
# 刻意不用 `source venv/bin/activate`：这个 venv 曾经是从别的机器整个复制过来的，
# activate 里硬编码着旧路径，激活后 python 会落回系统解释器、报 No module named 'Crypto'。
# 直接调 ./venv/bin/python 不受这个影响。

cd "$(dirname "$0")" || exit 1
printf '\033]0;漫画爬虫\007'          # 设置 Terminal 窗口标题

PY="./venv/bin/python"
URL_FILE=".last_url"                  # 记住上次的 URL，省得每次重敲

die() { echo; echo "$1"; echo; read -r -p "按回车关闭… " _; exit 1; }
pause() { echo; read -r -p "按回车回到菜单… " _; }

# ---------------------------------------------------------------- 环境自检
if [ ! -x "$PY" ]; then
  die "找不到虚拟环境：$PWD/venv

请先在这个目录执行：
  python3 -m venv venv
  ./venv/bin/pip install -r requirements.txt"
fi

if ! "$PY" -c "import requests, bs4, flask, Crypto" 2>/dev/null; then
  die "依赖不全。请执行：
  ./venv/bin/pip install -r requirements.txt"
fi

# ---------------------------------------------------------------- 小工具
ask_url() {
  local last=""
  [ -f "$URL_FILE" ] && last=$(cat "$URL_FILE" 2>/dev/null)
  echo
  if [ -n "$last" ]; then
    echo "上次用的：$last"
    read -r -p "漫画目录页 URL（直接回车沿用上次）: " REPLY_URL
    [ -z "$REPLY_URL" ] && REPLY_URL="$last"
  else
    echo "不知道该贴什么？先在菜单选 6 看支持哪些站点。"
    read -r -p "漫画目录页 URL: " REPLY_URL
  fi
  [ -z "$REPLY_URL" ] && { echo "没填，取消。"; return 1; }
  printf '%s' "$REPLY_URL" > "$URL_FILE"
  return 0
}

pick_comic() {
  local dirs=() i=1 d n
  while IFS= read -r d; do
    [ -n "$d" ] && dirs+=("$d")
  done < <(find downloads -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)

  if [ ${#dirs[@]} -eq 0 ]; then
    echo; echo "downloads/ 下还没有漫画，先下载一部吧。"
    return 1
  fi
  echo
  for d in "${dirs[@]}"; do
    echo "  $i) $(basename "$d")"
    i=$((i + 1))
  done
  echo
  read -r -p "选择编号（留空取消）: " n
  [ -z "$n" ] && return 1
  case "$n" in
    *[!0-9]*) echo "请输入数字。"; return 1 ;;
  esac
  if [ "$n" -lt 1 ] || [ "$n" -gt ${#dirs[@]} ]; then
    echo "超出范围。"; return 1
  fi
  REPLY_DIR="${dirs[$((n - 1))]}"
  return 0
}

# ---------------------------------------------------------------- 主菜单
while true; do
  echo
  echo "════════════════════════════════════════"
  echo "   漫画爬虫"
  echo "════════════════════════════════════════"
  echo "   1) 启动网页界面"
  echo "   2) 预检一部漫画（下载前先确认能下）"
  echo "   3) 下载一部漫画"
  echo "   4) 核对已下载的章节"
  echo "   5) 打包成 ZIP"
  echo "   6) 支持哪些站点"
  echo "   q) 退出"
  echo
  read -r -p "请选择: " choice

  case "$choice" in
    1)
      echo
      echo "启动中…浏览器会自动打开 http://127.0.0.1:60001"
      echo "按 Ctrl+C 停止网页界面，回到菜单。"
      echo
      ( sleep 2; open "http://127.0.0.1:60001" >/dev/null 2>&1 ) &
      "$PY" app.py
      pause
      ;;
    2)
      ask_url || { pause; continue; }
      echo
      "$PY" scraper.py probe "$REPLY_URL"
      pause
      ;;
    3)
      ask_url || { pause; continue; }
      echo "章节多的话建议分批，例如 1-50。"
      read -r -p "章节范围（留空 = 全部）: " rng
      echo
      if [ -n "$rng" ]; then
        "$PY" scraper.py download "$REPLY_URL" -c "$rng" --retry
      else
        "$PY" scraper.py download "$REPLY_URL" --retry
      fi
      pause
      ;;
    4)
      pick_comic || { pause; continue; }
      echo
      "$PY" scraper.py check "$REPLY_DIR"
      pause
      ;;
    5)
      pick_comic || { pause; continue; }
      echo
      echo "即将打包：$(basename "$REPLY_DIR")"
      echo "每章 zip 校验通过后会删掉对应源目录（空间正是这样省下来的）。"
      echo "建议先用 4) 核对一遍图片数。"
      read -r -p "确认打包？(y/N) " yn
      echo
      case "$yn" in
        y|Y) "$PY" scraper.py pack "$REPLY_DIR" ;;
        *)   echo "已取消。" ;;
      esac
      pause
      ;;
    6)
      echo
      "$PY" scraper.py sites
      pause
      ;;
    q|Q)
      echo; echo "再见。"; echo
      exit 0
      ;;
    "")
      ;;
    *)
      echo "没有这个选项：$choice"
      ;;
  esac
done
