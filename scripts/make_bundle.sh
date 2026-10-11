#!/usr/bin/env bash
# Сборка дистрибутива: dist/korrektor-<версия>.tar.gz и самораспаковывающийся
# dist/korrektor-<версия>-installer.sh (установка одной командой без распаковки).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd); V=$(cat "$ROOT/VERSION")
mkdir -p "$ROOT/dist"; T=$(mktemp -d)
mkdir -p "$T/korrektor-$V"
# служебное не попадает в дистрибутив: история git, кэши, файлы macOS (.DS_Store, ._*)
tar -C "$ROOT" --exclude=./dist --exclude=./data --exclude='__pycache__' --exclude=./venv --exclude='*.pyc' --exclude=./.pytest_cache \
    --exclude=./.git --exclude='.DS_Store' --exclude='._*' --exclude=./app/prompts/custom_prompt.md -cf - . | tar -C "$T/korrektor-$V" -xf -
# сборка (коммит и его дата) — видна в веб-интерфейсе, в «korrektor version» и при обновлении
B=""
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  B=$(git -C "$ROOT" log -1 --format='%h %cs')
  if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no -- . ':!dist')" ]; then B="${B%% *}+ ${B#* }"; fi   # «+»: есть незафиксированные изменения
  echo "$B" > "$T/korrektor-$V/app/BUILD"
fi
tar czf "$ROOT/dist/korrektor-$V.tar.gz" -C "$T" "korrektor-$V"
OUT="$ROOT/dist/korrektor-$V-installer.sh"
cat >"$OUT" <<'HEAD'
#!/usr/bin/env bash
# Корректор — самораспаковывающийся установщик.
#   sudo bash korrektor-VERSION-installer.sh [ключи install.sh]   — установка
#   sudo bash korrektor-VERSION-installer.sh --update             — обновление
set -e
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
sed '1,/^__PAYLOAD__$/d' "$0" | base64 -d | tar xz -C "$T"
D=$(ls -d "$T"/korrektor-*)
if [ "${1:-}" = "--update" ]; then shift; bash "$D/scripts/update.sh" --source "$D" "$@"
else bash "$D/scripts/install.sh" --source "$D" "$@"; fi
exit 0
__PAYLOAD__
HEAD
sed -i "s/VERSION/$V/g" "$OUT"
base64 "$ROOT/dist/korrektor-$V.tar.gz" >>"$OUT"
chmod +x "$OUT"; rm -rf "$T"
echo "Готово: $ROOT/dist/korrektor-$V.tar.gz (версия $V${B:+, сборка $B})"
echo "        $OUT"
