#!/usr/bin/env bash
# =============================================================================
#  Корректор — удаление
#    sudo bash scripts/uninstall.sh            удалить программу, данные сохранить
#    sudo bash scripts/uninstall.sh --purge    удалить все, включая задания и настройки
#  Ключи: --purge, --keep-backup (по умолчанию перед --purge делается копия), --yes
# =============================================================================
set -Euo pipefail
APP=korrektor; PREFIX=/opt/$APP; DATA=/var/lib/$APP; CONF=/etc/$APP
PURGE=0; YES=0; BACKUP=1
while [ $# -gt 0 ]; do case "$1" in
  --purge) PURGE=1; shift;; --yes|-y) YES=1; shift;; --no-backup) BACKUP=0; shift;; --keep-backup) BACKUP=1; shift;;
  -h|--help) sed -n '2,8p' "$0"; exit 0;; *) echo "Неизвестный ключ $1"; exit 1;; esac; done
[ "$(id -u)" -eq 0 ] || { echo "Запустите: sudo bash $0"; exit 1; }
if [ $YES -eq 0 ]; then
  if [ $PURGE -eq 1 ]; then echo "Будут удалены программа, ВСЕ задания, отчеты и настройки."
  else echo "Будет удалена программа. Задания и настройки останутся в $DATA и $CONF."; fi
  read -r -p "Продолжить? [y/N] " a; [[ "$a" =~ ^[YyДд] ]] || { echo "Отменено"; exit 0; }
fi
if [ $PURGE -eq 1 ] && [ $BACKUP -eq 1 ] && [ -d $DATA ]; then
  mkdir -p /var/backups/$APP; B=/var/backups/$APP/before-uninstall-$(date +%Y%m%d-%H%M%S).tgz
  ( umask 077; tar czf "$B" -C / var/lib/$APP etc/$APP 2>/dev/null ) && echo "Резервная копия: $B"
fi
systemctl disable --now $APP 2>/dev/null
LT_OURS=0; [ -f $CONF/.lt_installed ] && LT_OURS=1      # LanguageTool ставил установщик Корректора
if [ $LT_OURS -eq 1 ]; then
  systemctl disable --now languagetool 2>/dev/null
  rm -f /etc/systemd/system/languagetool.service
fi
rm -f /etc/systemd/system/$APP.service
systemctl daemon-reload 2>/dev/null
rm -f /etc/nginx/conf.d/$APP.conf /etc/nginx/sites-available/$APP /etc/nginx/sites-enabled/$APP
command -v nginx >/dev/null && nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null
rm -f /usr/local/bin/korrektor /usr/local/bin/korrektor-passwd
rm -rf $PREFIX
[ $LT_OURS -eq 1 ] && rm -rf /opt/languagetool
if [ $PURGE -eq 1 ]; then
  rm -rf $DATA $CONF
  userdel $APP 2>/dev/null
  echo "Корректор удален полностью."
else
  echo "Корректор удален. Данные сохранены: $DATA, $CONF (удалить: sudo rm -rf $DATA $CONF)."
fi
echo "Системные пакеты (python3, hunspell, nginx) не удалялись — они могут использоваться другими программами."
