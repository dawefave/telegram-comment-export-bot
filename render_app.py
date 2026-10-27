"""Webhook entry point for the free Render web-service deployment."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import shutil
import tempfile

from fastapi import FastAPI, Header, HTTPException, Request
from telethon import TelegramClient
from telethon.sessions import StringSession

import cloud_bot as core

WEBHOOK_SECRET = core.setting('WEBHOOK_SECRET')
PUBLIC_URL = core.setting('PUBLIC_URL').rstrip('/')
tasks = set()
handled = set()
export_lock = asyncio.Lock()
client = None
api = None


async def handle_update(update):
    update_id = update.get('update_id')
    if update_id in handled:
        return
    handled.add(update_id)
    if len(handled) > 2000:
        handled.clear()
        handled.add(update_id)
    message = update.get('message', {})
    allowed = (message.get('from', {}).get('id') == core.OWNER_ID and
               message.get('chat', {}).get('type') == 'private' and
               message.get('chat', {}).get('id') == core.OWNER_ID)
    if not allowed:
        return
    text = message.get('text', '')
    if text.startswith(('/start', '/help')):
        await api.say(core.HELP)
        return
    if text.strip() == '/status':
        await api.say('Працюю на безкоштовному сервері Render. Надішли посилання на допис.')
        return
    if export_lock.locked():
        await api.say('Попередній експорт ще триває. Надішли наступне посилання після отримання Excel.')
        return
    async with export_lock:
        folder = Path(tempfile.mkdtemp(prefix='telegram-export-', dir=core.DATA_DIR))
        try:
            core.parse_link(text)
            await api.say('Збираю коментарі та фото. Готовий Excel надішлю сюди.')
            payload = await core.collect(client, text, api.say)
            if not payload['rows']:
                await api.say('У гілці немає доступних коментарів.')
                return
            target = folder / f"comments_{payload['channel']}_{payload['post']}.xlsx"
            await asyncio.to_thread(core.make_workbook, payload, target)
            nicknames = sum(row['username'].startswith('@') for row in payload['rows'])
            caption = (f"Готово: {len(payload['rows'])} повідомлень, @нікнейми у {nicknames} рядках, "
                       f"фото у {payload['photos']} рядках.\n{payload['source']}")
            await api.request('sendDocument', {'chat_id': core.OWNER_ID, 'caption': caption}, target)
        except Exception as exc:
            await api.say(core.friendly_error(exc))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


@asynccontextmanager
async def lifespan(app):
    global client, api
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    api = core.BotAPI(core.BOT_TOKEN)
    bot = await api.request('getMe')
    client = TelegramClient(StringSession(core.TELEGRAM_SESSION), core.API_ID, core.API_HASH,
                            device_model='Comments Export Bot Render', receive_updates=False,
                            flood_sleep_threshold=30)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError('Telegram user session is no longer authorized.')
    me = await client.get_me()
    if me.id != core.OWNER_ID:
        raise RuntimeError('OWNER_ID does not match the Telegram user session.')
    await api.request('setWebhook', {
        'url': PUBLIC_URL + '/telegram',
        'secret_token': WEBHOOK_SECRET,
        'allowed_updates': ['message'],
        'drop_pending_updates': False,
    })
    print(f'@{bot["username"]} webhook ready at {PUBLIC_URL}', flush=True)
    try:
        yield
    finally:
        for task in list(tasks):
            task.cancel()
        await client.disconnect()


app = FastAPI(lifespan=lifespan)


@app.get('/')
async def health():
    return {'ok': True, 'service': 'telegram-comment-export'}


@app.post('/telegram')
async def telegram_update(request: Request, x_telegram_bot_api_secret_token: str = Header(default='')):
    if not secrets.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)
    update = await request.json()
    task = asyncio.create_task(handle_update(update))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {'ok': True}
