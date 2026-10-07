#!/usr/bin/env bash
# =============================================================================
#  Корректор — обновление одной командой
#    sudo bash scripts/update.sh                      из папки с новой версией
#    sudo bash korrektor-<версия>-installer.sh --update
#    sudo korrektor update --source URL|PATH          из архива, git или папки
#  Перед обновлением делается резервная копия данных и настроек
#  (/var/backups/korrektor). Если новая версия не запустится — откат.
#  Ключи: --source URL|PATH, --no-backup, --yes
# =============================================================================
set -Eeuo pipefail
APP=korrektor; PREFIX=/opt/$APP; DATA=/var/lib/$APP; ENVF=/etc/$APP/$APP.env
SOURCE=""; BACKUP=1; LOG=/var/log/$APP-update.log
c_ok="\033[1;32m"; c_info="\033[1;34m"; c_err="\033[1;31m"; c_0="\033[0m"
say() { echo -e "${c_info}==>${c_0} $*"; echo "[$(date +%T)] $*" >>"$LOG"; }
ok()  { echo -e "${c_ok}✔${c_0} $*"; }
die() { echo -e "${c_err}✖ $*${c_0}"; exit 1; }
# копирование дерева: старые файлы программы удаляются, venv и свой промпт сохраняются
copy_tree() {  # copy_tree ИСТОЧНИК НАЗНАЧЕНИЕ
  local src="$1" dst="$2"
  mkdir -p "$dst"
  find "$dst" -mindepth 1 -maxdepth 1 ! -name venv ! -name data ! -name app -exec rm -rf {} + 2>/dev/null || true
  if [ -d "$dst/app" ]; then
    find "$dst/app" -mindepth 1 -maxdepth 1 ! -name prompts -exec rm -rf {} + 2>/dev/null || true
    find "$dst/app/prompts" -mindepth 1 ! -name custom_prompt.md -exec rm -rf {} + 2>/dev/null || true
  fi
  tar -C "$src" --exclude=./venv --exclude=./data --exclude=./dist --exclude='__pycache__' \
      --exclude=./app/prompts/custom_prompt.md -cf - . | tar -C "$dst" -xf -
}

while [ $# -gt 0 ]; do case "$1" in
  --source) SOURCE="$2"; shift 2;; --no-backup) BACKUP=0; shift;; --yes|-y) shift;;
  -h|--help) sed -n '2,12p' "$0"; exit 0;; *) die "Неизвестный ключ $1";; esac; done
[ "$(id -u)" -eq 0 ] || die "Запустите: sudo bash $0"
[ -d $PREFIX ] || die "Корректор не установлен ($PREFIX нет). Используйте install.sh"
: >"$LOG"

HAS_SD=0; command -v systemctl >/dev/null && [ -d /run/systemd/system ] && HAS_SD=1
svc() { [ $HAS_SD -eq 1 ] && systemctl "$@"; return 0; }

# источник новой версии
TMP=""
if [ -z "$SOURCE" ]; then
  SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  [ "$SRC" = "$PREFIX" ] && die "Укажите источник новой версии: --source архив.tar.gz | git-URL | папка"
elif [ -d "$SOURCE" ]; then SRC=$(cd "$SOURCE" && pwd)
elif [[ "$SOURCE" == *.git ]]; then TMP=$(mktemp -d); git clone -q --depth 1 "$SOURCE" "$TMP"; SRC=$TMP
else
  TMP=$(mktemp -d)
  if [[ "$SOURCE" == http* ]]; then curl -fsSL "$SOURCE" -o $TMP/s.tgz; else cp "$SOURCE" $TMP/s.tgz; fi
  tar xzf $TMP/s.tgz -C $TMP; SRC=$(dirname "$(find $TMP -path '*/app/main.py' | head -1)")/..
fi
SRC=$(cd "$SRC" && pwd)
[ -f "$SRC/app/main.py" ] || die "В $SRC нет файлов программы"
OLD=$(cat $PREFIX/VERSION 2>/dev/null || echo ?); NEW=$(cat "$SRC/VERSION" 2>/dev/null || echo ?)
say "Обновление $OLD → $NEW"

if [ $BACKUP -eq 1 ]; then
  mkdir -p /var/backups/$APP
  B=/var/backups/$APP/before-update-$(date +%Y%m%d-%H%M%S).tgz
  tar czf "$B" -C / var/lib/$APP etc/$APP opt/$APP --exclude=opt/$APP/venv 2>>"$LOG" || true
  ok "Резервная копия: $B"
fi
ROLL=$(mktemp -d); tar -C $PREFIX --exclude=./venv -cf - . | tar -C $ROLL -xf -

svc stop $APP 2>/dev/null || true
say "Копирование файлов"
copy_tree "$SRC" $PREFIX
say "Обновление зависимостей Python"
$PREFIX/venv/bin/pip install -q -r $PREFIX/requirements.txt >>"$LOG" 2>&1 || die "Ошибка pip (журнал $LOG)"
chown $APP:$APP $PREFIX/app/prompts 2>/dev/null || true
svc daemon-reload 2>/dev/null || true
svc start $APP
PORT=$(grep '^KORREKTOR_PORT=' $ENVF | cut -d= -f2)
[ $HAS_SD -eq 1 ] && for i in $(seq 1 30); do curl -fs http://127.0.0.1:$PORT/api/health >/dev/null 2>&1 && break; sleep 1; done
if [ $HAS_SD -eq 1 ] && ! curl -fs http://127.0.0.1:$PORT/api/health >/dev/null 2>&1; then
  echo -e "${c_err}Новая версия не запустилась — откат на $OLD${c_0}"
  systemctl stop $APP || true
  copy_tree $ROLL $PREFIX
  $PREFIX/venv/bin/pip install -q -r $PREFIX/requirements.txt >>"$LOG" 2>&1 || true
  systemctl start $APP; die "Обновление отменено. Журнал службы: journalctl -u $APP -n 80"
fi
rm -rf "$ROLL" ${TMP:+"$TMP"}
ok "Корректор обновлен до версии $NEW. Данные, настройки и пароль сохранены."
