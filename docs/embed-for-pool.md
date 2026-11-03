# Встраивание панели Gemini Pool в molokohub.site

Единый сайт `https://molokohub.site/` показывает панели во вкладках через `<iframe>`:
`https://pool.molokohub.site/?embed=1` и `https://droidhub.molokohub.site/?embed=1`.
Общая шапка «Gemini Pool | DroidHub» живёт в оболочке (репозиторий `pazrabi/molokohub`), поэтому в режиме встраивания панель не должна рисовать свою.

## Что нужно сделать в панели пула

1. **Разрешить встраивание только своему сайту.** Ко всем HTML-ответам добавить заголовок
   `Content-Security-Policy: frame-ancestors 'self' https://molokohub.site`
   и убрать `X-Frame-Options: DENY/SAMEORIGIN`, если он есть. Другим сайтам встраивание не разрешать.
2. **Режим `?embed=1`.** Если страница открыта внутри iframe (`window.self !== window.top`) и в адресе есть `embed=1`
   (запомнить в `sessionStorage`, чтобы не терялось после переходов), добавить `class="embed"` на `<html>` и скрыть:
   полоску вкладок «Gemini Pool | DroidHub», кнопку «Выйти» и всё, что дублирует общую шапку.
   Образец у DroidHub: `hub/web/templates/base.html` (скрипт в `<head>`) и конец `hub/web/static/style.css`.
3. **Вход.** Cookie `molokohub_sso` (домен `.molokohub.site`) уходит и в iframe, так как сайты одного домена.
   Ссылку «Выйти» в режиме встраивания не показывать: выход делает оболочка (`/sso/logout`).
4. **Не использовать** `window.top.location` и `target="_top"`.

## Проверка
Открыть `https://pool.molokohub.site/?embed=1` внутри любого сайта, кроме molokohub.site, должно не получиться (блок по CSP).
Внутри оболочки панель открывается без своей шапки, вкладки переключаются без перезагрузки.
