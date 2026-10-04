"""Будильник для агента: по таймеру просит GitHub запустить ежедневный отчёт.

Зачем. Отчёт нужен ровно в 9:30, и ради этого сбор запускается в 8:40 —
с запасом в пятьдесят минут. Но расписание GitHub не обязано соблюдаться:
задачи по расписанию выполняются «когда дойдёт очередь», и это прямо сказано
в его документации. 4 октября 2026 года очередь дошла через пять с половиной
часов — отчёт ушёл в 14:10 вместо 9:30.

Запуск по команде (workflow_dispatch) в эту очередь не попадает: он стартует
за секунды. Таймеру Яндекс Облака, в отличие от планировщика GitHub, можно
верить — поэтому будильник живёт здесь, а не там.

Безопасность. У функции нет и не должно быть публичного адреса: её вызывает
только таймер. Поэтому здесь нет ни проверки токена, ни белого списка —
снаружи позвонить некому. Токен GitHub лежит в переменной окружения и должен
быть узким: один репозиторий, одно право «Actions: read and write». Ничего
другого этим токеном сделать нельзя — ни записать в код, ни прочитать секреты.

Расписание GitHub при этом остаётся как подстраховка: если таймер однажды
не сработает, отчёт всё равно выйдет, просто позже. Повторного отчёта
не будет — прогон по расписанию проверяет, не ушёл ли дневной отчёт сегодня.
"""
import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"
TIMEOUT = 20


def _need(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if not v:
        raise RuntimeError(f"не задана переменная окружения {name}")
    return v


def handler(event, context):
    """Вызывается таймером. Просит GitHub запустить рабочий процесс."""
    repo = _need("GH_REPO")            # вида «логин/regwatch»
    workflow = _need("GH_WORKFLOW")    # имя файла или числовой идентификатор
    token = _need("GH_TOKEN")
    mode = (os.environ.get("GH_MODE") or "daily").strip()

    url = f"{API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = json.dumps({"ref": os.environ.get("GH_REF", "main").strip(),
                       "inputs": {"mode": mode}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "regwatch-timer",
    })

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        # Текст ошибки GitHub полезен: он прямо называет недостающее право.
        detail = (e.read() or b"")[:400].decode("utf-8", "replace")
        return {"statusCode": 500,
                "body": f"GitHub отказал: HTTP {e.code} {detail}"}
    except Exception as e:                                   # noqa: BLE001
        return {"statusCode": 500,
                "body": f"не достучались до GitHub: {type(e).__name__}: {e}"}

    # 204 — принято к исполнению. Другого успешного ответа здесь не бывает.
    ok = code == 204
    return {"statusCode": 200 if ok else 500,
            "body": (f"запуск «{mode}» заказан" if ok
                     else f"неожиданный ответ GitHub: HTTP {code}")}
