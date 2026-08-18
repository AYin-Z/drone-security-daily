#!/usr/bin/env bash
# ============================================================================
# 定时任务安装脚本：读取 config.yaml 的 schedule.cron 写入 crontab
# 用法: bash scheduler_setup.sh          # 安装/更新
#       bash scheduler_setup.sh --remove # 移除
# 注意: cron 时间目前为占位（config.yaml 中 schedule.cron = "0 8 * * 1-5"），
#       确认最终时间后修改 config.yaml 再运行本脚本。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
MARKER="$PWD/run_daily.py"

if [ ! -f config.yaml ]; then
  echo "错误: 未找到 config.yaml，请先运行 bash install.sh" >&2
  exit 1
fi

if [ "${1:-}" = "--remove" ]; then
  crontab -l 2>/dev/null | grep -Fv "$MARKER" | crontab - || true
  echo "已移除定时任务"
  exit 0
fi

# 从 YAML 读取 cron 表达式（支持格式: cron: "0 8 * * *"）
CRON=$(grep -E '^\s*cron:' config.yaml | head -1 | sed -E 's/^[[:space:]]*cron:[[:space:]]*["'"'"']?([^"'"'"']*)["'"'"']?.*/\1/' | xargs)
if [ -z "$CRON" ]; then
  echo "错误: config.yaml 中未找到 schedule.cron" >&2
  exit 1
fi
FIELDS=$(echo "$CRON" | awk '{print NF}')
if [ "$FIELDS" != "5" ]; then
  echo "错误: cron 表达式应为 5 段（分 时 日 月 周），当前为: $CRON" >&2
  exit 1
fi

# 先移除旧条目（按 run_daily.py 路径标记），再追加新条目
crontab -l 2>/dev/null | grep -Fv "$MARKER" | crontab - || true

LINE="$CRON cd $PWD && /usr/bin/env bash -c 'source .venv/bin/activate && python3 run_daily.py >> logs/cron.log 2>&1'  # drone-security-daily"
(crontab -l 2>/dev/null || true; echo "$LINE") | crontab -

echo "已安装定时任务: $CRON"
echo "  命令: $LINE"
echo "  查看: crontab -l | grep drone-security-daily"
echo "  说明: 每日运行输出追加到 logs/cron.log，失败可在其中排查"
