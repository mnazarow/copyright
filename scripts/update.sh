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
      --exclude=./app/prompts/custom_prompt.md -cf - . | tar -C "$dst" --no-same-owner -xf -
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
[ "$SRC" = "$PREFIX" ] && die "Источник совпадает с установленной программой ($PREFIX) — укажите папку или архив новой версии"
OLD=$(cat $PREFIX/VERSION 2>/dev/null || echo ?); NEW=$(cat "$SRC/VERSION" 2>/dev/null || echo ?)
say "Обновление $OLD → $NEW"

if [ $BACKUP -eq 1 ]; then
  mkdir -p /var/backups/$APP
  B=/var/backups/$APP/before-update-$(date +%Y%m%d-%H%M%S).tgz
  # база — согласованным снимком (служба работает и может писать в нее), остальное — как есть;
  # в копии korrektor.env и настройки с ключами — архив только для root (umask лишь внутри скобок)
  SNAPD=$(mktemp -d); TARF="${B%.tgz}.tar"
  if [ -f $DATA/korrektor.db ] && $PREFIX/venv/bin/python -c "import sqlite3,sys
s=sqlite3.connect(sys.argv[1], timeout=30); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
       $DATA/korrektor.db $SNAPD/korrektor.db 2>>"$LOG"; then
    ( umask 077; tar cf "$TARF" --exclude=opt/$APP/venv --exclude='*/pages/*.png' --exclude=var/lib/$APP/korrektor.db \
        --exclude=var/lib/$APP/korrektor.db-wal --exclude=var/lib/$APP/korrektor.db-shm \
        -C / var/lib/$APP etc/$APP opt/$APP 2>>"$LOG" \
      && tar rf "$TARF" --transform "s|^korrektor.db\$|var/lib/$APP/korrektor.db|" -C "$SNAPD" korrektor.db 2>>"$LOG" \
      && gzip -c "$TARF" >"$B" ) && RCB=0 || RCB=$?
  else
    ( umask 077; tar czf "$B" --exclude=opt/$APP/venv --exclude='*/pages/*.png' -C / var/lib/$APP etc/$APP opt/$APP 2>>"$LOG" ) && RCB=0 || RCB=$?
  fi
  rm -rf "$SNAPD" "$TARF"
  [ $RCB -eq 0 ] && ok "Резервная копия: $B" || die "Резервная копия не создана (место на диске?). Журнал: $LOG; без копии: --no-backup"
  ls -1t /var/backups/$APP/before-update-*.tgz 2>/dev/null | tail -n +6 | xargs -r rm -f   # хранить 5 последних
fi
# зависимости ставятся до остановки службы: если PyPI недоступен, работающая версия не пострадает
say "Обновление зависимостей Python"
$PREFIX/venv/bin/pip install -q -r "$SRC/requirements.txt" >>"$LOG" 2>&1 || die "Ошибка pip — служба не остановлена, версия $OLD работает (журнал $LOG)"

ROLL=$(mktemp -d); tar -C $PREFIX --exclude=./venv -cf - . | tar -C $ROLL -xf -
rollback() {
  trap - ERR
  echo -e "${c_err}Ошибка обновления — откат на $OLD${c_0}"
  svc stop $APP 2>/dev/null || true
  copy_tree "$ROLL" $PREFIX
  chown -R root:root $PREFIX/app $PREFIX/scripts 2>/dev/null || true
  chown -R $APP:$APP $PREFIX/app/prompts 2>/dev/null || true
  $PREFIX/venv/bin/pip install -q -r $PREFIX/requirements.txt >>"$LOG" 2>&1 || true
  svc start $APP
  rm -rf "$ROLL" ${TMP:+"$TMP"}
}
trap 'rollback; die "Обновление отменено (журнал $LOG)"' ERR

svc stop $APP 2>/dev/null || true
say "Копирование файлов"
copy_tree "$SRC" $PREFIX
chown -R root:root $PREFIX/app $PREFIX/scripts $PREFIX/docs 2>/dev/null || true
find $PREFIX -maxdepth 1 -type f -exec chown root:root {} + 2>/dev/null || true
chown -R $APP:$APP $PREFIX/app/prompts 2>/dev/null || true
mkdir -p /var/backups/$APP && chown root:$APP /var/backups/$APP && chmod 2770 /var/backups/$APP
# команда управления korrektor — из новой версии установщика (новые подкоманды: verify, restore, 2fa-off…)
if [ -f $PREFIX/scripts/install.sh ]; then
  sed -n "/^cat >\/usr\/local\/bin\/korrektor <<'EOF'$/,/^EOF$/p" $PREFIX/scripts/install.sh | sed '1d;$d' > /usr/local/bin/korrektor.new \
    && [ -s /usr/local/bin/korrektor.new ] && chmod 755 /usr/local/bin/korrektor.new && mv /usr/local/bin/korrektor.new /usr/local/bin/korrektor \
    || rm -f /usr/local/bin/korrektor.new
  rm -f /usr/local/bin/korrektor-passwd                       # была ссылкой и только показывала справку
  printf '#!/usr/bin/env bash\nexec /usr/local/bin/korrektor passwd "$@"\n' >/usr/local/bin/korrektor-passwd
  chmod 755 /usr/local/bin/korrektor-passwd
fi
# настройки службы для устойчивой работы (дополнение к unit-файлу прежних версий установщика)
if [ $HAS_SD -eq 1 ] && [ -f /etc/systemd/system/$APP.service ]; then
  mkdir -p /etc/systemd/system/$APP.service.d
  cat >/etc/systemd/system/$APP.service.d/10-stability.conf <<'EOF'
[Service]
# задание обрабатывается отдельным процессом: если ядро завершит его из-за нехватки памяти, служба продолжает
# работу (по умолчанию systemd останавливал бы всю службу); MALLOC_ARENA_MAX — память не «оседает» в потоках
OOMPolicy=continue
Environment=MALLOC_ARENA_MAX=2 PYTHONFAULTHANDLER=1
EOF
fi
svc daemon-reload 2>/dev/null || true
svc start $APP
PORT=$(grep '^KORREKTOR_PORT=' $ENVF | cut -d= -f2)
H=$(grep '^KORREKTOR_HOST=' $ENVF | cut -d= -f2)
case "$H" in ""|0.0.0.0|::) HC=127.0.0.1;; *:*) HC="[$H]";; *) HC="$H";; esac   # служба может слушать только адрес в сети
[ $HAS_SD -eq 1 ] && for i in $(seq 1 30); do curl -fs "http://$HC:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
trap - ERR
if [ $HAS_SD -eq 1 ] && ! curl -fs "http://$HC:$PORT/api/health" >/dev/null 2>&1; then
  echo -e "${c_err}Новая версия не запустилась${c_0}"
  rollback; die "Обновление отменено. Журнал службы: journalctl -u $APP -n 80"
fi
rm -rf "$ROLL" ${TMP:+"$TMP"}
ok "Корректор обновлен до версии $NEW. Данные, настройки и пароль сохранены."
