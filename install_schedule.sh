#!/bin/bash
# Ставит агента в launchd: ежедневный отчёт и проверка срочных событий.
# launchd работает независимо от приложения Claude — агент автономен.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

DAILY_HOUR="${DAILY_HOUR:-9}"
DAILY_MIN="${DAILY_MIN:-30}"
ALERT_EVERY_HOURS="${ALERT_EVERY_HOURS:-3}"

make_plist () {
  local label="$1" mode="$2" schedule="$3"
  cat > "$LA/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HERE/run.sh</string>
    <string>$mode</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
$schedule
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$HERE/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/launchd.err.log</string>
</dict></plist>
PLIST
  launchctl unload "$LA/$label.plist" 2>/dev/null || true
  launchctl load  "$LA/$label.plist"
  echo "  установлено: $label"
}

make_plist "com.regwatch.daily" "daily" "  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$DAILY_HOUR</integer><key>Minute</key><integer>$DAILY_MIN</integer></dict>"

make_plist "com.regwatch.alert" "alert" "  <key>StartInterval</key><integer>$(( ALERT_EVERY_HOURS * 3600 ))</integer>"

# SSH-туннель до машины в России — ставится только если он настроен.
# KeepAlive поднимает его заново при обрыве связи или перезагрузке.
[ -f "$HOME/.regwatch.env" ] && . "$HOME/.regwatch.env" 2>/dev/null || true
if [ -n "${RU_SSH_HOST:-}" ] && [ -n "${RU_SSH_USER:-}" ]; then
  LABEL="com.regwatch.tunnel"
  cat > "$LA/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HERE/tunnel.sh</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>20</integer>
  <key>StandardOutPath</key><string>$HERE/logs/tunnel.out.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/tunnel.err.log</string>
</dict></plist>
PLIST
  launchctl unload "$LA/$LABEL.plist" 2>/dev/null || true
  launchctl load "$LA/$LABEL.plist"
  echo "  установлено: $LABEL (SSH-туннель, поднимается сам при обрыве)"
elif [ -n "${REGWATCH_PROXY:-}" ] || [ -f "$HERE/data/proxy_cache.json" ]; then
  echo "  туннель не настроен — СОЗД идёт через прокси (python3 -m regwatch proxies обновляет список)"
else
  echo "  ни туннеля, ни прокси — СОЗД и regulation.gov.ru будут пропускаться"
fi

echo
echo "Готово."
echo "  Ежедневный отчёт: $DAILY_HOUR:$(printf '%02d' "$DAILY_MIN")"
echo "  Проверка срочного: каждые $ALERT_EVERY_HOURS ч"
echo
echo "Снять с расписания:  ./uninstall_schedule.sh"
echo "Посмотреть очередь:  launchctl list | grep regwatch"
