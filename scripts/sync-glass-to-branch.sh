#!/usr/bin/env bash
# sync-glass-to-branch.sh
# 把 glass 仓库的工作树镜像同步到当前分支（通常在 ui-glass 上执行）
#
# 排除：.git / __pycache__ / release / tests/logs / dist / build / logs
# 也就是说：只同步 app/ + main.py + tests/test_*.py + 工具链 + docs + assets
#
set -euo pipefail

GLASS_DIR="/home/admin/NS/pulses_easier_glass"
MAIN_DIR="/home/admin/NS/pulses_easier"

# 确认当前在 ui-glass 分支
BRANCH=$(git -C "$MAIN_DIR" branch --show-current 2>/dev/null || echo "")
if [ "$BRANCH" != "ui-glass" ]; then
  echo "错误：当前分支是 '$BRANCH'，请在 ui-glass 分支上执行此脚本"
  echo "  cd $MAIN_DIR && git checkout ui-glass && bash scripts/sync-glass-to-branch.sh"
  exit 1
fi

echo "→ 从 $GLASS_DIR 镜像同步到 $MAIN_DIR (分支: $BRANCH)"

rsync -a --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='release/' \
  --exclude='tests/logs/' \
  --exclude='dist/' \
  --exclude='build/' \
  --exclude='logs/' \
  --exclude='.pytest_cache/' \
  "$GLASS_DIR/" "$MAIN_DIR/"

echo "→ 同步完成，变更文件数："
git -C "$MAIN_DIR" status --porcelain | wc -l
echo ""
echo "下一步："
echo "  git add -A"
echo "  git commit -m '美化线快照：同步 glass 仓库最新代码'"
echo "  git checkout main   # 回到稳定线"
