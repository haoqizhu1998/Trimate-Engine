#!/bin/bash
set -euo pipefail

CLI="$HOME/.local/share/athlete-growth/venv/bin/athlete-growth"
if [ ! -x "$CLI" ]; then
  echo "请先双击 安装Trimate.command。"
  read -r -p "按回车关闭窗口..." _
  exit 1
fi

if ! "$CLI" status >/dev/null 2>&1; then
  echo "先建立只保存在本机的训练档案。"
  read -r -p "你希望Trimate怎样称呼你？" ATHLETE_NAME
  ATHLETE_NAME="${ATHLETE_NAME:-运动员}"
  TIMEZONE="$(readlink /etc/localtime 2>/dev/null | sed 's#^.*/zoneinfo/##' || true)"
  TIMEZONE="${TIMEZONE:-Asia/Shanghai}"
  read -r -p "时区 [$TIMEZONE]：" INPUT_TIMEZONE
  TIMEZONE="${INPUT_TIMEZONE:-$TIMEZONE}"
  "$CLI" init --name "$ATHLETE_NAME" --timezone "$TIMEZONE" --units metric
fi

echo "档案已就绪。"
"$CLI" status
AUTO_SELECTED=0
read -r -p "现在连接 Garmin 中国区账号吗？[y/N] " CONNECT
if [[ "$CONNECT" == "y" || "$CONNECT" == "Y" ]]; then
  echo "密码和验证码只在本机隐藏输入。"
  "$CLI" garmin-cn-login
  "$CLI" automation-configure
  "$CLI" automation-run-once --force
  AUTO_SELECTED=1
fi

read -r -p "现在连接高驰中国区账号吗？[y/N] " CONNECT_COROS
if [[ "$CONNECT_COROS" == "y" || "$CONNECT_COROS" == "Y" ]]; then
  echo "将官方 COROS MCP 登录工具下载到本机个人数据目录。"
  "$CLI" coros-helper-install
  "$CLI" coros-login-start
  read -r -p "完成浏览器中的高驰授权后，按回车继续..." _
  if "$CLI" coros-login-finish; then
    read -r -p "先试同步最近 3 天的高驰活动吗？[y/N] " COROS_SYNC
    if [[ "$COROS_SYNC" == "y" || "$COROS_SYNC" == "Y" ]]; then
      START_DATE="$(date -v-2d +%Y-%m-%d)"
      END_DATE="$(date +%Y-%m-%d)"
      if "$CLI" sync --source coros --start "$START_DATE" --end "$END_DATE"; then
        "$CLI" automation-configure --source coros
        AUTO_SELECTED=1
      else
        echo "高驰同步尚未通过。可继续使用在线查询；详情见 使用说明.md。"
      fi
    fi
  else
    echo "高驰授权未完成，可稍后运行 coros-login-finish。"
  fi
fi

if [ "$AUTO_SELECTED" -eq 1 ]; then
  read -r -p "登录这台 Mac 时自动同步并分析训练数据吗？[y/N] " ENABLE_AUTO
  if [[ "$ENABLE_AUTO" == "y" || "$ENABLE_AUTO" == "Y" ]]; then
    "$CLI" automation-install-macos
  fi
fi

echo "请回到当前 AI 对话，询问：检查我的训练数据，帮我开始使用。"
if [ "${ATHLETE_GROWTH_INSTALLER:-0}" != "1" ]; then
  read -r -p "按回车关闭窗口..." _
fi
