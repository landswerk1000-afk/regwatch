#!/bin/bash
set -uo pipefail
LA="$HOME/Library/LaunchAgents"
for label in com.regwatch.daily com.regwatch.alert com.regwatch.tunnel; do
  if [ -f "$LA/$label.plist" ]; then
    launchctl unload "$LA/$label.plist" 2>/dev/null || true
    rm -f "$LA/$label.plist"
    echo "снято: $label"
  fi
done
echo "Агент снят с расписания. Данные и отчёты сохранены."
