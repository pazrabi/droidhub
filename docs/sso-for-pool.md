# Единый вход (SSO) для панели Gemini Pool

Цель владельца: один сайт `molokohub.site`, один логин и пароль, а внутри панели
Gemini Pool и DroidHub с вкладками. Вход живёт в приложении DroidHub
(`https://molokohub.site/sso/login`), панель пула сама проверяет подписанный cookie.

## Как это работает

1. Владелец входит на `https://molokohub.site/sso/login`. Ставится cookie `molokohub_sso`
   (домен `.molokohub.site`, Secure, HttpOnly, SameSite=Lax).
2. Заход на `https://molokohub.site/` Caddy пропускает только с валидным cookie
   (иначе показывает форму входа), затем редиректит на `https://pool.molokohub.site/`.
3. Панель пула проверяет cookie САМА по общему секрету (см. ниже) и отдаёт токен панели.
   Заголовок `X-Sso-User` пул НЕ использует: старый адрес `sslip.io` идёт на тот же пул без
   единого входа, и заголовок можно было бы подделать снаружи.
4. Caddy НЕ ставит `forward_auth` перед `pool.molokohub.site`: токены, MCP и API пула
   проверяются самим пулом, как раньше.

## Формат cookie (контракт, совпадает с hub/sso.py)

`molokohub_sso` = `<user>|<expires_epoch>|<hmac_sha256_hex>`, подпись считается по строке
`<user>|<expires_epoch>` ключом `HUB_SSO_SECRET` (общий секрет, лежит в
`/opt/droidhub/.env`; копировать только на сервере из файла в файл, не в чат).

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
«Выйти» в панели пула ведёт на `https://molokohub.site/sso/logout` (стирает cookie для
всех панелей).

## Пожелание
Если в панели пула нет токена и нет валидного cookie на хосте `*.molokohub.site`,
можно вместо формы токена редиректить на
`https://molokohub.site/sso/login?next=<текущий https-URL>`. На `sslip.io` поведение
оставить прежним.
