#!/usr/bin/env bash
# coding-agent 一键安装脚本
#
# 用法：
#   ./install.sh                 # 创建 .venv 并安装（含 dev 依赖）
#   ./install.sh --with-tokenizer # 额外装 tiktoken（更准的 token 计数）
#   ./install.sh --with-browser   # 额外装 playwright（browser_* 工具）
#   ./install.sh --all            # 装上面所有可选依赖
#   ./install.sh --no-dev         # 不装 dev 依赖（仅运行所需）
#
# 安装到独立的 .venv，避免污染全局 / conda 环境（coding-agent 固定了
# httpx>=0.27、rich 等版本，装进共享环境可能与其它项目冲突）。
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
WITH_TOKENIZER=0
WITH_BROWSER=0
DEV=1

for arg in "$@"; do
  case "$arg" in
    --with-tokenizer) WITH_TOKENIZER=1 ;;
    --with-browser)   WITH_BROWSER=1 ;;
    --all)            WITH_TOKENIZER=1; WITH_BROWSER=1 ;;
    --no-dev)         DEV=0 ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

# 1. 检查 python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 not found. Please install Python 3.10+ first."
  exit 1
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "🐍 Using python3 ($PYV)"

# 2. 创建 venv
if [ ! -d "$VENV" ]; then
  echo "📦 Creating virtualenv at $VENV ..."
  python3 -m venv "$VENV"
else
  echo "📦 Reusing existing virtualenv at $VENV"
fi

PIP="$VENV/bin/pip"
"$PIP" install -q --upgrade pip

# 3. 组装 extras
EXTRAS=""
if [ "$DEV" = "1" ]; then EXTRAS="dev"; fi
if [ "$WITH_TOKENIZER" = "1" ]; then EXTRAS="${EXTRAS:+$EXTRAS,}tokenizer"; fi
if [ "$WITH_BROWSER" = "1" ]; then EXTRAS="${EXTRAS:+$EXTRAS,}browser"; fi

if [ -n "$EXTRAS" ]; then
  echo "⬇️  Installing coding-agent with extras: [$EXTRAS] ..."
  "$PIP" install -q -e ".[$EXTRAS]"
else
  echo "⬇️  Installing coding-agent ..."
  "$PIP" install -q -e .
fi

# 4. playwright 浏览器二进制（如装了 browser extra）
if [ "$WITH_BROWSER" = "1" ]; then
  echo "🌐 Installing Playwright browser (chromium) ..."
  "$VENV/bin/python" -m playwright install chromium || \
    echo "⚠️  playwright browser install failed; browser_* tools may not work."
fi

echo ""
echo "✅ Installed. Next steps:"
echo ""
echo "  1) Set your model API key (OpenAI-compatible by default):"
echo "       export MODEL_API_KEY=sk-..."
echo "       export MODEL_BASE_URL=https://api.openai.com/v1   # or your gateway"
echo "       export MODEL_PRIMARY=gpt-4o                       # model name"
echo ""
echo "  2) Run it:"
echo "       $VENV/bin/coding-agent          # plain CLI"
echo "       $VENV/bin/coding-agent --tui    # rich TUI"
echo ""
echo "  (or 'make run' / 'make test' which use the same venv)"
