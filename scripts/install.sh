#!/usr/bin/env bash
# =============================================================================
#  Корректор — установка одной командой на чистый Linux
#  Поддерживаются: Ubuntu 20.04+, Debian 11+, Astra Linux, РЕД ОС, ALT Linux,
#  RHEL/Rocky/AlmaLinux/CentOS Stream 8+, Fedora, openSUSE, Arch.
#
#  Запуск из распакованной папки:   sudo bash scripts/install.sh
#  Запуск самораспаковывающегося файла:   sudo bash korrektor-<версия>-installer.sh
#
#  Ключи:
#    --port N              порт веб-сервиса (по умолчанию 8080)
#    --listen ADDR         адрес прослушивания (по умолчанию 0.0.0.0; с --with-nginx — 127.0.0.1)
#    --password PASS       пароль администратора (по умолчанию генерируется)
#    --with-nginx          поставить nginx как обратный прокси на порт 80
#    --domain NAME         имя сайта для nginx (по умолчанию _ — любой)
#    --with-ocr            поставить Tesseract с русским языком (распознавание сканов)
#    --with-languagetool   поставить локальный сервер LanguageTool (нужно ~1 ГБ, Java)
#    --with-office         поставить LibreOffice Writer: прием документов DOC, DOCX, ODT, RTF
#    --full                все дополнения сразу: --with-ocr --with-languagetool --with-office
#    --source URL|PATH     взять программу из архива .tar.gz, git-репозитория или папки
#    --open-firewall       открыть порт в firewalld/ufw
#    --yes                 не задавать вопросов
# =============================================================================
set -Eeuo pipefail

APP=korrektor
PREFIX=/opt/$APP
DATA=/var/lib/$APP
CONF=/etc/$APP
ENVF=$CONF/$APP.env
PORT=8080
LISTEN=""
PASSWORD=""
WITH_NGINX=0
DOMAIN="_"
WITH_OCR=0
WITH_OFFICE=0
WITH_LT=0
SOURCE=""
OPEN_FW=0
YES=0
LOG=/var/log/$APP-install.log
PORT_SET=0; LISTEN_SET=0

c_ok="\033[1;32m"; c_info="\033[1;34m"; c_warn="\033[1;33m"; c_err="\033[1;31m"; c_0="\033[0m"
say()  { echo -e "${c_info}==>${c_0} $*"; echo "[$(date +%T)] $*" >>"$LOG"; }
ok()   { echo -e "${c_ok}✔${c_0} $*"; echo "[$(date +%T)] OK $*" >>"$LOG"; }
warn() { echo -e "${c_warn}!${c_0} $*"; echo "[$(date +%T)] WARN $*" >>"$LOG"; }
die()  { echo -e "${c_err}✖ $*${c_0}"; echo "[$(date +%T)] ERROR $*" >>"$LOG"; echo "Журнал установки: $LOG"; exit 1; }
trap 'die "Установка прервана на строке $LINENO. Подробности в журнале."' ERR

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; PORT_SET=1; shift 2;;
    --listen) LISTEN="$2"; LISTEN_SET=1; shift 2;;
    --password) PASSWORD="$2"; shift 2
                case "$PASSWORD" in *[[:space:]]*|*\"*|*\'*|*\$*|*\`*|*\\*) echo "Пароль не должен содержать пробелы, кавычки, \$, \` и \\"; exit 1;; esac;;
    --with-nginx) WITH_NGINX=1; shift;;
    --domain) DOMAIN="$2"; shift 2;;
    --with-ocr) WITH_OCR=1; shift;;
    --with-office) WITH_OFFICE=1; shift;;
    --full) WITH_OCR=1; WITH_LT=1; WITH_OFFICE=1; shift;;
    --with-languagetool) WITH_LT=1; shift;;
    --source) SOURCE="$2"; shift 2;;
    --open-firewall) OPEN_FW=1; shift;;
    --yes|-y) YES=1; shift;;
    -h|--help) sed -n '2,25p' "$0"; exit 0;;
    *) die "Неизвестный ключ: $1 (справка: --help)";;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Запустите от администратора: sudo bash $0"
mkdir -p "$(dirname "$LOG")"; : >"$LOG"
case "$PORT" in ""|*[!0-9]*) die "--port: нужен номер порта";; esac
[ -n "$LISTEN" ] || { [ $WITH_NGINX -eq 1 ] && LISTEN_SET=1; }
[ -z "$LISTEN" ] && { [ $WITH_NGINX -eq 1 ] && LISTEN=127.0.0.1 || LISTEN=0.0.0.0; }

echo -e "${c_info}"
echo "  ┌──────────────────────────────────────────────┐"
echo "  │  КОРРЕКТОР — установка веб-сервиса вычитки   │"
echo "  └──────────────────────────────────────────────┘"
echo -e "${c_0}"

# ---------------------------------------------------------------- 1. система
. /etc/os-release 2>/dev/null || true
say "Система: ${PRETTY_NAME:-неизвестно}"
PM=""
for p in apt-get dnf yum zypper pacman; do command -v $p >/dev/null 2>&1 && { PM=$p; break; }; done
[ -n "$PM" ] || die "Не найден менеджер пакетов (apt, dnf, yum, zypper, pacman)"
say "Менеджер пакетов: $PM"

pkg_install() {
  case "$PM" in
    apt-get) DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" >>"$LOG" 2>&1;;
    dnf|yum) $PM install -y -q "$@" >>"$LOG" 2>&1;;
    zypper) zypper --non-interactive install -y "$@" >>"$LOG" 2>&1;;
    pacman) pacman -S --noconfirm --needed "$@" >>"$LOG" 2>&1;;
  esac
}
pkg_try() { for p in "$@"; do pkg_install "$p" && return 0; done; return 1; }
# копирование дерева: старые файлы программы удаляются, venv и свой промпт сохраняются
# сборка — коммит и его дата («a382dd4 2026-10-11»): у одной версии бывает несколько сборок.
# В дистрибутиве — файл app/BUILD; у папки с исходниками из git — берется из git.
build_of() {  # build_of ПАПКА
  if [ -s "$1/app/BUILD" ]; then head -1 "$1/app/BUILD"; return 0; fi
  command -v git >/dev/null 2>&1 || return 0
  git -c safe.directory="$1" -C "$1" rev-parse --git-dir >/dev/null 2>&1 || return 0
  local b; b=$(git -c safe.directory="$1" -C "$1" log -1 --format='%h %cs' 2>/dev/null) || return 0
  [ -n "$(git -c safe.directory="$1" -C "$1" status --porcelain --untracked-files=no -- . ':!dist' 2>/dev/null)" ] && b="${b%% *}+ ${b#* }"
  echo "$b"
}
ver_full() {  # ver_full ВЕРСИЯ СБОРКА → «1.7.0 (сборка a382dd4 2026-10-11)»
  echo "$1${2:+ (сборка $2)}"
}
copy_tree() {  # copy_tree ИСТОЧНИК НАЗНАЧЕНИЕ
  local src="$1" dst="$2"
  mkdir -p "$dst"
  find "$dst" -mindepth 1 -maxdepth 1 ! -name venv ! -name data ! -name app -exec rm -rf {} + 2>/dev/null || true
  if [ -d "$dst/app" ]; then
    find "$dst/app" -mindepth 1 -maxdepth 1 ! -name prompts -exec rm -rf {} + 2>/dev/null || true
    find "$dst/app/prompts" -mindepth 1 ! -name custom_prompt.md -exec rm -rf {} + 2>/dev/null || true
  fi
  tar -C "$src" --exclude=./venv --exclude=./data --exclude=./dist --exclude='__pycache__' --exclude=./.git \
      --exclude='.DS_Store' --exclude=./app/prompts/custom_prompt.md -cf - . | tar -C "$dst" --no-same-owner -xf -
}


say "Обновление списков пакетов"
case "$PM" in
  apt-get) apt-get update -q >>"$LOG" 2>&1 || warn "apt-get update завершился с ошибкой";;
  dnf|yum) $PM -y -q makecache >>"$LOG" 2>&1 || true; pkg_try epel-release || true;;
  zypper) zypper --non-interactive refresh >>"$LOG" 2>&1 || true;;
  pacman) pacman -Sy --noconfirm >>"$LOG" 2>&1 || true;;
esac

say "Установка системных пакетов"
case "$PM" in
  apt-get) pkg_install python3 python3-venv python3-pip ca-certificates curl tar gzip openssl \
             || die "Не удалось установить python3";;
  dnf|yum) pkg_install python3 python3-pip ca-certificates curl tar gzip openssl || die "Не удалось установить python3";;
  zypper)  pkg_install python3 python3-pip ca-certificates curl tar gzip openssl || die "Не удалось установить python3";;
  pacman)  pkg_install python python-pip ca-certificates curl tar gzip openssl || die "Не удалось установить python";;
esac
PY=$(command -v python3 || command -v python)
PYV=$($PY -c 'import sys;print("%d.%d"%sys.version_info[:2])')
$PY -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' || die "Нужен Python 3.9 или новее, установлен $PYV"
ok "Python $PYV"

say "Словарь Hunspell (русский)"
case "$PM" in
  apt-get) pkg_try hunspell >/dev/null; pkg_try hunspell-ru myspell-ru || warn "русский словарь Hunspell не найден в репозитории";;
  dnf|yum) pkg_try hunspell >/dev/null; pkg_try hunspell-ru || warn "русский словарь Hunspell не найден";;
  zypper)  pkg_try hunspell >/dev/null; pkg_try myspell-ru_RU myspell-russian || warn "русский словарь Hunspell не найден";;
  pacman)  pkg_try hunspell >/dev/null; pkg_try hunspell-ru || warn "русский словарь Hunspell не найден (AUR)";;
esac
command -v hunspell >/dev/null && ok "Hunspell установлен" || warn "Hunspell не установлен — словарная проверка будет пропускаться"

if [ $WITH_OFFICE -eq 1 ]; then
  say "LibreOffice Writer (прием документов Word) — 200–400 МБ"
  case "$PM" in
    apt-get) pkg_try libreoffice-writer-nogui libreoffice-writer || warn "LibreOffice не установлен";
             pkg_try fonts-liberation fonts-dejavu-core fonts-crosextra-carlito >/dev/null || true;;
    dnf|yum) pkg_try libreoffice-writer libreoffice-headless || warn "LibreOffice не установлен";
             pkg_try liberation-fonts dejavu-sans-fonts >/dev/null || true;;
    zypper)  pkg_try libreoffice-writer || warn "LibreOffice не установлен";;
    pacman)  pkg_try libreoffice-fresh || warn "LibreOffice не установлен";;
  esac
  command -v soffice >/dev/null && ok "LibreOffice установлен: документы Word принимаются"
fi
if [ $WITH_OCR -eq 1 ]; then
  say "Tesseract OCR"
  case "$PM" in
    apt-get) pkg_install tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng || warn "Tesseract не установлен";;
    dnf|yum) pkg_try tesseract tesseract-langpack-rus || warn "Tesseract не установлен";;
    zypper)  pkg_try tesseract-ocr tesseract-ocr-traineddata-russian || warn "Tesseract не установлен";;
    pacman)  pkg_try tesseract tesseract-data-rus || warn "Tesseract не установлен";;
  esac
fi

# ---------------------------------------------------------------- 2. исходники
SRC_DIR=""
cleanup_src=""
if [ -n "$SOURCE" ]; then
  if [ -d "$SOURCE" ]; then SRC_DIR=$(cd "$SOURCE" && pwd)
  elif [[ "$SOURCE" == *.git ]] || [[ "$SOURCE" == git@* ]]; then
    pkg_try git >/dev/null; SRC_DIR=$(mktemp -d); cleanup_src=$SRC_DIR
    say "Загрузка из git: $SOURCE"; git clone -q --depth 1 "$SOURCE" "$SRC_DIR" >>"$LOG" 2>&1 || die "git clone не удался"
  else
    SRC_DIR=$(mktemp -d); cleanup_src=$SRC_DIR
    say "Загрузка архива: $SOURCE"
    if [[ "$SOURCE" == http* ]]; then curl -fsSL "$SOURCE" -o "$SRC_DIR/src.tgz" || die "не удалось скачать $SOURCE"
    else cp "$SOURCE" "$SRC_DIR/src.tgz"; fi
    tar xzf "$SRC_DIR/src.tgz" -C "$SRC_DIR"
    [ -f "$SRC_DIR/app/main.py" ] || SRC_DIR=$(dirname "$(find "$SRC_DIR" -path '*/app/main.py' | head -1)")/..
  fi
else
  SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fi
SRC_DIR=$(cd "$SRC_DIR" && pwd)
[ -f "$SRC_DIR/app/main.py" ] || die "Не найдены файлы программы в $SRC_DIR (ожидается app/main.py)"
[ "$SRC_DIR" = "$(readlink -f $PREFIX 2>/dev/null || echo $PREFIX)" ] && die "Источник совпадает с $PREFIX: запустите установщик из распакованного архива или дистрибутива"
VERSION=$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo dev)
BUILD_ID=$(build_of "$SRC_DIR")
VERSION_FULL=$(ver_full "$VERSION" "$BUILD_ID")
ok "Исходники: $SRC_DIR (версия $VERSION_FULL)"

# ---------------------------------------------------------------- 3. пользователь и папки
if ! id $APP >/dev/null 2>&1; then
  useradd --system --home-dir $DATA --shell /usr/sbin/nologin $APP 2>/dev/null \
    || useradd -r -d $DATA -s /sbin/nologin $APP
  ok "Создан системный пользователь $APP"
fi
mkdir -p $PREFIX $DATA $CONF /var/backups/$APP
chown root:$APP /var/backups/$APP && chmod 2770 /var/backups/$APP   # служба пишет сюда копии по расписанию
say "Копирование программы в $PREFIX"
copy_tree "$SRC_DIR" $PREFIX
if [ -n "$BUILD_ID" ]; then echo "$BUILD_ID" > $PREFIX/app/BUILD; fi  # сборка — для вывода версии
chown -R root:root $PREFIX
chown -R $APP:$APP $DATA
mkdir -p $PREFIX/app/prompts && chown -R $APP:$APP $PREFIX/app/prompts

# ---------------------------------------------------------------- 4. Python-окружение
say "Виртуальное окружение Python и зависимости (1–3 минуты)"
[ -x $PREFIX/venv/bin/python ] || $PY -m venv $PREFIX/venv >>"$LOG" 2>&1 || die "Не удалось создать venv (пакет python3-venv)"
$PREFIX/venv/bin/pip install -q --upgrade pip >>"$LOG" 2>&1 || true
$PREFIX/venv/bin/pip install -q -r $PREFIX/requirements.txt >>"$LOG" 2>&1 || die "Не удалось установить зависимости Python (см. журнал; нужен доступ к PyPI или зеркалу)"
ok "Зависимости установлены"

# ---------------------------------------------------------------- 5. конфигурация
if [ ! -f "$ENVF" ]; then
  [ -z "$PASSWORD" ] && PASSWORD=$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-14)
  cat >"$ENVF" <<EOF
# Конфигурация службы «Корректор». После изменения: sudo systemctl restart $APP
KORREKTOR_ADMIN_PASSWORD=$PASSWORD
KORREKTOR_SECRET=$(openssl rand -hex 32)
KORREKTOR_DATA=$DATA
KORREKTOR_HOST=$LISTEN
KORREKTOR_PORT=$PORT
KORREKTOR_LOG=INFO
EOF
  chmod 640 "$ENVF"; chown root:$APP "$ENVF"
  ok "Создан $ENVF"
else
  PASSWORD=$(grep '^KORREKTOR_ADMIN_PASSWORD=' "$ENVF" | cut -d= -f2-)
  # повторный запуск: меняются только явно заданные --port / --listen, остальное — как было
  if [ $PORT_SET -eq 1 ]; then sed -i "s/^KORREKTOR_PORT=.*/KORREKTOR_PORT=$PORT/" "$ENVF"
  else PORT=$(grep '^KORREKTOR_PORT=' "$ENVF" | cut -d= -f2); PORT=${PORT:-8080}; fi
  if [ $LISTEN_SET -eq 1 ]; then sed -i "s/^KORREKTOR_HOST=.*/KORREKTOR_HOST=$LISTEN/" "$ENVF"
  else LISTEN=$(grep '^KORREKTOR_HOST=' "$ENVF" | cut -d= -f2); LISTEN=${LISTEN:-0.0.0.0}; fi
  ok "Конфигурация $ENVF сохранена (пароль не изменен, адрес $LISTEN:$PORT)"
fi

# ---------------------------------------------------------------- 6. LanguageTool
if [ $WITH_LT -eq 1 ]; then
  say "LanguageTool (сервер грамматики) — загрузка ~250 МБ"
  case "$PM" in
    apt-get) pkg_try default-jre-headless openjdk-17-jre-headless;;
    dnf|yum) pkg_try java-17-openjdk-headless java-11-openjdk-headless;;
    zypper)  pkg_try java-17-openjdk-headless;;
    pacman)  pkg_try jre17-openjdk-headless;;
  esac
  pkg_try unzip >/dev/null
  if [ ! -d /opt/languagetool ]; then
    T=$(mktemp -d)
    curl -fsSL https://languagetool.org/download/LanguageTool-stable.zip -o $T/lt.zip \
      && unzip -q $T/lt.zip -d $T && mv $T/LanguageTool-* /opt/languagetool && touch $CONF/.lt_installed \
      || warn "LanguageTool не скачан"
    rm -rf $T
  fi
  if [ -d /opt/languagetool ]; then
    cat >/etc/systemd/system/languagetool.service <<EOF
[Unit]
Description=LanguageTool server for Korrektor
After=network.target
[Service]
User=$APP
ExecStart=/usr/bin/java -Xmx1g -cp /opt/languagetool/languagetool-server.jar org.languagetool.server.HTTPServer --port 8010 --allow-origin '*'
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
    LT_ON=1
  fi
fi

# ---------------------------------------------------------------- 7. служба systemd
UNIT=/etc/systemd/system/$APP.service
cat >$UNIT <<EOF
[Unit]
Description=Korrektor — сервис корректорской вычитки PDF
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP
Group=$APP
EnvironmentFile=$ENVF
WorkingDirectory=$PREFIX
ExecStart=/bin/sh -c 'exec $PREFIX/venv/bin/uvicorn app.main:app --host \${KORREKTOR_HOST} --port \${KORREKTOR_PORT} --proxy-headers'
Restart=on-failure
RestartSec=3
# задание обрабатывается отдельным процессом: если ядро завершит его из-за нехватки памяти, служба продолжает
# работу (по умолчанию systemd останавливал бы всю службу); MALLOC_ARENA_MAX — память не «оседает» в потоках
OOMPolicy=continue
Environment=MALLOC_ARENA_MAX=2 PYTHONFAULTHANDLER=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=$DATA $PREFIX/app/prompts

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 8. команда korrektor
cat >/usr/local/bin/korrektor <<'EOF'
#!/usr/bin/env bash
# Управление сервисом «Корректор». Справка: korrektor help
APP=korrektor; ENVF=/etc/korrektor/korrektor.env; DATA=/var/lib/korrektor; PREFIX=/opt/korrektor
need_root() { [ "$(id -u)" -eq 0 ] || { echo "Нужны права администратора: sudo korrektor $*"; exit 1; }; }
port() { grep '^KORREKTOR_PORT=' $ENVF 2>/dev/null | cut -d= -f2; }
# адрес для проверки ответа: адрес прослушивания, а для «все адреса» — 127.0.0.1
hchost() { H=$(grep '^KORREKTOR_HOST=' $ENVF 2>/dev/null | cut -d= -f2); case "$H" in ""|0.0.0.0|::) echo 127.0.0.1;; *:*) echo "[$H]";; *) echo "$H";; esac; }
# python программы от имени службы; переменные — из файла (не в командной строке: их не видно в ps)
py() {
  if command -v runuser >/dev/null; then
    runuser -u $APP -- sh -c 'set -a; . "$0"; set +a; cd /opt/korrektor && exec /opt/korrektor/venv/bin/python "$@"' $ENVF "$@"
  else
    sudo -u $APP sh -c 'set -a; . "$0"; set +a; cd /opt/korrektor && exec /opt/korrektor/venv/bin/python "$@"' $ENVF "$@"
  fi
}
case "${1:-help}" in
  status)  systemctl status $APP --no-pager; echo; curl -s "http://$(hchost):$(port)/api/health"; echo;;
  start|stop|restart) need_root "$@"; systemctl "$1" $APP && echo "Готово: $1";;
  logs)    journalctl -u $APP -n "${2:-200}" --no-pager;;
  follow)  journalctl -u $APP -f;;
  passwd)  need_root "$@"; P="${2:-$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-14)}"
           case "$P" in *[[:space:]]*|*\"*|*\'*) echo "Пароль не должен содержать пробелы и кавычки"; exit 1;; esac
           PE=$(printf '%s' "$P" | sed 's/[\\&|]/\\&/g')
           # новый ключ подписи — все открытые сеансы завершаются
           sed -i "s|^KORREKTOR_ADMIN_PASSWORD=.*|KORREKTOR_ADMIN_PASSWORD=$PE|; s|^KORREKTOR_SECRET=.*|KORREKTOR_SECRET=$(openssl rand -hex 32)|" $ENVF \
             && systemctl restart $APP
           echo "Новый пароль администратора: $P";;
  password) need_root "$@"; grep '^KORREKTOR_ADMIN_PASSWORD=' $ENVF | cut -d= -f2-;;
  backup)  need_root "$@"; py -m app.backup backup;;
  verify)  need_root "$@"; [ -f "$2" ] || { echo "Укажите файл: sudo korrektor verify /var/backups/korrektor/....tgz"; exit 1; }
           chgrp $APP "$2" 2>/dev/null; chmod g+r "$2" 2>/dev/null; py -m app.backup verify "$(readlink -f "$2")";;
  restore) need_root "$@"; [ -f "$2" ] || { echo "Укажите файл: sudo korrektor restore /var/backups/korrektor/....tgz [--with-env]"; exit 1; }
           chgrp $APP "$2" 2>/dev/null; chmod g+r "$2" 2>/dev/null   # служба читает копию от своего имени
           py -m app.backup verify "$(readlink -f "$2")" >/dev/null \
             || { echo "Копия не прошла проверку: sudo korrektor verify $2"; exit 1; }
           systemctl stop $APP
           py -m app.backup restore "$(readlink -f "$2")"; RC=$?
           if [ "${3:-}" = "--with-env" ]; then
             T=$(mktemp -d); A="$2"
             if [ "$(head -c 14 "$2")" = "KORREKTOR-ENC1" ]; then      # зашифрованная копия — сначала расшифровать
               chown $APP "$T"; py -m app.backup decrypt "$(readlink -f "$2")" "$T/plain.tgz" && A="$T/plain.tgz"
             fi
             tar xzf "$A" -C "$T" etc/korrektor.env etc/korrektor/korrektor.env 2>/dev/null
             for E in "$T/etc/korrektor.env" "$T/etc/korrektor/korrektor.env"; do
               [ -f "$E" ] || continue
               for V in KORREKTOR_SECRET KORREKTOR_ADMIN_PASSWORD; do   # в незашифрованной копии их нет — берем текущие
                 grep -q "^$V=" "$E" || grep "^$V=" $ENVF >>"$E"
               done
               install -m 640 -o root -g $APP "$E" $ENVF && echo "Файл настроек службы восстановлен"
             done
             rm -rf "$T"
           fi
           chown -R $APP:$APP $DATA; systemctl start $APP; [ $RC -eq 0 ] && echo "Восстановлено из $2";;
  2fa-off) need_root "$@"; [ -n "${2:-}" ] || { echo "Укажите логин: sudo korrektor 2fa-off admin"; exit 1; }
           py -m app.totp off "$2";;
  update)  need_root "$@"; shift; bash $PREFIX/scripts/update.sh "$@";;
  uninstall) need_root "$@"; shift; bash $PREFIX/scripts/uninstall.sh "$@";;
  version) b=$(head -1 $PREFIX/app/BUILD 2>/dev/null || true); echo "$(cat $PREFIX/VERSION)${b:+ (сборка $b)}";;
  url)     echo "http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(port)/";;
  *) cat <<H
Команды:
  korrektor status            состояние службы и проверка ответа
  korrektor logs [N]          последние N строк журнала (по умолчанию 200)
  korrektor follow            журнал в реальном времени
  sudo korrektor restart      перезапуск (также start, stop)
  sudo korrektor passwd [П]   сменить пароль администратора (без П — сгенерировать)
  sudo korrektor password     показать текущий пароль
  sudo korrektor backup       резервная копия данных и настроек в /var/backups/korrektor
  sudo korrektor verify Ф     проверить целостность резервной копии
  sudo korrektor restore Ф    восстановить из копии (--with-env — и файл настроек службы);
                              для зашифрованной (.tgz.enc) пароль — из настроек или KORREKTOR_BACKUP_PASSWORD
  sudo korrektor 2fa-off Л     отключить двухфакторный вход пользователя Л (потерян телефон)
  sudo korrektor update [...] обновить (ключи как у update.sh)
  sudo korrektor uninstall    удалить (ключ --purge удалит и данные)
  korrektor version | url     версия и адрес сервиса
H
  ;;
esac
EOF
chmod 755 /usr/local/bin/korrektor
# короткая команда «korrektor-passwd» = «korrektor passwd» (раньше была ссылкой и только показывала справку)
printf '#!/usr/bin/env bash\nexec /usr/local/bin/korrektor passwd "$@"\n' >/usr/local/bin/korrektor-passwd && chmod 755 /usr/local/bin/korrektor-passwd
ok "Установлена команда управления: korrektor"

# начальные настройки (только при первой установке)
if [ ! -f $DATA/settings.json ]; then
  if [ "${LT_ON:-0}" -eq 1 ]; then
    echo '{"lt_enabled": true, "lt_url": "http://127.0.0.1:8010"}' >$DATA/settings.json
  fi
  [ $WITH_OCR -eq 1 ] && command -v tesseract >/dev/null && \
    $PY - "$DATA/settings.json" <<'EOF'
import json, os, sys
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {}
d["ocr_enabled"] = True
json.dump(d, open(p, "w"), ensure_ascii=False)
EOF
  [ -f $DATA/settings.json ] && chown $APP:$APP $DATA/settings.json && chmod 600 $DATA/settings.json
fi

# ---------------------------------------------------------------- 9. nginx
if [ $WITH_NGINX -eq 1 ]; then
  say "nginx как обратный прокси"
  pkg_install nginx || die "nginx не установлен"
  NG=/etc/nginx/conf.d/$APP.conf
  [ -d /etc/nginx/sites-available ] && NG=/etc/nginx/sites-available/$APP
  cat >$NG <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 512m;
    proxy_read_timeout 600s;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  if [ -d /etc/nginx/sites-enabled ]; then ln -sf $NG /etc/nginx/sites-enabled/$APP; rm -f /etc/nginx/sites-enabled/default; fi
  nginx -t >>"$LOG" 2>&1 || die "Ошибка в конфигурации nginx"
fi

# ---------------------------------------------------------------- 10. брандмауэр
FWPORT=$PORT; [ $WITH_NGINX -eq 1 ] && FWPORT=80
if [ $OPEN_FW -eq 1 ]; then
  if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=$FWPORT/tcp >>"$LOG" 2>&1 && firewall-cmd --reload >>"$LOG" 2>&1 && ok "firewalld: открыт порт $FWPORT"
  elif command -v ufw >/dev/null && ufw status | grep -q active; then
    ufw allow $FWPORT/tcp >>"$LOG" 2>&1 && ok "ufw: открыт порт $FWPORT"
  fi
fi
if command -v getenforce >/dev/null && [ "$(getenforce 2>/dev/null)" = "Enforcing" ] && [ $WITH_NGINX -eq 1 ]; then
  setsebool -P httpd_can_network_connect 1 >>"$LOG" 2>&1 || warn "SELinux: не удалось разрешить nginx соединения с портом $PORT"
fi

# ---------------------------------------------------------------- 11. запуск
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  [ "${LT_ON:-0}" -eq 1 ] && systemctl enable --now languagetool >>"$LOG" 2>&1 && ok "LanguageTool запущен на порту 8010"
  systemctl enable $APP >>"$LOG" 2>&1
  systemctl restart $APP
  [ $WITH_NGINX -eq 1 ] && { systemctl enable nginx >>"$LOG" 2>&1; systemctl restart nginx; }
  say "Проверка запуска"
  case "$LISTEN" in ""|0.0.0.0|::) HC=127.0.0.1;; *:*) HC="[$LISTEN]";; *) HC="$LISTEN";; esac
  for i in $(seq 1 30); do
    curl -fs "http://$HC:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1
  done
  curl -fs "http://$HC:$PORT/api/health" >/dev/null 2>&1 || die "Служба не отвечает. Журнал: journalctl -u $APP -n 50"
  ok "Служба $APP запущена"
else
  warn "systemd не найден (контейнер?). Запуск вручную:"
  echo "   sudo -u $APP env \$(grep -v '^#' $ENVF | xargs) $PREFIX/venv/bin/uvicorn app.main:app --app-dir $PREFIX --host $LISTEN --port $PORT"
fi
[ -n "$cleanup_src" ] && rm -rf "$cleanup_src"

IP=$(hostname -I 2>/dev/null | awk '{print $1}'); IP=${IP:-127.0.0.1}
URL="http://$IP:$PORT/"; [ $WITH_NGINX -eq 1 ] && URL="http://${DOMAIN/_/$IP}/"
echo
echo -e "${c_ok}════════════════════════════════════════════════════════${c_0}"
echo -e "${c_ok}  Корректор $VERSION_FULL установлен${c_0}"
echo    "  Адрес:     $URL"
echo    "  Пароль:    $PASSWORD"
echo    "  Управление: korrektor help"
echo    "  Данные:    $DATA    Настройки службы: $ENVF"
echo -e "${c_ok}════════════════════════════════════════════════════════${c_0}"
echo "Сохраните пароль. Посмотреть его снова: sudo korrektor password"
trap - ERR
