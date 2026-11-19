#!/usr/bin/env python3
"""Telegram 起床電話預約 bot（常駐，由 systemd 執行）。

私訊指令（BOT_GROUP_IDS 群組的成員才能用）：
  alarm 1025 0550                 10/25 05:50 叫自己起床
  alarm 1025 0550 @someone        叫別人（只有 BOT_ADMIN_IDS 可以）
  help / list / cancel 12         教學、自己的預約、取消 #12
  不是指令的私訊（一般聊天）一律不回。

群組指令（只在 BOT_GROUP_IDS 的群組、只有 BOT_ADMIN_IDS 可以用；其他人會被提示改用私訊）：
  @帳號 help                      教學
  @帳號 1025 0550 [@someone]      預約（前面加 alarm 也可以）
  @帳號 list                      列出本群組還沒執行的預約
  @帳號 cancel 12                 取消 #12

管理指令（在主機上執行，不需要停服務）：
  bot.py add --target someone --at "2026-10-17 06:00" --until 08:00 --fallback 起床了
  bot.py list
  bot.py cancel 12
"""
import argparse
import asyncio
import fcntl
import html
import logging
import platform
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Taipei')
DB_FILE = BASE / 'bookings.db'
LOG_FILE = BASE / 'logs' / 'bot.log'
SESSION = BASE / 'wakeup'
WEEKDAYS = '一二三四五六日'
ACTIVE = ('pending', 'running')
AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.wav', '.ogg', '.oga', '.opus', '.flac', '.webm'}

PRIVACY_HINT = (
    '請到 Telegram 設定 → 隱私與安全 → 通話，把「誰可以打電話給我」設成「所有人」，'
    '或把 @{me} 加到「永遠允許」，然後重新預約。'
)
FATAL_CALL_ERRORS = {
    'USER_PRIVACY_RESTRICTED': '對方的通話隱私設定不允許我打給他',
    'USER_IS_BLOCKED': '對方封鎖了我（或我封鎖了對方）',
    'PARTICIPANT_VERSION_OUTDATED': '對方的 Telegram 版本太舊，不支援這種通話',
    'USER_ID_INVALID': '對方帳號無效',
    'INPUT_USER_DEACTIVATED': '對方帳號已停用',
    'CALL_PROTOCOL_FLAGS_INVALID': '通話協定參數無效',
}

log = logging.getLogger('bot')


# ---------------------------------------------------------------- 基本工具

def now_tw() -> datetime:
    return datetime.now(TZ)


def fmt_dt(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, TZ)
    return f'{dt:%m/%d}（{WEEKDAYS[dt.weekday()]}）{dt:%H:%M}'


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(name)s: %(message)s')
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, TZ).timetuple()
    fmt.default_time_format = '%Y-%m-%d %H:%M:%S'
    fmt.default_msec_format = '%s.%03d+08:00'
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.getLogger('telethon').setLevel(logging.WARNING)


def load_settings() -> dict:
    settings = {}
    for name in ('config.env', '.env'):
        path = BASE / name
        if path.exists():
            settings.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    return settings


def id_list(value: str | None) -> set[int]:
    return {int(x) for x in (value or '').replace(' ', '').split(',') if x}


# ---------------------------------------------------------------- 資料庫

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,              -- 預約來源群組；NULL = 主機上用 bot.py add 建立
            msg_id INTEGER,               -- 預約訊息 id，結果回覆在它下面
            requester_id INTEGER,
            requester_name TEXT,
            target TEXT NOT NULL,         -- username 或數字 id
            target_id INTEGER,
            target_label TEXT,
            due_ts REAL NOT NULL,         -- 開始撥號時間
            deadline_ts REAL NOT NULL,    -- 超過就不再重撥
            ring_timeout INTEGER NOT NULL,
            retry_interval INTEGER NOT NULL,
            fallback_message TEXT,        -- 沒接通時私訊對方的內容；NULL = 不私訊
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            started_ts REAL,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL
        )''')
    return conn


def db_update(booking_id: int, **fields) -> None:
    fields['updated_ts'] = time.time()
    cols = ', '.join(f'{k} = ?' for k in fields)
    with db() as conn:
        conn.execute(f'UPDATE bookings SET {cols} WHERE id = ?', (*fields.values(), booking_id))


def db_get(booking_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,)).fetchone()


def db_insert(**fields) -> int:
    fields['created_ts'] = fields['updated_ts'] = time.time()
    cols = ', '.join(fields)
    marks = ', '.join('?' for _ in fields)
    with db() as conn:
        return conn.execute(f'INSERT INTO bookings ({cols}) VALUES ({marks})', tuple(fields.values())).lastrowid


# ---------------------------------------------------------------- 指令解析

class UserError(Exception):
    """要回覆給使用者看的錯誤。"""


def parse_when(mmdd: str, hhmm: str, now: datetime, max_days: int = 7) -> datetime:
    month, day, hour, minute = int(mmdd[:2]), int(mmdd[2:]), int(hhmm[:2]), int(hhmm[2:])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise UserError(f'時間 {hhmm} 不對，請用 24 小時制 HHMM，例如 0550')
    try:
        when = datetime(now.year, month, day, hour, minute, tzinfo=TZ)
        if when < now - timedelta(days=180):  # 跨年：12 月底預約 0102 → 明年
            when = when.replace(year=now.year + 1)
    except ValueError:
        raise UserError(f'日期 {mmdd} 不對，請用 MMDD，例如 1025') from None
    if when < now - timedelta(minutes=1):
        raise UserError(f'{mmdd[:2]}/{mmdd[2:]} {hhmm[:2]}:{hhmm[2:]} 已經過了')
    if when > now + timedelta(days=max_days):
        raise UserError(f'最多只能預約 {max_days} 天內的時間')
    return when


def parse_command(body: str) -> tuple[str, list[str]]:
    """回傳 (指令, 參數)。指令：help / book / list / cancel / unknown。開頭的 alarm 可有可無。"""
    parts = body.split()
    if parts and parts[0].lower() in ('alarm', '/alarm'):
        parts = parts[1:]
        if not parts:
            return 'help', []
    if not parts or parts[0].lower() in ('help', '/help', '/start', '教學', '說明', '幫助'):
        return 'help', []
    head = parts[0].lower()
    if head in ('list', '列表', '清單'):
        return 'list', []
    if head in ('cancel', '取消'):
        return 'cancel', parts[1:]
    if re.fullmatch(r'\d{4}', parts[0]) and len(parts) >= 2 and re.fullmatch(r'\d{4}', parts[1]) and len(parts) <= 3:
        return 'book', parts
    return 'unknown', parts


# ---------------------------------------------------------------- Bot

class WakeupBot:
    def __init__(self, settings: dict):
        from telethon import TelegramClient

        self.settings = settings
        self.group_ids = id_list(settings.get('BOT_GROUP_IDS'))
        self.admin_ids = id_list(settings.get('BOT_ADMIN_IDS'))
        self.ring_timeout = int(settings.get('GROUP_RING_TIMEOUT', 45))
        self.retry_interval = int(settings.get('GROUP_RETRY_INTERVAL', 60))
        self.max_minutes = int(settings.get('GROUP_MAX_MINUTES', 30))
        self.max_pending = int(settings.get('GROUP_MAX_PENDING_PER_USER', 3))
        self.max_days = int(settings.get('GROUP_MAX_DAYS_AHEAD', 14))
        self.play_seconds = int(settings.get('PLAY_SECONDS', 60))
        self.alarm = BASE / settings.get('ALARM_FILE', 'alarm.mp3')
        self.alarm_dir = BASE / settings['ALARM_DIR'] if settings.get('ALARM_DIR') else None
        self.client = TelegramClient(
            str(SESSION), int(settings['TG_API_ID']), settings['TG_API_HASH'],
            device_model=f'tg-wakeup-call ({platform.node()})', system_version='Linux', app_version='wakeup-bot 1.0',
        )
        self.calls = None
        self.me = None
        self.line = asyncio.Lock()          # 同一時間只打一通
        self.current = {'chat_id': None, 'connected': False, 'ended': asyncio.Event(), 'track': None}
        self.call_ids: dict[int, int] = {}  # phone_call.id -> user_id，只用來寫 log
        self.tasks: set[asyncio.Task] = set()
        self.member_cache: dict[int, tuple[bool, float]] = {}  # user_id -> (是否群組成員, 查詢時間)
        self.denied_notice: dict[int, float] = {}               # 非成員提示的冷卻

    # ------------------------------------------------ 啟動

    async def start(self) -> int:
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate
        from telethon import events
        from telethon.tl import types as tl

        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.error('尚未登入（session 無效），請先停止服務並執行 wakeup.py --login')
            return 2
        self.me = await self.client.get_me()
        log.info('登入帳號：@%s (id=%s)；群組（管理者指令 / 私訊成員資格）：%s；管理者：%s',
                 self.me.username, self.me.id, sorted(self.group_ids), sorted(self.admin_ids))

        self.calls = PyTgCalls(self.client)
        await self.calls.start()

        @self.calls.on_update(filters.stream_end())
        async def _loop_alarm(_, update):
            cur = self.current
            if update.chat_id == cur['chat_id'] and cur['connected'] and not cur['ended'].is_set():
                cur['track'] = await self.pick_alarm(exclude=cur['track'])
                log.info('音檔播完，接著播 %s', cur['track'].name)
                await self.calls.play(update.chat_id, self._stream(cur['track']))

        @self.calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _remote_left(_, update):
            cur = self.current
            if update.chat_id == cur['chat_id'] and cur['connected']:
                log.info('對方掛斷了通話')
                cur['ended'].set()

        @self.client.on(events.Raw(tl.UpdatePhoneCall))
        async def _log_phone_call(event):
            call = event.phone_call
            peer = getattr(call, 'participant_id', None)
            if peer is not None and peer != self.me.id:
                self.call_ids[call.id] = peer
            elif getattr(call, 'admin_id', None) not in (None, self.me.id):
                log.info('有人打電話給這個帳號（user id %s），不處理', call.admin_id)
                return
            user_id = self.call_ids.get(call.id)
            if user_id is None:
                return
            if isinstance(call, tl.PhoneCallWaiting) and call.receive_date:
                log.info('對方（%s）裝置已收到來電，正在響鈴', user_id)
            elif isinstance(call, tl.PhoneCallAccepted):
                log.info('對方（%s）按下接聽', user_id)
            elif isinstance(call, tl.PhoneCallDiscarded):
                reason = type(call.reason).__name__ if call.reason else 'None'
                log.info('與 %s 的通話結束，原因：%s，通話秒數：%s', user_id, reason, call.duration)
                self.call_ids.pop(call.id, None)

        @self.client.on(events.NewMessage(chats=list(self.group_ids), incoming=True))
        async def _on_group_message(event):
            try:
                await self.handle_message(event)
            except Exception:
                log.exception('處理群組訊息時發生錯誤')
                await self.reply(event, '⚠️ 處理指令時發生錯誤，請稍後再試。')

        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def _on_private_message(event):
            try:
                await self.handle_private(event)
            except Exception:
                log.exception('處理私訊時發生錯誤')
                await self.reply(event, '⚠️ 處理指令時發生錯誤，請稍後再試。')

        # 自我檢查：成員查詢在這個群組能不能用（群組隱藏成員清單時會全部查不到）
        for admin_id in sorted(self.admin_ids):
            log.info('成員檢查自我測試：%s 是群組成員 = %s', admin_id, await self.is_member(admin_id))
        self.member_cache.clear()

        self.recover()
        log.info('=== bot 已啟動，開始監聽群組、私訊與排程 ===')
        await asyncio.gather(self.scheduler(), self.client.run_until_disconnected())
        return 0

    def recover(self) -> None:
        """程式重啟後整理狀態：執行到一半的重新排入；從沒開始、又錯過 10 分鐘以上的標為 missed。"""
        now = time.time()
        with db() as conn:
            conn.execute("UPDATE bookings SET status='pending', updated_ts=? "
                         "WHERE status='running' AND deadline_ts > ?", (now, now))
            conn.execute("UPDATE bookings SET status='failed', result='程式中斷，已超過撥號期限', updated_ts=? "
                         "WHERE status='running'", (now,))
            missed = conn.execute("SELECT * FROM bookings WHERE status='pending' AND started_ts IS NULL "
                                  "AND due_ts < ?", (now - 600,)).fetchall()
        for row in missed:
            log.warning('#%s 錯過了（排定 %s，服務當時沒在執行）', row['id'], fmt_dt(row['due_ts']))
            db_update(row['id'], status='missed', result='服務當時沒在執行')
            self.spawn(self.report(row, f'⚠️ #{row["id"]} 錯過了（{fmt_dt(row["due_ts"])}），'
                                        '當時叫醒服務沒在執行，抱歉！請重新預約。'))

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ------------------------------------------------ 群組指令

    async def handle_message(self, event) -> None:
        if not event.message.mentioned:
            return
        text = event.raw_text or ''
        tag = re.compile(rf'@{re.escape(self.me.username)}\b', re.I) if self.me.username else None
        explicit = bool(tag and tag.search(text))
        body = tag.sub(' ', text).strip() if tag else text.strip()
        cmd, args = parse_command(body)
        if not explicit and cmd in ('help', 'unknown'):
            return  # 只是回覆了 bot 的訊息、沒有下指令
        sender = await event.get_sender()
        log.info('群組 %s 收到 %s(%s) 的指令：%r', event.chat_id, getattr(sender, 'username', None),
                 event.sender_id, body)
        if event.sender_id not in self.admin_ids:
            me = html.escape(self.me.username or '')
            await self.reply(event, f'群組裡的指令只有管理者能用 🙏\n要預約起床電話，請私訊 @{me}：'
                                    '<code>alarm 1025 0550</code>（日期 MMDD、時間 24 小時制 HHMM）')
            return
        if cmd == 'help':
            await self.reply(event, self.help_text(private=False, is_admin=True))
        elif cmd == 'list':
            await self.reply(event, self.list_text(event.chat_id))
        elif cmd == 'cancel':
            await self.reply(event, self.cancel(args, event.sender_id))
        elif cmd == 'book':
            try:
                await self.reply(event, await self.book(event, sender, args))
            except UserError as e:
                await self.reply(event, f'❌ {html.escape(str(e))}')
        else:
            await self.reply(event, '看不懂這個指令 🤔\n\n' + self.help_text(private=False, is_admin=True))

    async def handle_private(self, event) -> None:
        from telethon.tl import types as tl

        text = (event.raw_text or '').strip()
        if not text:
            return
        cmd, args = parse_command(text)
        if cmd == 'unknown':
            if text.split()[0].lower() in ('alarm', '/alarm'):
                await self.reply(event, '格式是 <code>alarm 日期 時間</code>，例如 <code>alarm 1025 0550</code>'
                                        '（日期 MMDD、時間 24 小時制 HHMM）')
            return  # 一般聊天，不回
        sender = await event.get_sender()
        if not isinstance(sender, tl.User) or sender.bot or sender.id == 777000:
            return
        log.info('私訊收到 %s(%s) 的指令：%r', sender.username, sender.id, text)
        is_admin = sender.id in self.admin_ids
        if not is_admin and not await self.is_member(sender.id):
            log.info('%s(%s) 不是群組成員，拒絕', sender.username, sender.id)
            if time.time() - self.denied_notice.get(sender.id, 0) > 600:
                self.denied_notice[sender.id] = time.time()
                await self.reply(event, '抱歉，這個起床電話服務只開放給特定群組的成員使用。')
            return
        if cmd == 'help':
            await self.reply(event, self.help_text(private=True, is_admin=is_admin))
        elif cmd == 'list':
            await self.reply(event, self.list_mine(sender.id))
        elif cmd == 'cancel':
            await self.reply(event, self.cancel(args, sender.id))
        elif cmd == 'book':
            try:
                await self.reply(event, await self.book(event, sender, args, private=True))
            except UserError as e:
                await self.reply(event, f'❌ {html.escape(str(e))}')

    async def is_member(self, user_id: int) -> bool:
        """是否為 BOT_GROUP_IDS 任一群組的成員。成功結果快取 10 分鐘、失敗 1 分鐘；查不到就當不是（fail-closed）。"""
        from telethon import errors as tg_errors

        cached = self.member_cache.get(user_id)
        if cached and time.time() - cached[1] < (600 if cached[0] else 60):
            return cached[0]
        result = False
        for group_id in self.group_ids:
            try:
                perms = await self.client.get_permissions(group_id, user_id)
                if not perms.has_left and not perms.is_banned:
                    result = True
                    break
            except tg_errors.UserNotParticipantError:
                continue
            except Exception:
                log.exception('檢查 %s 是否為群組 %s 成員時失敗', user_id, group_id)
        self.member_cache[user_id] = (result, time.time())
        return result

    def help_text(self, private: bool, is_admin: bool) -> str:
        me = html.escape(self.me.username or '')
        rules = (
            f'時間到會打 Telegram 語音電話，接起來會播鬧鈴；沒接每 {self.retry_interval} 秒重撥，'
            f'最多 {self.max_minutes} 分鐘。\n'
            f'預約時會先檢查能不能打給你：如果你的通話隱私設定擋住了，請把 @{me} 加到「永遠允許」後重新預約。'
        )
        if private:
            lines = ['<b>☎️ 起床電話預約</b>\n',
                     '<code>alarm 1025 0550</code>',
                     '　10/25 05:50 打電話叫你起床（日期 MMDD、時間 24 小時制 HHMM）']
            if is_admin:
                lines += ['<code>alarm 1025 0550 @某人</code>　叫別人起床（管理者）']
            lines += ['<code>list</code>　你的預約',
                      '<code>cancel 12</code>　取消 #12\n',
                      rules]
            return '\n'.join(lines)
        return '\n'.join([
            '<b>☎️ 起床電話預約（群組指令限管理者）</b>\n',
            f'<code>@{me} 1025 0550</code>　叫自己（日期 MMDD、時間 24 小時制 HHMM）',
            f'<code>@{me} 1025 0550 @某人</code>　叫別人',
            f'<code>@{me} list</code>　本群還沒執行的預約',
            f'<code>@{me} cancel 12</code>　取消 #12\n',
            f'其他成員請私訊 @{me}：<code>alarm 1025 0550</code>\n',
            rules,
        ])

    def list_mine(self, user_id: int) -> str:
        with db() as conn:
            rows = conn.execute("SELECT * FROM bookings WHERE status IN ('pending','running') "
                                'AND (requester_id = ? OR target_id = ?) ORDER BY due_ts', (user_id, user_id)).fetchall()
        if not rows:
            return '你目前沒有預約。'
        lines = ['<b>你的預約</b>']
        for r in rows:
            state = '（撥號中）' if r['status'] == 'running' else ''
            lines.append(f'#{r["id"]} {fmt_dt(r["due_ts"])} → {html.escape(r["target_label"] or r["target"])}{state}')
        return '\n'.join(lines)

    def list_text(self, chat_id: int) -> str:
        with db() as conn:
            rows = conn.execute("SELECT * FROM bookings WHERE chat_id = ? AND status IN ('pending','running') "
                                'ORDER BY due_ts', (chat_id,)).fetchall()
        if not rows:
            return '目前沒有預約。'
        lines = ['<b>還沒執行的預約</b>']
        for r in rows:
            state = '（撥號中）' if r['status'] == 'running' else ''
            lines.append(f'#{r["id"]} {fmt_dt(r["due_ts"])} → {html.escape(r["target_label"] or r["target"])}{state}')
        return '\n'.join(lines)

    def cancel(self, args: list[str], sender_id: int) -> str:
        if not args or not args[0].lstrip('#').isdigit():
            return '請告訴我要取消哪一筆，例如 <code>cancel 12</code>（編號可以用 list 查）'
        row = db_get(int(args[0].lstrip('#')))
        if row is None or row['status'] not in ACTIVE:
            return f'找不到還沒執行的 #{html.escape(args[0].lstrip("#"))}'
        if sender_id not in (row['requester_id'], row['target_id']) and sender_id not in self.admin_ids:
            return '只有預約的人、被叫的人或管理者可以取消這筆預約'
        db_update(row['id'], status='cancelled', result=f'由 {sender_id} 取消')
        log.info('#%s 由 %s 取消', row['id'], sender_id)
        return f'🗑 已取消 #{row["id"]}（{fmt_dt(row["due_ts"])} → {html.escape(row["target_label"] or row["target"])}）'

    async def book(self, event, sender, args: list[str], private: bool = False) -> str:
        now = now_tw()
        when = parse_when(args[0], args[1], now, self.max_days)
        is_admin = event.sender_id in self.admin_ids

        if len(args) == 3:
            target = args[2]
            if not target.startswith('@') and not target.isdigit():
                raise UserError('對象請用 @username，或留空代表自己')
            # 非管理者先擋，不讓別人拿登入帳號去查任意 username（限流、探測帳號）
            own = {str(sender.id)} | ({sender.username.lower()} if sender.username else set())
            if not is_admin and target.lstrip('@').lower() not in own:
                raise UserError('目前只能預約叫自己起床（把對象留空就好）')
        else:
            target = sender
        user, reason = await self.check_callable(target)
        if user is None:
            raise UserError(reason)
        if user.id != event.sender_id and not is_admin:
            raise UserError('目前只能預約叫自己起床（把對象留空就好）')
        if reason:
            raise UserError(f'現在沒辦法打給 {self.label(user, plain=True)}：{reason}。\n'
                            + PRIVACY_HINT.format(me=self.me.username))

        due_ts = when.timestamp()
        deadline_ts = due_ts + self.max_minutes * 60
        with db() as conn:
            if not is_admin:
                pending = conn.execute("SELECT COUNT(*) FROM bookings WHERE requester_id = ? AND status IN "
                                       "('pending','running')", (event.sender_id,)).fetchone()[0]
                if pending >= self.max_pending:
                    raise UserError(f'你已經有 {pending} 筆預約了，最多 {self.max_pending} 筆')
            # bot.py add 建立的預約可能只有 username、還沒有 target_id，也要一起比對
            clash = conn.execute(
                "SELECT * FROM bookings WHERE status IN ('pending','running') AND due_ts < ? AND deadline_ts > ? "
                'AND (target_id = ? OR (target_id IS NULL AND lower(target) = lower(?)))',
                (deadline_ts, due_ts, user.id, user.username or str(user.id))).fetchone()
        if clash:
            raise UserError(f'{fmt_dt(clash["due_ts"])} 已經有 #{clash["id"]} 要叫 {self.label(user, plain=True)} 了')

        booking_id = db_insert(
            chat_id=event.chat_id, msg_id=event.id, requester_id=event.sender_id,
            requester_name=self.label(sender, plain=True), target=str(user.id), target_id=user.id,
            target_label=self.label(user, plain=True), due_ts=due_ts, deadline_ts=deadline_ts,
            ring_timeout=self.ring_timeout, retry_interval=self.retry_interval, fallback_message=None,
        )
        log.info('新增預約 #%s（%s）：%s 叫 %s(%s)，%s', booking_id, '私訊' if private else '群組',
                 event.sender_id, user.username, user.id, fmt_dt(due_ts))
        cancel_cmd = f'cancel {booking_id}' if private else f'@{html.escape(self.me.username)} cancel {booking_id}'
        return (f'✅ 已預約 #{booking_id}：{fmt_dt(due_ts)} 打給 {html.escape(self.label(user, plain=True))}\n'
                f'沒接會每 {self.retry_interval} 秒重撥，最多 {self.max_minutes} 分鐘。'
                f'取消請傳 <code>{cancel_cmd}</code>')

    # ------------------------------------------------ 撥號前檢查

    async def check_callable(self, target):
        """回傳 (user, reason)。user 為 None 代表對象根本不能用；reason 不為 None 代表現在打不通。"""
        from telethon import errors as tg_errors
        from telethon.tl import types as tl
        from telethon.tl.functions.users import GetFullUserRequest

        if isinstance(target, str) and target.isdigit():
            target = int(target)
        try:
            user = target if isinstance(target, tl.User) else await self.client.get_entity(target)
        except (ValueError, TypeError, tg_errors.UsernameNotOccupiedError, tg_errors.UsernameInvalidError):
            return None, f'找不到 {target}（username 打錯或帳號不存在）'
        if not isinstance(user, tl.User):
            return None, '對象不是個人帳號'
        if user.id == self.me.id:
            return None, '我沒辦法打給我自己 😅'
        if user.bot:
            return None, '對方是 bot，不能打電話'
        if user.deleted:
            return None, '對方帳號已刪除'
        full = (await self.client(GetFullUserRequest(user))).full_user
        log.info('檢查 %s(%s)：phone_calls_available=%s phone_calls_private=%s blocked=%s',
                 user.username, user.id, full.phone_calls_available, full.phone_calls_private, full.blocked)
        if full.blocked:
            return user, '我封鎖了對方'
        if full.phone_calls_private:
            return user, '對方的通話隱私設定不允許我打給他'
        if not full.phone_calls_available:
            return user, '對方目前不接受 Telegram 通話'
        return user, None

    @staticmethod
    def label(user, plain: bool = False) -> str:
        """plain：純文字（@username 或名字）；否則回傳 HTML mention。"""
        if user is None:
            return '?'
        name = ' '.join(x for x in (getattr(user, 'first_name', None), getattr(user, 'last_name', None)) if x) or str(user.id)
        if plain:
            return f'@{user.username}' if getattr(user, 'username', None) else name
        return f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'

    # ------------------------------------------------ 排程與撥號

    async def scheduler(self) -> None:
        while True:
            try:
                with db() as conn:
                    rows = conn.execute("SELECT * FROM bookings WHERE status='pending' AND due_ts <= ? "
                                        'ORDER BY due_ts', (time.time(),)).fetchall()
                for row in rows:
                    db_update(row['id'], status='running', started_ts=row['started_ts'] or time.time())
                    self.spawn(self.execute(row['id']))
            except Exception:
                log.exception('排程迴圈發生錯誤')
            await asyncio.sleep(2)

    async def execute(self, booking_id: int) -> None:
        row = db_get(booking_id)
        log.info('#%s 開始：目標 %s，撥到 %s 為止', booking_id, row['target'],
                 datetime.fromtimestamp(row['deadline_ts'], TZ).strftime('%m-%d %H:%M'))
        try:
            user, reason = await self.check_callable(row['target'])
            if user is None or reason:
                log.error('#%s 撥號前檢查失敗：%s', booking_id, reason)
                db_update(booking_id, status='failed', result=reason)
                await self.report(row, f'❌ #{booking_id} 現在沒辦法打給 {html.escape(row["target_label"] or row["target"])}：'
                                       f'{html.escape(reason)}。\n' + PRIVACY_HINT.format(me=html.escape(self.me.username)))
                return
            if row['target_id'] is None:
                db_update(booking_id, target_id=user.id)
            attempt = 0
            while time.time() < row['deadline_ts']:
                if db_get(booking_id)['status'] != 'running':
                    log.info('#%s 已被取消，停止撥號', booking_id)
                    return
                attempt += 1
                async with self.line:
                    if time.time() >= row['deadline_ts']:
                        break
                    outcome = await self.place_call(booking_id, attempt, user, row['ring_timeout'])
                if outcome == 'answered':
                    db_update(booking_id, status='answered', result=f'第 {attempt} 通接通')
                    await self.report(row, f'☀️ #{booking_id} {self.label(user)} 在第 {attempt} 通接起來了，起床囉！')
                    return
                if outcome.startswith('fatal:'):
                    reason = outcome[6:]
                    db_update(booking_id, status='failed', result=reason)
                    await self.report(row, f'❌ #{booking_id} 打不通 {self.label(user)}：{html.escape(reason)}。\n'
                                           + PRIVACY_HINT.format(me=html.escape(self.me.username)))
                    return
                wait = min(row['retry_interval'], row['deadline_ts'] - time.time())
                if wait <= 0:
                    break
                log.info('#%s %d 秒後重撥', booking_id, row['retry_interval'])
                await asyncio.sleep(wait)
            log.warning('#%s 到期限都沒接通（共 %d 通）', booking_id, attempt)
            db_update(booking_id, status='unanswered', result=f'{attempt} 通都沒接')
            await self.report(row, f'😴 #{booking_id} 叫不醒 {self.label(user)}：打了 {attempt} 通都沒接。')
            if row['fallback_message']:
                try:
                    await self.client.send_message(user, row['fallback_message'])
                    log.info('#%s 已私訊 %s：%s', booking_id, user.id, row['fallback_message'])
                except Exception:
                    log.exception('#%s 私訊失敗', booking_id)
        except Exception as e:
            log.exception('#%s 執行時發生未預期錯誤', booking_id)
            db_update(booking_id, status='failed', result=f'程式錯誤：{e}')
            await self.report(row, f'⚠️ #{booking_id} 執行時發生錯誤，請重新預約。')

    @staticmethod
    def _stream(track: Path):
        from pytgcalls.types import AudioQuality, MediaStream
        # IGNORE：帶封面圖的 mp3 會被當成影片，不忽略的話會變成視訊通話
        return MediaStream(str(track), audio_parameters=AudioQuality.HIGH,
                           video_flags=MediaStream.Flags.IGNORE)

    async def pick_alarm(self, exclude: Path | None = None) -> Path:
        """每次重新讀 ALARM_DIR，隨機挑一個有音訊的檔案；都不行就用預設 ALARM_FILE。"""
        files = []
        if self.alarm_dir and self.alarm_dir.is_dir():
            files = [p for p in self.alarm_dir.iterdir()
                     if p.is_file() and not p.name.startswith('.') and p.suffix.lower() in AUDIO_EXTS]
        random.shuffle(files)
        if exclude in files and len(files) > 1:
            files.remove(exclude)
            files.append(exclude)  # 上一首排到最後，只有其他都不能播時才重複
        for track in files:
            if await self._has_audio(track):
                return track
            log.error('鬧鈴 %s 讀不到音訊（壞檔或格式不支援），略過', track.name)
        if self.alarm_dir:
            log.warning('隨機鬧鈴資料夾沒有可用的檔案，改用預設 %s', self.alarm.name)
        return self.alarm

    @staticmethod
    async def _has_audio(track: Path) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                'ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=codec_type',
                '-of', 'csv=p=0', str(track), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return b'audio' in out
        except Exception:
            log.exception('ffprobe 檢查 %s 失敗', track.name)
            return False

    async def place_call(self, booking_id: int, attempt: int, user, ring_timeout: int) -> str:
        """撥一通。回傳 'answered' / 'no_answer' / 'fatal:<原因>'。"""
        from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, NotInCallError, TimedOutAnswer
        from pytgcalls.types import CallConfig
        from telethon import errors as tg_errors

        cur = self.current
        track = await self.pick_alarm()
        cur.update(chat_id=user.id, connected=False, ended=asyncio.Event(), track=track)
        log.info('#%s 第 %d 通：撥號給 %s(%s)（響鈴上限 %d 秒，鬧鈴：%s）',
                 booking_id, attempt, user.username, user.id, ring_timeout, track.name)
        try:
            await self.calls.play(user.id, self._stream(track), CallConfig(timeout=ring_timeout))
        except TimedOutAnswer:
            log.info('#%s 第 %d 通：%d 秒內沒人接，已掛斷', booking_id, attempt, ring_timeout)
        except CallDeclined:
            log.info('#%s 第 %d 通：對方拒接或未接（通話被結束）', booking_id, attempt)
        except CallBusy:
            log.info('#%s 第 %d 通：對方忙線中', booking_id, attempt)
        except CallDiscarded:
            log.info('#%s 第 %d 通：通話在建立連線時被結束', booking_id, attempt)
        except tg_errors.FloodWaitError as e:
            log.error('#%s 第 %d 通：Telegram 限流 %d 秒', booking_id, attempt, e.seconds)
            if e.seconds > 600:
                return f'fatal:Telegram 限制撥號 {e.seconds} 秒'
            await asyncio.sleep(e.seconds)
        except tg_errors.RPCError as e:
            reason = FATAL_CALL_ERRORS.get(e.message)
            if reason:
                log.error('#%s 第 %d 通：被 Telegram 拒絕 %s：%s', booking_id, attempt, e.message, reason)
                return f'fatal:{reason}'
            log.error('#%s 第 %d 通：Telegram 錯誤 %s', booking_id, attempt, e)
        except Exception:
            log.exception('#%s 第 %d 通：撥號時發生未預期錯誤', booking_id, attempt)
        else:
            cur['connected'] = True
            log.info('#%s 第 %d 通：已接通！播放鬧鈴 %d 秒', booking_id, attempt, self.play_seconds)
            try:
                await asyncio.wait_for(cur['ended'].wait(), timeout=self.play_seconds)
            except asyncio.TimeoutError:
                log.info('#%s 已播放 %d 秒，掛斷', booking_id, self.play_seconds)
            cur['ended'].set()
            try:
                await self.calls.leave_call(user.id)
                log.info('#%s 已掛斷通話', booking_id)
            except NotInCallError:
                log.info('#%s 通話已經結束（對方先掛斷）', booking_id)
            except Exception:
                log.exception('#%s 掛斷時發生錯誤', booking_id)
            return 'answered'
        finally:
            cur['chat_id'] = None
        return 'no_answer'

    # ------------------------------------------------ 回覆

    async def reply(self, event, text: str) -> None:
        try:
            await event.reply(text, parse_mode='html', link_preview=False)
        except Exception:
            log.exception('回覆群組訊息失敗')

    async def report(self, row, text: str) -> None:
        if not row['chat_id']:
            return
        for reply_to in (row['msg_id'], None):
            try:
                await self.client.send_message(row['chat_id'], text, reply_to=reply_to,
                                               parse_mode='html', link_preview=False)
                return
            except Exception:
                log.exception('#%s 回報到群組失敗（reply_to=%s）', row['id'], reply_to)


# ---------------------------------------------------------------- 主機上的管理指令

def cli_add(args) -> int:
    now = now_tw()
    when = now if args.at == 'now' else datetime.strptime(args.at, '%Y-%m-%d %H:%M').replace(tzinfo=TZ)
    if when < now - timedelta(minutes=1):
        print(f'時間 {when:%Y-%m-%d %H:%M} 已經過了')
        return 1
    if args.until:
        deadline = datetime.combine(when.date(), datetime.strptime(args.until, '%H:%M').time(), TZ)
        if deadline <= when:
            deadline += timedelta(days=1)
    else:
        deadline = when + timedelta(minutes=args.minutes)
    target = args.target.lstrip('@')
    with db() as conn:
        clash = conn.execute(
            "SELECT * FROM bookings WHERE status IN ('pending','running') AND due_ts < ? AND deadline_ts > ? "
            'AND (lower(target) = lower(?) OR lower(target_label) = lower(?) OR target_id = ?)',
            (deadline.timestamp(), when.timestamp(), target, f'@{target}',
             int(target) if target.isdigit() else -1)).fetchone()
    if clash and not args.force:
        print(f'@{target} 在這段時間已經有 #{clash["id"]}（{fmt_dt(clash["due_ts"])}）了；確定要重複預約請加 --force')
        return 1
    booking_id = db_insert(
        chat_id=args.chat_id, msg_id=None, requester_id=None, requester_name='cli',
        target=target, target_id=int(target) if target.isdigit() else None, target_label=f'@{target}',
        due_ts=when.timestamp(), deadline_ts=deadline.timestamp(),
        ring_timeout=args.ring_timeout, retry_interval=args.retry_interval, fallback_message=args.fallback,
    )
    print(f'已新增 #{booking_id}：{when:%Y-%m-%d %H:%M} 打給 @{target}，撥到 {deadline:%m-%d %H:%M} 為止，'
          f'沒接通私訊：{args.fallback or "（不私訊）"}')
    return 0


def cli_list(args) -> int:
    with db() as conn:
        query = 'SELECT * FROM bookings' + ('' if args.all else " WHERE status IN ('pending','running')")
        rows = conn.execute(query + ' ORDER BY due_ts').fetchall()
    for r in rows:
        print(f'#{r["id"]:<4} {r["status"]:<10} {fmt_dt(r["due_ts"])} → {r["target_label"] or r["target"]:<20} '
              f'到 {datetime.fromtimestamp(r["deadline_ts"], TZ):%m-%d %H:%M}  群組={r["chat_id"]}  '
              f'by={r["requester_name"]}  {r["result"] or ""}')
    if not rows:
        print('（沒有預約）')
    return 0


def cli_cancel(args) -> int:
    row = db_get(args.id)
    if row is None or row['status'] not in ACTIVE:
        print(f'找不到還沒執行的 #{args.id}')
        return 1
    db_update(args.id, status='cancelled', result='由主機 cli 取消')
    print(f'已取消 #{args.id}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Telegram 起床電話預約 bot')
    sub = parser.add_subparsers(dest='cmd')
    sub.add_parser('run', help='常駐執行（systemd 用）')
    p_add = sub.add_parser('add', help='直接新增預約（不經群組）')
    p_add.add_argument('--target', required=True, help='username 或 user id')
    p_add.add_argument('--at', required=True, help='台北時間 "YYYY-MM-DD HH:MM" 或 now')
    p_add.add_argument('--until', help='台北時間 HH:MM 之後不再重撥')
    p_add.add_argument('--minutes', type=int, default=30, help='沒有 --until 時，最多撥幾分鐘')
    p_add.add_argument('--ring-timeout', type=int, default=45)
    p_add.add_argument('--retry-interval', type=int, default=60)
    p_add.add_argument('--fallback', help='沒接通時私訊對方的內容')
    p_add.add_argument('--chat-id', type=int, help='結果要回報到哪個群組（預設不回報）')
    p_add.add_argument('--force', action='store_true', help='同一個人時段重疊也照樣新增')
    p_list = sub.add_parser('list', help='列出預約')
    p_list.add_argument('--all', action='store_true', help='包含已結束的')
    p_cancel = sub.add_parser('cancel', help='取消預約')
    p_cancel.add_argument('id', type=int)
    args = parser.parse_args()

    if args.cmd == 'add':
        return cli_add(args)
    if args.cmd == 'list':
        return cli_list(args)
    if args.cmd == 'cancel':
        return cli_cancel(args)

    setup_logging()
    lock = open(BASE / '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error('session 正被另一個程式使用（wakeup.py 或另一個 bot），這次不啟動')
        return 3
    async def amain() -> int:
        # TelegramClient / asyncio 物件要在執行中的 event loop 裡建立
        return await WakeupBot(load_settings()).start()

    try:
        return asyncio.run(amain())
    except Exception:
        log.exception('bot 異常結束')
        return 1


if __name__ == '__main__':
    sys.exit(main())
