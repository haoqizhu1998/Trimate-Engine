#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
APP_HOME="$HOME/.local/share/athlete-growth"
AUTOMATED=0
HOST=""
if [ "${1:-}" = "--automated" ]; then
  AUTOMATED=1
  HOST="${2:-}"
  if [ "$HOST" != "codex" ] && [ "$HOST" != "workbuddy" ]; then
    echo "自动安装须指定 codex 或 workbuddy 宿主。" >&2
    exit 2
  fi
fi
on_error() {
  echo "安装未完成，请查看上方错误并确认网络可用后重试。" >&2
  if [ "$AUTOMATED" -eq 0 ]; then read -r -p "按回车关闭窗口..." _; fi
}
trap on_error ERR
WHEEL="$(find . -maxdepth 1 -name 'athlete_growth-*.whl' -type f -print -quit)"
if [ -z "$WHEEL" ]; then
  echo "安装包不完整：找不到产品文件。"
  if [ "$AUTOMATED" -eq 0 ]; then read -r -p "按回车关闭窗口..." _; fi
  exit 1
fi

echo "Trimate安装到：$APP_HOME"
echo "个人训练数据保存在：$HOME/.athlete-growth"
echo "首次安装需要网络下载 Python 运行环境和依赖。"
mkdir -p "$APP_HOME"

if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV_BIN="$HOME/.local/bin/uv"
else
  echo "正在从 Astral 官方站安装 uv 运行环境管理器..."
  curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
    https://astral.sh/uv/install.sh -o "$APP_HOME/uv-install.sh"
  UV_NO_MODIFY_PATH=1 sh "$APP_HOME/uv-install.sh"
  UV_BIN="$HOME/.local/bin/uv"
  if [ ! -x "$UV_BIN" ]; then
    echo "uv 未安装成功，请检查网络后重试。"
    if [ "$AUTOMATED" -eq 0 ]; then read -r -p "按回车关闭窗口..." _; fi
    exit 1
  fi
fi

if [ ! -x "$APP_HOME/venv/bin/python" ]; then
  "$UV_BIN" venv --python 3.12 "$APP_HOME/venv"
fi
"$UV_BIN" pip install --reinstall-package athlete-growth \
  --python "$APP_HOME/venv/bin/python" "$WHEEL"

if [ ! -x "$APP_HOME/venv/bin/athlete-growth-mcp" ]; then
  echo "安装未完成：MCP 入口不存在。"
  if [ "$AUTOMATED" -eq 0 ]; then read -r -p "按回车关闭窗口..." _; fi
  exit 1
fi

cp "开始使用.command" "$APP_HOME/开始使用.command"
chmod 700 "$APP_HOME/开始使用.command"
cp "卸载Trimate.command" "$APP_HOME/卸载Trimate.command"
chmod 700 "$APP_HOME/卸载Trimate.command"
if [ "$AUTOMATED" -eq 1 ]; then
  "$APP_HOME/venv/bin/python" "bootstrap-local.py" --host "$HOST" \
    --cli "$APP_HOME/venv/bin/athlete-growth"
  exit 0
fi
echo "安装完成，接下来建立个人档案。"
ATHLETE_GROWTH_INSTALLER=1 "$APP_HOME/开始使用.command"
echo "请回到当前 AI 对话，让 AI 检查并连接本机 Trimate MCP。"
read -r -p "按回车关闭窗口..." _
