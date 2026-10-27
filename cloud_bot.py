"""Headless personal Telegram comment exporter for a Linux VM."""
import asyncio
import base64
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps
from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession
from telethon.tl.types import Channel, MessageService
import xlsxwriter

DATA_DIR = Path(os.environ.get('DATA_DIR', '/var/lib/telegram-comment-bot'))
OFFSET_FILE = DATA_DIR / 'offset.json'
KYIV = ZoneInfo('Europe/Kyiv')
MAX_EMBEDDED_PHOTO_BYTES = 24 * 1024 * 1024
MAX_MESSAGES = 100000

def setting(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'Missing required setting: {name}')
    return value

BOT_TOKEN = setting('BOT_TOKEN')
API_ID = int(setting('API_ID'))
API_HASH = setting('API_HASH')
TELEGRAM_SESSION = setting('TELEGRAM_SESSION')
OWNER_ID = int(setting('OWNER_ID'))

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)

def parse_link(text):
    links = re.findall(r'https?://(?:t\.me|telegram\.me)/[^\s<>]+', text or '', re.I)
    if len(links) != 1:
        raise ValueError('Надішли одне посилання на допис: https://t.me/назва_каналу/123')
    url = urllib.parse.urlsplit(links[0].rstrip('.,;)'))
    match = re.fullmatch(r'/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/([1-9][0-9]*)/?', url.path)
    if not match:
        raise ValueError('Потрібне посилання на допис публічного каналу: https://t.me/назва_каналу/123')
    channel, post = match.groups()
    return channel, int(post), f'https://t.me/{channel}/{post}'

class BotError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Bot API {code}')

class BotAPI:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, data=None, document=None):
        data = data or {}
        if document:
            boundary = 'Export' + uuid.uuid4().hex
            body = bytearray()
            for key, value in data.items():
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{document.name}"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'.encode())
            body.extend(document.read_bytes())
            body.extend(f'\r\n--{boundary}--\r\n'.encode())
            content_type = f'multipart/form-data; boundary={boundary}'
        else:
            body = json.dumps(data).encode()
            content_type = 'application/json'
        request = urllib.request.Request(self.base + method, data=bytes(body), headers={'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except Exception:
                raise BotError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BotError('network') from None
        if not result.get('ok'):
            raise BotError(result.get('error_code', 'unknown'), result.get('parameters', {}).get('retry_after', 0))
        return result['result']

    async def request(self, method, data=None, document=None):
        for attempt in range(4):
            try:
                return await asyncio.to_thread(self.call, method, data, document)
            except BotError as exc:
                if exc.code != 429 or attempt == 3:
                    raise
                await asyncio.sleep(max(1, exc.retry_after))

    async def say(self, text):
        return await self.request('sendMessage', {'chat_id': OWNER_ID, 'text': text})

def author_info(sender):
    if sender is None:
        return 'Невідомий автор', 'Не вдалося перевірити'
    name = utils.get_display_name(sender) or 'Без імені'
    if getattr(sender, 'deleted', False):
        return 'Deleted Account', 'Акаунт видалено'
    handles = []
    for value in [getattr(sender, 'username', None)] + [item.username for item in (getattr(sender, 'usernames', None) or []) if item.active]:
        if value and '@' + value not in handles:
            handles.append('@' + value)
    return name, ', '.join(handles) or 'Не вказано'

def media_kind(message):
    if message.photo:
        return 'Фото з підписом' if message.message else 'Фото без тексту'
    if message.sticker:
        return 'Стікер'
    if message.voice:
        return 'Голосове повідомлення'
    if message.video:
        return 'Відео'
    if message.document:
        return 'Файл'
    return 'Коментар'

async def photo_thumbnail(client, message):
    sizes = [size for size in (message.photo.sizes or []) if getattr(size, 'w', 0) and getattr(size, 'h', 0)]
    if not sizes:
        return '', 0
    thumb = min(sizes, key=lambda size: abs(max(size.w, size.h) - 480))
    blob = await client.download_media(message.photo, bytes, thumb=thumb)
    if not blob:
        return '', 0
    with Image.open(BytesIO(blob)) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((232, 172), Image.Resampling.LANCZOS)
        canvas = Image.new('RGB', (240, 180), '#FFFFFF')
        canvas.paste(image, ((240-image.width)//2, (180-image.height)//2))
        output = BytesIO()
        canvas.save(output, format='JPEG', quality=72, optimize=True)
    data = output.getvalue()
    return base64.b64encode(data).decode('ascii'), len(data)

async def collect(client, text, progress):
    channel_name, post_id, source = parse_link(text)
    channel = await client.get_entity(channel_name)
    if not isinstance(channel, Channel) or not channel.broadcast:
        raise ValueError('Це посилання не на допис каналу.')
    post = await client.get_messages(channel, ids=post_id)
    if not post:
        raise ValueError('Допис не знайдено або він видалений.')
    if not post.replies or not post.replies.comments:
        raise ValueError('У цього допису немає відкритої гілки коментарів.')
    rows, seen, authors = [], set(), {}
    embedded = failures = 0
    async for message in client.iter_messages(channel, reply_to=post_id):
        if isinstance(message, MessageService) or message.id in seen:
            continue
        seen.add(message.id)
        if len(seen) > MAX_MESSAGES:
            raise ValueError('У гілці понад 100 000 повідомлень. Потрібен експорт частинами.')
        if message.sender_id not in authors:
            authors[message.sender_id] = author_info(await message.get_sender())
        name, username = authors[message.sender_id]
        photo, photo_note = '', ''
        if message.photo:
            if embedded >= MAX_EMBEDDED_PHOTO_BYTES:
                photo_note, failures = 'Не додано: досягнуто ліміт розміру', failures + 1
            else:
                try:
                    photo, size = await photo_thumbnail(client, message)
                    if photo and embedded + size <= MAX_EMBEDDED_PHOTO_BYTES:
                        embedded += size
                    else:
                        photo, photo_note, failures = '', 'Не вдалося додати фото', failures + 1
                except Exception:
                    photo_note, failures = 'Не вдалося додати фото', failures + 1
        local = message.date.astimezone(KYIV)
        rows.append({'id':message.id,'author':name,'username':username,'date':local.strftime('%Y-%m-%d'),
                     'time':local.strftime('%H:%M'),'text':message.message or '','photo':photo,
                     'photo_note':photo_note,'kind':media_kind(message),'url':f'{source}?comment={message.id}',
                     'edited':bool(message.edit_date)})
        if len(rows) % 100 == 0:
            await progress(f'Зібрано {len(rows)} повідомлень…')
    rows.sort(key=lambda row: row['id'])
    return {'source':source,'channel':channel_name,'post':post_id,'title':channel.title,'rows':rows,
            'exported':datetime.now(KYIV).strftime('%d.%m.%Y %H:%M'),
            'photos':sum(bool(row['photo']) for row in rows),'photo_failures':failures}

def make_workbook(data, target):
    workbook = xlsxwriter.Workbook(target)
    sheet = workbook.add_worksheet('Коментарі')
    sheet.hide_gridlines(2)
    sheet.freeze_panes(6, 0)
    sheet.set_tab_color('#365F91')
    base = workbook.add_format({'font_name':'Arial','font_size':11,'font_color':'#172B4D','valign':'top'})
    center = workbook.add_format({'font_name':'Arial','font_size':11,'font_color':'#172B4D','align':'center','valign':'top'})
    wrap = workbook.add_format({'font_name':'Arial','font_size':11,'font_color':'#172B4D','text_wrap':True,'valign':'top'})
    link = workbook.add_format({'font_name':'Arial','font_size':11,'font_color':'#2563A6','underline':True,'valign':'top'})
    date_fmt = workbook.add_format({'font_name':'Arial','font_size':11,'font_color':'#172B4D','num_format':'dd/mm/yyyy','valign':'top'})
    time_fmt = workbook.add_format({'font_name':'Arial','font_size':11,'font_color':'#172B4D','num_format':'hh:mm','valign':'top'})
    title = workbook.add_format({'font_name':'Arial','font_size':16,'bold':True,'font_color':'#172B4D','bottom':1,'bottom_color':'#365F91'})
    note = workbook.add_format({'font_name':'Arial','font_size':11,'font_color':'#58677B'})
    header = workbook.add_format({'font_name':'Arial','font_size':11,'bold':True,'font_color':'#FFFFFF','bg_color':'#365F91','align':'center','valign':'vcenter'})
    sheet.set_column('A:A', 8)
    sheet.set_column('B:B', 26)
    sheet.set_column('C:C', 27)
    sheet.set_column('D:D', 14)
    sheet.set_column('E:E', 9)
    sheet.set_column('F:F', 120)
    sheet.set_column('G:G', 19)
    sheet.set_column('H:H', 24)
    sheet.set_column('I:I', 55)
    sheet.set_column('J:J', 15)
    sheet.set_row(1, 28)
    sheet.write(1, 0, f"{data['title']} — коментарі до допису №{data['post']}", title)
    sheet.write(2, 0, f"Джерело: {data['source']} • Зібрано {data['exported']} • {len(data['rows'])} повідомлень", note)
    sheet.write(3, 0, 'Час — Київ. Фото додані як мініатюри; інші вкладення доступні за посиланнями. «Не вказано» — немає публічного нікнейму.', note)
    headers = ['№','Автор','Нікнейм','Дата','Час','Текст коментаря','Фото','Тип повідомлення','Посилання','Редаговано']
    sheet.add_table(5, 0, 5 + len(data['rows']), 9, {'name':'TelegramComments','style':'Table Style Light 1','columns':[{'header':h,'header_format':header} for h in headers]})
    for index, row in enumerate(data['rows']):
        r = index + 6
        text_lines = sum(max(1, (len(part)+119)//120) for part in row['text'].split('\n'))
        author_lines = max((len(row['author'])+22)//23, (len(row['username'])+23)//24)
        sheet.set_row(r, min(409, max(30, 72 if row['photo'] else 0, max(text_lines,author_lines)*14+12)))
        sheet.write_number(r,0,index+1,center)
        sheet.write(r,1,row['author'],wrap)
        sheet.write(r,2,row['username'],wrap)
        sheet.write_datetime(r,3,datetime.strptime(row['date'],'%Y-%m-%d'),date_fmt)
        sheet.write_datetime(r,4,datetime.strptime(row['time'],'%H:%M'),time_fmt)
        sheet.write(r,5,row['text'],wrap)
        sheet.write(r,6,row['photo_note'],wrap)
        sheet.write(r,7,row['kind'],wrap)
        sheet.write_url(r,8,row['url'],link,row['url'])
        sheet.write(r,9,'Так' if row['edited'] else 'Ні',base)
        if row['photo']:
            photo = BytesIO(base64.b64decode(row['photo']))
            sheet.insert_image(r,6,'photo.jpg',{'image_data':photo,'x_offset':4,'y_offset':4,
                                               'x_scale':112/240,'y_scale':84/180,'object_position':1})
    workbook.close()

def friendly_error(exc):
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, errors.FloodWaitError):
        return f'Telegram просить зачекати {exc.seconds} с. Повтори пізніше.'
    if isinstance(exc, errors.ChannelPrivateError):
        return 'Немає доступу до каналу або його обговорення.'
    if isinstance(exc, errors.UsernameNotOccupiedError):
        return 'Канал із такою назвою не знайдено.'
    return f'Експорт не завершено ({type(exc).__name__}). Спробуй ще раз.'

HELP = 'Надішли посилання на допис публічного каналу — поверну Excel з коментарями, іменами, @нікнеймами та мініатюрами фото. /status — перевірка роботи.'

async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    api = BotAPI(BOT_TOKEN)
    bot = await api.request('getMe')
    webhook = await api.request('getWebhookInfo')
    if webhook.get('url'):
        raise RuntimeError('Bot has an active webhook; long polling cannot start.')
    client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH,
                            device_model='Comments Export Bot Cloud', receive_updates=True,
                            use_ipv6=True,
                            flood_sleep_threshold=30)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError('Telegram user session is no longer authorized.')
    me = await client.get_me()
    if me.id != OWNER_ID:
        raise RuntimeError('OWNER_ID does not match the Telegram user session.')
    offset = json.loads(OFFSET_FILE.read_text()).get('offset',0) if OFFSET_FILE.exists() else 0
    print(f'@{bot["username"]} ready for owner {OWNER_ID}', flush=True)
    try:
        while True:
            try:
                updates = await api.request('getUpdates',{'offset':offset,'timeout':20,'allowed_updates':['message']})
            except BotError as exc:
                if exc.code in (401,409):
                    raise
                await asyncio.sleep(5)
                continue
            for update in updates:
                message = update.get('message',{})
                allowed = (message.get('from',{}).get('id') == OWNER_ID and
                           message.get('chat',{}).get('type') == 'private' and
                           message.get('chat',{}).get('id') == OWNER_ID)
                if allowed:
                    text = message.get('text','')
                    if text.startswith(('/start','/help')):
                        await api.say(HELP)
                    elif text.strip() == '/status':
                        await api.say('Працюю на сервері. Надішли посилання на допис.')
                    else:
                        folder = Path(tempfile.mkdtemp(prefix='telegram-export-', dir=DATA_DIR))
                        try:
                            parse_link(text)
                            await api.say('Збираю коментарі та фото. Готовий Excel надішлю сюди.')
                            payload = await collect(client,text,api.say)
                            if not payload['rows']:
                                await api.say('У гілці немає доступних коментарів.')
                            else:
                                target = folder / f"comments_{payload['channel']}_{payload['post']}.xlsx"
                                await asyncio.to_thread(make_workbook,payload,target)
                                nicknames = sum(row['username'].startswith('@') for row in payload['rows'])
                                caption = f"Готово: {len(payload['rows'])} повідомлень, @нікнейми у {nicknames} рядках, фото у {payload['photos']} рядках.\n{payload['source']}"
                                await api.request('sendDocument',{'chat_id':OWNER_ID,'caption':caption},target)
                        except Exception as exc:
                            await api.say(friendly_error(exc))
                        finally:
                            shutil.rmtree(folder, ignore_errors=True)
                offset = update['update_id'] + 1
                atomic_json(OFFSET_FILE,{'offset':offset})
    finally:
        await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
