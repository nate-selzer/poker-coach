#!/usr/bin/env bash
# Package the claude.ai version of the skill.
#   tools/package_chat.sh <console_solver binary> <TexasSolver resources dir> [out.zip]
# The zip's SKILL.md is skill/chat.md plus skill/SKILL.md from "## Game" on.
set -euo pipefail
BIN=${1:?usage: package_chat.sh <console_solver> <resources dir> [out.zip]}
RES=${2:?usage: package_chat.sh <console_solver> <resources dir> [out.zip]}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${3:-$ROOT/dist/poker-coach-chat.zip}
mkdir -p "$(dirname "$OUT")" && OUT=$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")
STAGE=$(mktemp -d)/poker-coach-chat
mkdir -p "$STAGE/bin" "$STAGE/resources"
cp -R "$ROOT/skill/scripts" "$ROOT/skill/data" "$STAGE/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
{ cat "$ROOT/skill/chat.md"; echo; sed -n '/^## Game$/,$p' "$ROOT/skill/SKILL.md"; } > "$STAGE/SKILL.md"
grep -q '^## Game$' "$STAGE/SKILL.md"
cp "$BIN" "$STAGE/bin/console_solver"
xz -9 -c "$RES/compairer/card5_dic_sorted.txt" > "$STAGE/resources/card5_dic_sorted.txt.xz"
cp "$ROOT/LICENSE" "$STAGE/LICENSE"
cp "$ROOT/solver/NOTICE.md" "$STAGE/NOTICE.md"
rm -f "$OUT"
(cd "$(dirname "$STAGE")" && zip -qr "$OUT" poker-coach-chat)
echo "$OUT"
