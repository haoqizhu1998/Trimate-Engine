#!/bin/bash
set -euo pipefail

APP_HOME="$HOME/.local/share/athlete-growth"
CLI="$APP_HOME/venv/bin/athlete-growth"
echo "将移除本机Trimate程序和后台任务。"
echo "个人训练数据 $HOME/.athlete-growth 会保留。"
read -r -p "确认卸载程序？[y/N] " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  echo "已取消。"
  exit 0
fi
if [ -x "$CLI" ]; then
  "$CLI" automation-uninstall-macos || true
  "$CLI" workbuddy-unconfigure || true
fi
if [ -L "$APP_HOME" ]; then
  echo "安装目录是符号链接，请手动检查后移除。"
  exit 1
fi
if [ -d "$APP_HOME" ]; then
  rm -rf -- "$APP_HOME"
fi
echo "程序已移除。若曾在 AI 宿主中添加 Trimate MCP，请在该宿主中移除连接。"
read -r -p "按回车关闭窗口..." _
