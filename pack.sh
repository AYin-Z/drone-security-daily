#!/usr/bin/env bash
# ============================================================================
# 交付包打包脚本：生成不含任何密钥/运行数据的干净交付压缩包
# 用法: bash pack.sh [版本号]
# 产物: drone-security-daily-delivery-v<版本>.tar.gz （默认 v1.0.0）
# 排除: config.yaml（含 llm.api_key / smtp 授权码！）、data/、logs/、.venv/、__pycache__
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

VER="${1:-1.0.0}"
STAGE=".pack_stage"
OUT="drone-security-daily-delivery-v${VER}.tar.gz"

rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE"

# 1) 复制交付文件（仅源码/模板/文档/脚本）
cp -r dsdaily run_daily.py install.sh scheduler_setup.sh requirements.txt README.md config.example.yaml "$STAGE"/
find "$STAGE" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 2) 安全校验：交付目录内不得出现密钥痕迹
if grep -rInE "sk-[A-Za-z0-9]{20,}" "$STAGE" 2>/dev/null; then
  echo "错误: 交付目录发现疑似 API key，终止打包" >&2
  rm -rf "$STAGE"
  exit 1
fi
if grep -rInE "password:[[:space:]]*[^\"'#[:space:]]{8,}" "$STAGE" 2>/dev/null; then
  echo "错误: 交付目录发现疑似凭据值（password 字段非空），终止打包" >&2
  rm -rf "$STAGE"
  exit 1
fi
if [ -f "$STAGE/config.yaml" ]; then
  echo "错误: config.yaml 不应出现在交付包（含密钥），已由模板代替" >&2
  rm -rf "$STAGE"
  exit 1
fi

# 3) 打包
tar -czf "$OUT" -C "$STAGE" .
rm -rf "$STAGE"

echo "✅ 交付包已生成: $OUT"
echo "  内容: dsdaily/ run_daily.py install.sh scheduler_setup.sh requirements.txt README.md config.example.yaml"
echo "  部署: 解压后 bash install.sh（会从 config.example.yaml 生成空白 config.yaml），再填 llm.api_key / smtp 凭据"
