"""Отправщик web push. Запускается ИНТЕРПРЕТАТОРОМ ИЗ .venv-push, не системным.

Шифрование web push (ECDH P-256, HKDF, AES-GCM) и подпись VAPID в стандартную
библиотеку не входят, а тащить зависимости в ядро агента не хочется: cron-задача
должна переживать любые обновления системы. Поэтому зависимость изолирована
здесь, а основной код общается с этим файлом через JSON на stdin/stdout.

Формат входа:  {"vapid": {...}, "payload": {...}, "subscriptions": [{...}]}
Формат выхода: {"results": [{"endpoint": ..., "ok": bool, "status": int, "error": str}]}
"""
import json
import sys


def main() -> int:
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print(json.dumps({"fatal": "pywebpush не установлен в .venv-push"}))
        return 2

    try:
        task = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"fatal": f"не разобрал вход: {e}"}))
        return 2

    vapid = task.get("vapid") or {}
    payload = json.dumps(task.get("payload") or {}, ensure_ascii=False)
    results = []

    for sub in task.get("subscriptions") or []:
        endpoint = sub.get("endpoint", "")
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid.get("private"),
                vapid_claims={"sub": vapid.get("subject", "mailto:admin@example.com")},
                ttl=vapid.get("ttl", 86400),
                timeout=25,
            )
            results.append({"endpoint": endpoint, "ok": True, "status": 201})
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", 0) or 0
            results.append({"endpoint": endpoint, "ok": False, "status": status,
                            "error": str(e)[:300]})
        except Exception as e:
            results.append({"endpoint": endpoint, "ok": False, "status": 0,
                            "error": f"{type(e).__name__}: {e}"[:300]})

    print(json.dumps({"results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
