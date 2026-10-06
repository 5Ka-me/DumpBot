"""The Telegram Mini App's backend (docs/features/iteration-2.md §D, contract: docs/features/miniapp-api.md).

An aiohttp application inside the bot process, started and stopped by ``bot.app.Lifecycle``: ``/api/*`` (JSON, every
request signed by Telegram's initData of the owner) as a thin layer over the services, and the built frontend
(``webapp_static_dir``) with a fallback to ``index.html``.

- ``server``: the application, its middlewares, static files and ``WebServer`` (start / stop);
- ``auth``: initData validation (HMAC-SHA256, ``auth_date`` not older than 24 h, owner only);
- ``http``: errors as JSON, request parsing helpers;
- ``serialize``: rows, items, occurrences… as JSON;
- ``api_lists`` (feed, today, tasks, …, ask, settings, usage, jobs), ``api_items`` (an item, its changes and steps),
  ``api_inbox`` ("закинуть в свалку"), ``files`` (picture previews from Telegram with an in-memory LRU).
"""
