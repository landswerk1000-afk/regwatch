#!/bin/bash
# SOCKS5-туннель до вашей машины в России — открывает СОЗД и regulation.gov.ru.
# Под капотом обычный `ssh -D`: отдельный сервер не нужен.
#
# Настройки берутся из ~/.regwatch.env:
#   RU_SSH_HOST=...        адрес машины в России (IP, домен или имя в Tailscale)
#   RU_SSH_USER=...        пользователь на ней
#   RU_SSH_PORT=22         порт SSH (по умолчанию 22)
#   TUNNEL_PORT=1080       локальный порт SOCKS (по умолчанию 1080)
set -uo pipefail
cd "$(dirname "$0")" || exit 1
[ -f "$HOME/.regwatch.env" ] && { set -a; . "$HOME/.regwatch.env"; set +a; }

PORT="${TUNNEL_PORT:-1080}"
SSH_PORT="${RU_SSH_PORT:-22}"
CMD="${1:-run}"

is_up () { nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; }

case "$CMD" in
  status)
    if is_up; then
      echo "Туннель работает: SOCKS5 на 127.0.0.1:$PORT"
      echo "В ~/.regwatch.env должно быть: REGWATCH_PROXY=socks5h://127.0.0.1:$PORT"
      exit 0
    fi
    echo "Туннель не поднят (порт $PORT закрыт)."
    exit 1
    ;;
  check)
    is_up || { echo "Туннель не поднят."; exit 1; }
    echo "Проверяю СОЗД через туннель…"
    curl -s -o /dev/null -w "СОЗД: HTTP %{http_code} за %{time_total}s\n" \
         --max-time 45 --socks5-hostname "127.0.0.1:$PORT" \
         https://sozd.duma.gov.ru/oz
    ;;
  stop)
    pkill -f "ssh -N -D 127.0.0.1:$PORT" && echo "Туннель остановлен." || echo "Туннель не был запущен."
    ;;
  run|*)
    if [ -z "${RU_SSH_HOST:-}" ] || [ -z "${RU_SSH_USER:-}" ]; then
      echo "Не заданы RU_SSH_HOST и RU_SSH_USER в ~/.regwatch.env" >&2
      exit 2
    fi
    echo "Туннель до ${RU_SSH_USER}@${RU_SSH_HOST}:${SSH_PORT} → SOCKS5 на 127.0.0.1:$PORT"
    # BatchMode: без пароля, только по ключу — иначе в фоне зависнет на запросе пароля.
    exec ssh -N -D "127.0.0.1:$PORT" \
      -o BatchMode=yes \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=15 \
      -p "$SSH_PORT" "${RU_SSH_USER}@${RU_SSH_HOST}"
    ;;
esac
