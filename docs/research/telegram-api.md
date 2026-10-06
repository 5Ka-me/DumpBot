# Telegram Bot API research for the personal dump bot (checked 2026-10-06)

## Versions
- **Bot API**: the latest is **10.3 (2026-08-24)**. Before it: 10.2 (2026-07-14), 10.1 (2026-06-11), 10.0 (2026-05-08), 9.6 (2026-04-03), 9.5 (2026-03-01), 9.4 (2026-02-09) and 9.3 (2025-12-31).
- **aiogram**: the latest is **3.31.0 (2026-08-26)**. Its changelog says "Updated to Bot API 10.3". It needs Python `>=3.10,<3.15`, `pydantic>=2.4.1,<2.14` and `aiohttp>=3.9,<3.15`. **3.29.0 and 3.29.1 are yanked on PyPI.** 3.29.1 fixed an "exponential slowdown" when validating nested RichBlock and MessageOrigin unions. I could not find the stated yank reason.
- **Recommendation**: pin `aiogram==3.31.0`. That release also fixes `DefaultBotProperties(parse_mode=…, link_preview=…)` being silently ignored for newer methods such as sendMessageDraft and sendRichMessage.

Minimum aiogram version per feature:

| Feature | Bot API | Minimum aiogram |
|---|---|---|
| Reactions, `MessageOrigin`, `ReplyParameters`, `LinkPreviewOptions`, blockquote | 7.0 | 3.3.0 (2023-12-31) |
| `copy_text` inline button | 7.11 | 3.14.0 |
| Button `style` / `icon_custom_emoji_id`, topics in private chats | 9.4 | 3.25.0 (builder kwargs in 3.26.0) |
| `date_time` entity, `sendMessageDraft` for all bots | 9.5 | 3.26.0 |
| Rich messages | 10.1 / 10.2 | 3.30.0 (avoid the yanked 3.29.x) |

## Findings

### Files: voice, photos, getFile
- **getFile**: bots can download files up to **20 MB**. The URL is `https://api.telegram.org/file/bot<token>/<file_path>` and stays valid for at least 1 hour; call getFile again when it expires.
  - The download URL contains the bot token, so never log it.
  - getFile "may not preserve the original file name and MIME type". Save `mime_type` and `file_name` from the incoming message.
- **Upload limits**: 10 MB for photos and 50 MB for other files via multipart; 5 MB / 20 MB via URL. Resending by `file_id` has no limit.
- **Local Bot API server**: removes the download limit and allows uploads up to 2000 MB. You don't need it for 10-second voice notes.
- **Voice**: the `Voice` object has `file_id`, `file_unique_id`, `duration`, optional `mime_type` and optional `file_size`.
  - Telegram voice notes are normally OGG/Opus. The docs only state this for *sending* (sendVoice); I did not verify it for incoming files.
  - aiogram: `data = await bot.download(m.voice)` returns a `BytesIO` (getFile plus GET, 30 s timeout by default).
- **Photos**:
  - `message.photo` is a list of PhotoSize. Use `[-1]`, the largest, for OCR.
  - A screenshot sent "as file" arrives as `message.document` with an `image/*` MIME type. Handle both cases.
  - I did not verify the maximum resolution of compressed photos. It can hurt OCR, so the user can send screenshots as files.

### file_id persistence
- Per the docs, a `file_id` is valid for download and reuse, belongs to one bot ("can't be transferred from one bot to another"), and one file can have several valid file_ids.
- `file_unique_id` is "supposed to be the same over time and for different bots" but cannot be used to download. Use it for de-duplication.
- **No expiry or TTL is documented.** I could not verify what happens to a file_id if the user deletes the message or if the bot is recreated.
- Since the decision is "no copies", store per file: `file_id`, `file_unique_id`, `mime_type`, `file_name`, `file_size`, `width`/`height`/`duration`.
- Also store the OCR text or transcript, so the content survives if a file_id dies.

### Forwarded posts: `forward_origin`
Since 7.0 the separate `forward_from*` fields are replaced by `MessageOrigin`, which is one of four types:
- `user`: `sender_user`, `date`
- `hidden_user`: `sender_user_name`, `date` (the original sender's privacy settings hide their account)
- `chat`: `sender_chat`, `author_signature?` (anonymous group admin)
- `channel`: `chat`, `message_id`, `author_signature?`, `date`

For a channel post, build the link as `https://t.me/<username>/<message_id>` for public channels. For private channels use `https://t.me/c/<id without -100>/<message_id>`, which only works for members.

Related fields:
- `external_reply` (ExternalReplyInfo) is set when the user "replies in another chat" to a channel post. Treat it as a source too.
- `quote` (TextQuote) holds the quoted fragment.
- Extract links from `entities` / `caption_entities` (`url`, `text_link`), not by regex: a `text_link` hides its URL behind the visible text.

### Media groups (albums)
- Each item arrives as a separate `Message` with the same `media_group_id`. There is no "album finished" marker, and the caption is usually on only one item.
- aiogram has **no built-in receive-side aggregator**; its `MediaGroupBuilder` is for sending only. Use a debounce of about 1–1.5 s keyed by `media_group_id`. This works because `start_polling(handle_as_tasks=True)`, the default, processes updates concurrently.
- `setMessageReaction` on a message that belongs to a group "is set to the first non-deleted message in the group".

### edited_message and deletions
- `edited_message` is a new version of a message the bot already knows. Telegram says it "may at times be triggered by changes to message fields that are either unavailable or not actively used by your bot". So compare a hash of text/caption before re-running the LLM.
- Key items by `(chat_id, message_id)` so an edit updates existing items instead of duplicating them.
- **There is no deletion update in normal private chats.** Only `deleted_business_messages` exists, for business connections. Deleting items must therefore go through the bot's own UI.
- Bots can delete incoming and outgoing messages in a private chat only if the message is less than 48 hours old.
- Editing the bot's own messages has no time limit. The 48-hour edit limit applies only to business messages.

### Reactions
- `setMessageReaction`: a non-premium bot can set **at most one reaction per message**. Bots cannot use paid reactions. Custom emoji only work if already present on the message or allowed by admins.
- Setting a new reaction replaces the old one, so a status flow like 👀 → 👌 works.
- **The 73 allowed `ReactionTypeEmoji` values**, copied from the docs on 2026-10-06. Send them exactly as written; for example "❤" is U+2764 with no FE0F variation selector.
  `❤ 👍 👎 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡`
- Suggested status set:
  - 👀 received / processing
  - 👌 saved
  - ✍ saved raw, LLM queued for retry
  - 🤔 needs your input
  - 🫡 reminder set
- **Reactions from the user as input** (`message_reaction`), for example 👍 on a reminder to mark it done:
  - Not delivered by default. They must be listed explicitly in `allowed_updates`.
  - The docs say "the bot must be an administrator in the chat". One third-party source says private chats work without that; this is **unverified, so test it**.
  - Updates are never sent for reactions set by bots.
  - Keep buttons as the primary way to mark done.

### Limits
- **Text**: 1–4096 characters *after entities parsing*.
- **Captions**: 0–1024.
- **answerCallbackQuery text**: 0–200.
- **Reply quote**: 0–1024.
- **Entity offsets** are in UTF-16 code units.
- **No message splitter** exists in aiogram. Split long digests by section before formatting.
- **HTML parse mode**:
  - Supported tags: `b/strong`, `i/em`, `u/ins`, `s/strike/del`, `tg-spoiler`, `a`, `tg-emoji`, `tg-time`, `code`, `pre`, `blockquote [expandable]`.
  - Every `<`, `>` and `&` outside a tag must be escaped.
  - Only the named entities `&lt; &gt; &amp; &quot;` are allowed (all numeric entities work).
  - aiogram `html.quote()` is `html.escape(value, quote=False)`. For `href` values use `html.escape(url, quote=True)`.
  - Safer for LLM output: `aiogram.utils.formatting` (Text/Bold/as_list/DateTime) produces entities with no escaping at all.
- **callback_data**: 1–64 **bytes**. aiogram's `CallbackData.pack()` raises `ValueError` if the encoded data is over 64 bytes. Use short prefixes and integer IDs.
- **Commands**:
  - `setMyCommands` takes at most 100 commands.
  - A command is 1–32 characters of `[a-z0-9_]`; a description is 1–256 characters.
  - There are 7 scopes. Use `BotCommandScopeChat(chat_id=OWNER)` so only the owner sees the menu.
  - An optional `language_code` (e.g. "ru") is available.
- **Rate limits** (bots FAQ):
  - In a single chat, "avoid sending more than one message per second". Short bursts are tolerated, then you get 429.
  - Groups: 20 messages per minute. Broadcast: about 30 messages per second.
  - aiogram raises `TelegramRetryAfter(retry_after)` and **does not retry automatically**, so add a retry wrapper.
  - Batch reminders that fall in the same minute into one message.
- **disable_notification**: the message arrives "silently" — users still get a notification, just with no sound. For quiet hours, it is better to delay delivery than to send silently.
- **Scheduled messages**: **the Bot API has no scheduled-send method**; `sendMessage` has no schedule parameter. Reminders and digests must be scheduled by our own service (APScheduler or a DB poller).

### Long polling
- Telegram keeps pending updates **at most 24 hours**. If the bot is down longer, those messages are lost.
- `allowed_updates`: if not specified, the previous setting is reused. An empty list means everything except `chat_member`, `message_reaction` and `message_reaction_count`. Use `dp.resolve_used_update_types()`.
- An update counts as confirmed once the next getUpdates call has a higher offset. With aiogram's task mode, a crash mid-handler can lose an update. **Save the raw message to the DB as the first step**; this matches the retry-queue design.
- Never pass `drop_pending_updates=True`.
- Only one poller per token is possible. Telegram returns a 409 Conflict otherwise; this is well known but not quoted in the docs. Run a single replica and use a separate test bot for development.

### New features worth considering

| Feature | Use for us | Verdict |
|---|---|---|
| `date_time` entity / `<tg-time unix=… format="wDT">` (9.5). Format regex `r\|w?[dD]?[tT]?`; `r` = relative | Reminder and due times shown in the user's own locale and timezone ("через 5 минут", "Вт 19:00") | **Use in MVP** |
| `sendMessageDraft` (9.3; all bots since 9.5; empty text = "Thinking…" since 10.0; `can_stop`/`keep_on_stop` since 10.3) | Streaming answers to natural-language questions. The draft is a 30-second ephemeral preview, so always finish with `sendMessage` | Nice to have |
| Button `style` = `success`/`danger`/`primary` (9.4) | Green "Сделано", red "Удалить" | Use, it's cheap |
| `copy_text` button (7.11), 1–256 characters | "Скопировать" for OCR snippets and links | Use, within 256 characters |
| `reply_parameters` / `quote` (7.0) | Reply to the source message when confirming the parse | Use |
| `link_preview_options` (7.0) | Turn previews off in digests and lists; `DefaultBotProperties(link_preview=LinkPreviewOptions(is_disabled=True))` | Use |
| `pinChatMessage` (any non-service message in private chats) | Pin today's digest | Optional |
| Topics in private chats (9.3/9.4; `createForumTopic`, `has_topics_enabled` from getMe) | Separate streams such as Inbox, Reminders, Digest | Later. Needs forum-topic mode turned on in BotFather (exact steps unverified) |
| Rich messages (10.1–10.3: `sendRichMessage` with html, markdown or blocks; tables, details, lists, buttons) | Formatted digests | Later. Very new; client support and limits unverified; keep an HTML fallback |
| Checklists (9.1/9.2) | — | **Not usable**: `sendChecklist` only sends "on behalf of a connected business account". Only handle an incoming `message.checklist` gracefully |
| Ephemeral messages (10.2/10.3) | — | Meant for groups; no value in a one-to-one chat |
| Business connection | — | Not needed. Whether 10.0's "without a Telegram Premium subscription" change makes it free to use is unclear/unverified |
| Guest mode (10.0) | Possibly "save to dump" from other chats | Future idea; details not studied |
| `message_effect_id` | — | Skip: effect IDs aren't listed in the Bot API docs |

## Minimal aiogram 3.31 snippets
```python
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
          link_preview=LinkPreviewOptions(is_disabled=True)))
router.message.filter(F.chat.type == "private", F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)

await bot.set_my_commands([BotCommand(command="today", description="Сегодня"), ...],
                          scope=BotCommandScopeChat(chat_id=OWNER_ID))
await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

await message.react([ReactionTypeEmoji(emoji="👀")])   # later "👌" replaces it

class TaskCb(CallbackData, prefix="t"):
    a: str            # d=done u=undone s=snooze
    id: int           # "t:d:123" fits 64 bytes easily

content = as_list(Bold("Напоминание"), Text(title),
                  DateTime("19:00", unix_time=due_at, date_time_format="wDT"))
await bot.send_message(OWNER_ID, **content.as_kwargs(), disable_notification=quiet)

def channel_source(m: Message) -> dict | None:
    o = m.forward_origin
    if isinstance(o, MessageOriginChannel):
        ch = o.chat
        url = f"https://t.me/{ch.username}/{o.message_id}" if ch.username \
              else f"https://t.me/c/{str(ch.id)[4:]}/{o.message_id}"
        return {"kind": "channel", "chat_id": ch.id, "title": ch.title, "url": url,
                "date": o.date, "signature": o.author_signature}
    ...

_albums: dict[str, list[Message]] = {}
async def on_media(m: Message):
    if not m.media_group_id: return await process([m])
    buf = _albums.setdefault(m.media_group_id, []); buf.append(m)
    if len(buf) == 1:
        await asyncio.sleep(1.5); await process(_albums.pop(m.media_group_id))

async def tg_call(factory, tries=3):
    for _ in range(tries):
        try: return await factory()
        except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after + 1)
```

## Recommendation
- Pin aiogram 3.31.0 and use long polling with `resolve_used_update_types()`.
- Order of work for each incoming message:
  1. Save the raw message and file metadata to the DB.
  2. React 👀.
  3. Debounce albums.
  4. Download through getFile (local OCR for images, voice to chutes).
  5. Run the LLM.
  6. Reply with an inline keyboard, using `reply_parameters` pointing at the source message.
  7. React 👌, or ✍ if the LLM call is queued for retry.
- Handle `edited_message` by re-parsing and updating items keyed by `message_id`.
- Show times with the `date_time` entity.
- Do all scheduling ourselves: reminders, digests, the 19:00 fallback and quiet hours.
- Defer rich messages, private topics and reaction-as-input until after the MVP.

## Open risks / unverified
- Whether a file_id stays valid long-term, or after the user deletes the message, is not documented.
- Whether `message_reaction` updates arrive in private chats without admin rights (docs say admin; one third-party source says it works).
- Exact incoming voice MIME type and the resolution cap for compressed photos.
- Rich-message client support and size limits.
- How to enable private-chat topics in BotFather.
- Why aiogram 3.29.x was yanked.
- The 24-hour update retention means outages longer than a day lose messages.
- 409 Conflict if two pollers run with one token (well known, not quoted from the docs).

## Sources (all read 2026-10-06)
- https://core.telegram.org/bots/api (getFile, File, Sending files, setMessageReaction, ReactionTypeEmoji, sendMessage, Formatting options, date-time formatting, MessageOrigin, LinkPreviewOptions, ReplyParameters, InlineKeyboardButton, CopyTextButton, setMyCommands, BotCommand, BotCommandScope, getUpdates, Update, deleteMessage, pinChatMessage, sendChecklist, sendMessageDraft, sendRichMessage, InputRichMessage, createForumTopic, Local Bot API Server)
- https://core.telegram.org/bots/api-changelog
- https://core.telegram.org/bots/faq (section "My bot is hitting limits")
- https://pypi.org/project/aiogram/
- https://docs.aiogram.dev/en/latest/changelog.html
- https://github.com/aiogram/aiogram/archive/refs/tags/v3.31.0.tar.gz (source checked for: `filters/callback_data.py` MAX_CALLBACK_LENGTH=64, `utils/text_decorations.py`, `utils/formatting.py` DateTime, `client/bot.py` download, `utils/media_group.py`, `exceptions.py` TelegramRetryAfter, `pyproject.toml`)
- https://agpt.co/docs/integrations/block-integrations/triggers-4 (third-party claim about private-chat reactions; unverified)