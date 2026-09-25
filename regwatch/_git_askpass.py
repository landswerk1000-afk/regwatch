#!/usr/bin/env python3
"""Выдаёт git пароль из переменной окружения.

Нужен, чтобы токен не попадал в аргументы процесса. Запись вида
`git push https://user:TOKEN@github.com/...` работает, но такой адрес виден
в `ps` любому, кто смотрит список процессов, и оседает в .git/config.
GIT_ASKPASS решает это: git спрашивает пароль, мы отвечаем из окружения.
"""
import os
import sys

if __name__ == "__main__":
    # git зовёт этот скрипт дважды: за именем пользователя и за паролем.
    # Различаем по тексту приглашения.
    prompt = " ".join(sys.argv[1:]).lower()
    if "username" in prompt or "имя" in prompt:
        print("x-access-token")
    else:
        print(os.environ.get("REGWATCH_GIT_TOKEN", ""))
