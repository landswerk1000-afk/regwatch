"""Функция-ретранслятор в Yandex Cloud: скачивает госсайт с российского IP.

Зачем. Портал СОЗД, regulation.gov.ru и council.gov.ru обрывают TLS-соединение
с зарубежных адресов. Функция живёт на серверах Яндекса в России, поэтому для
госсайта запрос выглядит местным. Агент на компьютере остаётся где угодно.

Безопасность. Функция публична — её адрес знает любой, кто его увидит.
От превращения в открытый прокси защищают две вещи сразу:
  * токен в заголовке X-Relay-Token (в адресе он утёк бы в журналы);
  * белый список доменов: ничего, кроме перечисленных госсайтов, не скачается.

Ответ отдаём в base64. Госсайты часто используют windows-1251, а если отдавать
текстом, кодировка угадывается по дороге и кириллица превращается в мусор.
Байты доезжают как есть, и разбирает их уже агент.
"""
import base64
import os

import requests

ALLOWED = (
    "duma.gov.ru",
    "sozd.duma.gov.ru",
    "regulation.gov.ru",
    "council.gov.ru",
    "cbr.ru",
    "minfin.gov.ru",
)

TIMEOUT = 25
MAX_BYTES = 8 * 1024 * 1024

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def _host_allowed(url: str) -> bool:
    """Сравниваем именно имя узла, а не подстроку.

    Проверка `"duma.gov.ru" in url` пропустила бы duma.gov.ru.злодей.рф —
    домен злоумышленника, в котором наше имя всего лишь встречается.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in ALLOWED)


def _reply(code, body, ctype="text/plain; charset=utf-8", b64=False):
    return {"statusCode": code, "headers": {"Content-Type": ctype},
            "body": body, "isBase64Encoded": b64}


def handler(event, context):
    expected = os.environ.get("TOKEN", "")
    if not expected:
        return _reply(500, "на функции не задана переменная TOKEN")

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    params = event.get("queryStringParameters") or {}
    token = headers.get("x-relay-token") or params.get("token", "")

    # Сравнение постоянного времени: обычное == выдаёт длину общего префикса
    # тому, кто измеряет время ответа, и токен подбирается посимвольно.
    # Сравниваем байты, а не строки: на строке с не-ASCII compare_digest
    # бросает TypeError, и посторонний с кириллицей в токене получил бы
    # не отказ, а падение функции.
    import hmac
    if not hmac.compare_digest(str(token).encode("utf-8", "replace"),
                               expected.encode("utf-8")):
        return _reply(403, "неверный токен")

    url = params.get("url", "")
    if not url.startswith(("http://", "https://")):
        return _reply(400, "нужен абсолютный адрес")
    if not _host_allowed(url):
        return _reply(400, "домен не в списке разрешённых")

    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HEADERS,
                         allow_redirects=True, stream=True)
        raw = r.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as e:
        return _reply(502, f"источник недоступен: {type(e).__name__}: {e}")

    if len(raw) > MAX_BYTES:
        return _reply(502, "ответ больше допустимого размера")

    return {
        "statusCode": r.status_code,
        "headers": {
            "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
            # Агент узнаёт, куда его в итоге привели редиректы.
            "X-Relay-Final-Url": r.url,
        },
        "body": base64.b64encode(raw).decode("ascii"),
        "isBase64Encoded": True,
    }
