"""Конфигурация. Секреты берутся только из переменных окружения."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "db_path": "data/regwatch.db",
    "reports_dir": "reports",
    "topics_path": "topics.json",
    "lookback_days": 21,
    "thresholds": {
        "report_min_relevance": 0.45,
        "alert_min_relevance": 0.70,
        "alert_urgencies": ["critical"],
        "max_items_per_report": 60,
    },
    "http": {"timeout": 35, "retries": 3, "min_interval": 0.6},
    "proxy": {
        "url": "",
        "hosts": ["duma.gov.ru", "sozd.duma.gov.ru", "regulation.gov.ru"],
        "_hint": "url вида http://user:pass@host:port или socks5h://127.0.0.1:1080; пустое значение = прокси выключен",
    },
    "sources": {},
    "email": {
        "enabled": False,
        "smtp_host": "",
        "smtp_port": 465,
        "use_ssl": True,
        "use_starttls": False,
        "username": "",
        "password_env": "REGWATCH_SMTP_PASSWORD",
        "from_addr": "",
        "to": [],
        "subject_prefix": "[Регмонитор]",
    },
}


def load_env_file(path: str | Path | None = None) -> int:
    """Подхватывает ~/.regwatch.env при любом запуске.

    Обёртки run.sh и regwatch-cli делают это сами, но `python3 -m regwatch`
    запускается напрямую — без этого секреты не видны. Уже заданные
    переменные окружения не перетираем: они старше файла.
    """
    p = Path(path) if path else Path.home() / ".regwatch.env"
    if not p.exists():
        return 0
    loaded = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


@dataclass
class Config:
    root: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "config.json") -> "Config":
        p = Path(path).resolve()
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        return cls(root=p.parent, data=_deep_merge(DEFAULTS, raw))

    def _path(self, key: str) -> Path:
        v = Path(self.data[key])
        return v if v.is_absolute() else self.root / v

    @property
    def db_path(self) -> Path:
        return self._path("db_path")

    @property
    def reports_dir(self) -> Path:
        return self._path("reports_dir")

    @property
    def topics_path(self) -> Path:
        return self._path("topics_path")

    @property
    def sources(self) -> dict:
        return self.data.get("sources", {})

    @property
    def thresholds(self) -> dict:
        return self.data["thresholds"]

    @property
    def email(self) -> dict:
        return self.data["email"]

    @property
    def proxy_url(self) -> str:
        """Приоритет: переменная окружения → config.json → кэш найденных прокси.

        Кэш заполняет команда `regwatch proxies`. Это запасной путь: если
        туннель до своей машины лёг, агент подхватит публичный прокси сам.
        """
        explicit = os.environ.get("REGWATCH_PROXY") or self.data["proxy"].get("url") or ""
        if explicit:
            return explicit
        try:
            from . import proxypool
            return proxypool.load(self.root) or ""
        except Exception:
            return ""

    @property
    def relay_url(self) -> str:
        """Адрес функции-ретранслятора в Yandex Cloud (если поднята)."""
        return (os.environ.get("REGWATCH_RELAY_URL")
                or self.data.get("relay", {}).get("url") or "").strip()

    @property
    def relay_token(self) -> str:
        """Токен функции. Только из окружения: в config.json секретам не место."""
        return os.environ.get("REGWATCH_RELAY_TOKEN", "").strip()

    @property
    def proxy_hosts(self) -> list:
        return self.data["proxy"].get("hosts", [])

    @property
    def smtp_password(self) -> str:
        return os.environ.get(self.email.get("password_env") or "REGWATCH_SMTP_PASSWORD", "")

    def make_http(self, logger=None):
        from .http import Http
        h = self.data["http"]
        return Http(proxy_url=self.proxy_url or None, proxy_hosts=self.proxy_hosts,
                    timeout=h["timeout"], retries=h["retries"],
                    min_interval=h["min_interval"], logger=logger,
                    relay_url=self.relay_url or None, relay_token=self.relay_token or None)
