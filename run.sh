#!/bin/bash
# Обёртка для launchd/cron. Держит секреты вне репозитория и пишет лог.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# Секреты: пароль SMTP и (при наличии) прокси с российским IP.
# Файл ~/.regwatch.env не попадает в папку проекта и не индексируется.
if [ -f "$HOME/.regwatch.env" ]; then
  set -a; . "$HOME/.regwatch.env"; set +a
fi

MODE="${1:-run}"
mkdir -p logs
exec >> "logs/cron.log" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') :: regwatch $MODE ==="

case "$MODE" in
  daily) /usr/bin/python3 -m regwatch run ;;
  alert) /usr/bin/python3 -m regwatch run --alert ;;
  *)     /usr/bin/python3 -m regwatch "$@" ;;
esac
status=$?
echo "--- завершено с кодом $status"
exit $status
