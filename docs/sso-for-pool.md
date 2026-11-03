# Единый вход (SSO) для панели Gemini Pool

Цель владельца: один сайт `molokohub.site`, один логин и пароль, а внутри панели
Gemini Pool и DroidHub с вкладками. Вход живёт в приложении DroidHub
(`https://molokohub.site/sso/login`), панель пула просто доверяет ему.

## Как это работает

1. Владелец входит на `https://molokohub.site/sso/login`. Ставится cookie `molokohub_sso`
   (домен `.molokohub.site`, Secure, HttpOnly, SameSite=Lax).
2. Caddy перед `pool.molokohub.site` вызывает `forward_auth` на DroidHub (`/sso/verify`).
   Без валидного cookie запрос на пул не доходит: владельца отправляют на форму входа.
   С валидным cookie Caddy добавляет к запросу заголовок `X-Sso-User: <имя>`
   (клиентский `X-Sso-User` Caddy затирает, подделать его снаружи нельзя).
3. Пул может (по желанию разработчика пула) считать запрос с `X-Sso-User` уже
   аутентифицированным и не показывать свою форму входа.

## Что нужно сделать в панели пула (по желанию)

Пока этого нет, всё работает, но после единого входа панель пула может ещё раз
показать свою собственную форму входа. Чтобы её убрать:

- Доверять `X-Sso-User` **только если запрос пришёл с 127.0.0.1** (от Caddy). Приложение
  пула слушает 127.0.0.1:8080, значит снаружи прямого доступа к нему нет, но проверку
  источника лучше оставить.
- Если заголовок есть и непустой, создать обычную сессию пула для этого пользователя
  (или пропустить проверку пароля).
- Адрес `193-41-86-92.sslip.io` продолжает работать со старым входом пула. Не ломать.

## Вариант без заголовка: проверка cookie самим пулом

Cookie `molokohub_sso` = `<user>|<expires_epoch>|<hmac_sha256_hex>`, подпись считается
по строке `<user>|<expires_epoch>` ключом `HUB_SSO_SECRET` (общий секрет, лежит в
`/opt/droidhub/.env`; передавать только через файл на сервере, не в чат).

```python
import hmac, hashlib, time

def verify(value: str, secret: str) -> str | None:
    try:
        user, exp, sig = value.rsplit("|", 2)
        good = hmac.new(secret.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and float(exp) > time.time():
            return user
    except Exception:
        pass
    return None
```

## Выход
Ссылка «Выйти» в панели пула может вести на `https://molokohub.site/sso/logout`
(стирает cookie для всех панелей).
