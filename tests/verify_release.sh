#!/usr/bin/env bash
# verify_release.sh — 把「已发布的更新包」下载下来，实际装到原始源码上再验证。
#
# 做四件事：
#   1. 匿名从 GitHub 下载指定的更新包（无需登录），核对 md5 与本地一致
#   2. 解压**原始上传版**源码 → 清掉陈旧的 __pycache__ → 用更新包覆盖
#   3. 断言覆盖后 app/ 与 main.py 和当前工作区**逐字节一致**（不漏文件）
#   4. 在覆盖后的目录里跑基础测试 + 功能测试（可选再跑真实网络实战）
#
# 用法：
#   bash tests/verify_release.sh                      # 验证聚合包 v0.3.0
#   bash tests/verify_release.sh v0.2.3 <zip名>       # 验证指定的历史包
#   REALWORLD=1 bash tests/verify_release.sh          # 追加真实网络实战测试
#
# 说明：本机直连 github.com 不稳定，默认走 gh-proxy 通道；REPO 可覆盖。

set -euo pipefail

REPO="${REPO:-456123739/pulses-easier-updates}"
TAG="${1:-v0.3.0}"
PKG="${2:-pulses-easier-download-engine-v0.3.0-aggregate.zip}"
PROXY="${PROXY:-https://gh-proxy.com/}"
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIG_ZIP="${ORIG_ZIP:-/home/admin/.dsh/attachments/v1/files/09/09911d1b2a13e3eadcd1090a12e48c584eb635e050b968d86da45f84fb831d0c/Pulses Easier.zip}"
WORK="$WS/_verify/$TAG"

echo "=================================================================="
echo "验证已发布更新包：$TAG / $PKG"
echo "工作目录：$WORK"
echo "=================================================================="

[ -f "$ORIG_ZIP" ] || { echo "✗ 找不到原始源码包：$ORIG_ZIP"; exit 1; }

rm -rf "$WORK"
mkdir -p "$WORK"

# ---------------------------------------------------------------- 1
echo
echo "[1] 匿名下载已发布包"
URL="${PROXY}https://github.com/${REPO}/releases/download/${TAG}/${PKG}"
echo "    $URL"
curl -fsSL -o "$WORK/pub.zip" "$URL"
PUB_MD5="$(md5sum "$WORK/pub.zip" | cut -d' ' -f1)"
LOCAL_MD5="$(md5sum "$WS/release/$PKG" 2>/dev/null | cut -d' ' -f1 || echo '(本地无此文件)')"
echo "    下载 md5 : $PUB_MD5"
echo "    本地 md5 : $LOCAL_MD5"
if [ "$PUB_MD5" = "$LOCAL_MD5" ]; then
    echo "    ✓ 与本地文件一致"
else
    echo "    ✗ 与本地文件不一致（继续验证下载到的这一份）"
fi

# ---------------------------------------------------------------- 2
echo
echo "[2] 原始源码 + 覆盖更新包"
mkdir -p "$WORK/applied"
unzip -q "$ORIG_ZIP" -d "$WORK/applied"
find "$WORK/applied" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "    原始文件数：$(find "$WORK/applied" -type f | wc -l)"
unzip -q -o "$WORK/pub.zip" -d "$WORK/applied"
echo "    覆盖后文件数：$(find "$WORK/applied" -type f | wc -l)"
echo "    包内条目："
unzip -Z1 "$WORK/pub.zip" | sed 's/^/      /'

# ---------------------------------------------------------------- 3
echo
echo "[3] 断言覆盖结果"

# 3a：包内每个文件都必须原样落到目标位置（证明覆盖真的生效、没被跳过）
BAD=0
while IFS= read -r entry; do
    case "$entry" in */) continue ;; esac
    if ! cmp -s "$WORK/applied/$entry" <(unzip -p "$WORK/pub.zip" "$entry"); then
        echo "    ✗ 覆盖后与包内内容不一致：$entry"
        BAD=1
    fi
done < <(unzip -Z1 "$WORK/pub.zip")
[ "$BAD" = "0" ] && echo "    ✓ 包内 $(unzip -Z1 "$WORK/pub.zip" | grep -vc '/$') 个文件都已正确覆盖"

# 3b：与**当前工作区**比对。
#     验证「最新包」用 STRICT=1，要求 app/ 与 main.py 逐字节一致；
#     验证历史包时只要求"差异不超过版本号文件"。
DIFF_OUT="$(diff -r -q "$WORK/applied/app" "$WS/app" 2>&1 || true)"
DIFF_FILES="$(printf '%s\n' "$DIFF_OUT" | sed -n 's/^Files .*applied\/\(.*\) and .* differ$/\1/p')"
cmp -s "$WORK/applied/main.py" "$WS/main.py" || DIFF_FILES="$(printf '%s\nmain.py\n' "$DIFF_FILES")"
DIFF_FILES="$(printf '%s\n' "$DIFF_FILES" | sed '/^$/d' | sort)"

if [ -z "$DIFF_FILES" ]; then
    echo "    ✓ app/ 与 main.py 与当前工作区逐字节一致（无遗漏）"
elif [ "${STRICT:-0}" = "1" ]; then
    echo "    ✗ 期望与工作区完全一致，但有差异："
    printf '      %s\n' $DIFF_FILES
    exit 1
else
    echo "    · 与当前工作区的差异（历史包属正常）："
    printf '      %s\n' $DIFF_FILES
    EXTRA="$(printf '%s\n' $DIFF_FILES | grep -v '^app/core/__init__.py$' || true)"
    if [ -z "$EXTRA" ]; then
        echo "    ✓ 差异仅为版本号文件，没有遗漏"
    else
        echo "    ✗ 出现了版本号之外的差异（说明有文件漏进包）："
        printf '      %s\n' $EXTRA
        exit 1
    fi
fi

# ---------------------------------------------------------------- 4
echo
echo "[4] 在覆盖后的目录里跑测试"
cp -a "$WS/tests" "$WORK/applied/tests"
cd "$WORK/applied"
python3 -c "import sys; sys.path.insert(0,'.'); from app.core import __version__; print('    app.core.__version__ =', __version__)"
echo "    --- 基础测试 ---"
python3 -W ignore::ResourceWarning tests/test_basic.py 2>&1 | tail -4
echo "    --- 功能测试 ---"
python3 -W ignore::ResourceWarning tests/test_downloader.py 2>&1 | tail -4

if [ "${REALWORLD:-0}" = "1" ]; then
    echo "    --- 真实网络实战（Modrinth 10 + CurseForge 10）---"
    python3 -u tests/realworld_test.py --reps 1 2>&1 | tail -30
fi

echo
echo "=================================================================="
echo "验证完成：$TAG"
echo "=================================================================="
