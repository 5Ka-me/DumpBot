# Mini App «📱 Свалка» (фронтенд)

React + TypeScript + Vite, без UI-библиотек. Спецификация — [docs/features/iteration-2.md, D](../docs/features/iteration-2.md),
контракт API — [docs/features/miniapp-api.md](../docs/features/miniapp-api.md) и [src/api/types.ts](src/api/types.ts).
Бот отдаёт собранный `dist/` из `WEBAPP_STATIC_DIR` (`/app/webapp` в образе; собирается в Dockerfile, стадия `webapp`).

```bash
npm ci
npm run dev        # http://127.0.0.1:5173 с мок-API в памяти (mock/), бот не нужен
npm run check      # tsc --noEmit + eslint + vitest + vite build
npm run build      # dist/
```

## Устройство

| Папка | Что там |
|---|---|
| `src/api/` | `types.ts` (контракт), `client.ts` — по методу на эндпоинт, `Authorization: tma <initData>`, ошибки → `ApiError` (`code`, русский `message`, `field`), `pollJob` |
| `src/telegram.ts` | `telegram-web-app.js`: ready / expand, тема из `themeParams` (CSS берёт `--tg-theme-*`), haptics, диалоги, ссылки |
| `src/lib/` | кэш чтений (`useResource`, `markStale`), навигация (вкладки + стек экранов, BackButton), MainButton, «закинуть в свалку» с опросом, превью картинок (fetch → blob URL, кэш по числу и байтам), черновики (`drafts`: localStorage до сохранения), форматирование дат по-русски в часовом поясе владельца |
| `src/screens/` | Сегодня (и 7 дней), Лента, Задачи, Карточка (`card/`), Добавить, Спросить, Настройки, Расходы, Ещё |
| `mock/` | мок-API для `npm run dev` и тестов: те же пути, формы и коды ошибок, данные в памяти |

Вне Telegram (в обычном браузере) собранное приложение показывает только подсказку «открой кнопкой в чате»; в
`npm run dev` оно работает и там — кнопки «Назад» / «Сохранить» рисует само, а вместо initData шлёт `dev`.

## Мок-API

- Авторизация: любой `tma …`; `tma expired` → 401 `expired`, `tma stranger` → 403 (`VITE_DEV_INIT_DATA=expired npm run dev`).
- «Закинуть в свалку»: через ~2 с появляется запись. Слова в тексте: `?…` — ответ на вопрос, `уточни` — уточняющий
  вопрос, `перенеси` / `измени` — выбор записи, `бюджет` — ждёт бюджета, `ошибка` — сбой.
- «🕒 Время» словами понимает «сегодня / завтра / послезавтра в ЧЧ:ММ» и «через N ч»; «🪄 Разбить» даёт 4 шага.
- Вопрос со словом `бюджет` → 402, `MOCK_DELAY=0 npm run dev` убирает искусственную задержку ответов.

Против настоящего бота: `VITE_API_PROXY=http://127.0.0.1:8080 VITE_DEV_INIT_DATA='<initData владельца>' npm run dev`.

Тему и кнопки Telegram можно проверить в браузере: откройте `http://127.0.0.1:5173/#tgWebAppData=<любая строка>&tgWebAppVersion=8.0&tgWebAppPlatform=ios&tgWebAppThemeParams=<JSON темы>`
— `telegram-web-app.js` применит тему, а вызовы кнопок и haptics будут видны в консоли (`[Telegram.WebView] > postEvent …`).
