# Полоска вкладок для панели Gemini Pool

Задача: показать сверху панели пула переключатель инструментов владельца
**Gemini Pool | DroidHub**, как в панели DroidHub. Активная вкладка подсвечена,
вторая ведёт на другую панель. Панели остаются на своих адресах.

- Gemini Pool: https://pool.molokohub.site/
- DroidHub: https://droidhub.molokohub.site/

## Разметка (вставить в самый верх `<body>`, до остального содержимого)

```html
<nav class="tool-tabs" aria-label="Инструменты">
  <a class="tool-tab is-active" aria-current="page">Gemini Pool</a>
  <a class="tool-tab" href="https://droidhub.molokohub.site/" rel="noopener">DroidHub</a>
</nav>
```

Новый инструмент добавляется ещё одной ссылкой `<a class="tool-tab" href="...">Название</a>`.
Если в пуле вкладки лучше собирать из настройки (список «Название|URL»), формат такой же, как у DroidHub:
`HUB_NAV_LINKS="Gemini Pool|https://pool.molokohub.site/,DroidHub|https://droidhub.molokohub.site/"`.

## Стили (подогнать цвета под фактические переменные панели пула)

```css
.tool-tabs {
  display: flex; align-items: center; gap: 10px;
  height: 56px; padding: 0 20px;
  background: #0b1220;               /* фон шапки пула */
  border-bottom: 1px solid #1c2740;
  overflow-x: auto; white-space: nowrap;
}
.tool-tab {
  display: inline-flex; align-items: center;
  padding: 8px 16px; border-radius: 999px;
  color: #aab4c8; text-decoration: none; font-weight: 600;
  border: 1px solid #243150; background: #121a2c;
}
.tool-tab:hover { color: #fff; border-color: #3a4a73; }
.tool-tab.is-active {
  color: #fff; background: transparent; border-color: transparent;
  font-size: 1.35rem; font-weight: 800; pointer-events: none;
}
@media (max-width: 600px) { .tool-tabs { padding: 0 12px; } }
```

## Что важно
- Ссылки открываются в той же вкладке браузера (без `target="_blank"`).
- У активной вкладки нет `href`, она не кликабельна.
- Никаких внешних скриптов и CDN.
- Вход в панели пока раздельный (у каждой свой пароль). Общий вход обсуждается отдельно.
