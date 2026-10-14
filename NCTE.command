#!/bin/bash
# Double-click in Finder to open NCTE in the browser. The first start installs what it needs (a few minutes).
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.12 以上：請先到 https://www.python.org/downloads/ 安裝，再雙擊一次。"
  read -r -p "按 Enter 關閉…" _; exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
  echo "Python 版本太舊（$(python3 --version)），需要 3.12 以上：https://www.python.org/downloads/"
  read -r -p "按 Enter 關閉…" _; exit 1
fi
if [ ! -x .venv/bin/ncte ]; then
  echo "第一次啟動：安裝 NCTE…"
  python3 -m venv .venv && .venv/bin/pip install -q -e . || { read -r -p "安裝失敗，按 Enter 關閉…" _; exit 1; }
fi
echo "NCTE 已啟動，瀏覽器會自動打開。用完關掉這個視窗即可（翻譯中關掉會暫停，下次按「繼續」接著做）。"
exec .venv/bin/ncte gui
