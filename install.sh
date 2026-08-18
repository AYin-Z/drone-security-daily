#!/usr/bin/env bash
# ============================================================================
# 无人机感知与反制技术日报 Agent —— 一键安装脚本
# 用法: bash install.sh
# 依赖: Python 3.9+（含 pip）、可访问外网（拉取依赖与 RSS/搜索）
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo "==> 检查 Python 版本"
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), "需要 Python 3.9+"; print("Python", sys.version.split()[0], "OK")'

echo "==> 创建虚拟环境 .venv（优先 venv，缺失 ensurepip 时回退 uv）"
if [ ! -d .venv ]; then
  if ! "$PY" -m venv .venv 2>/dev/null; then
    if command -v uv >/dev/null 2>&1; then
      uv venv .venv -q
    else
      echo "错误: python3-venv/ensurepip 不可用且未找到 uv。请安装其一："
      echo "  Debian/Ubuntu:  sudo apt install python3-venv"
      echo "  或:            curl -LsSf https://astral.sh/uv/install.sh | sh"
      exit 1
    fi
  fi
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> 安装依赖（httpx / feedparser / beautifulsoup4 / pyyaml）"
if command -v uv >/dev/null 2>&1; then
  uv pip install -q -r requirements.txt
else
  pip install --upgrade pip -q
  pip install -r requirements.txt
fi

echo "==> 初始化配置文件"
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "    已生成 config.yaml —— 请编辑填写："
  echo "      · llm.api_key        （必填，DeepSeek 等 OpenAI 兼容 API key）"
  echo "      · smtp.user/password （发件邮箱与 SMTP 授权码）"
  echo "      · smtp.to            （收件人，默认测试 yinz7032@qq.com）"
  echo "      · schedule.cron      （定时，当前为占位，确认后运行 ./scheduler_setup.sh）"
else
  echo "    config.yaml 已存在，跳过"
fi

echo "==> 创建数据/日志目录"
mkdir -p data/reports data/traces data/state data/emails logs

echo ""
echo "安装完成。下一步："
echo "  1) 编辑 config.yaml 填入 API key 与 SMTP 配置"
echo "  2) 手动试跑:  ./run_daily.py --dry-run-mail --mock-llm"
echo "  3) 确认无误后正式跑: ./run_daily.py"
echo "  4) 配置定时: 编辑 config.yaml 的 schedule.cron 后执行 ./scheduler_setup.sh"
