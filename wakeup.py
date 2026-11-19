#!/usr/bin/env python3
"""Telegram 起床電話：用 py-tgcalls 的 p2p 私人通話撥給指定使用者並播放鬧鈴。

用法：
  wakeup.py --login                     首次登入（驗證碼寫進 login_code 檔）
  wakeup.py --now                       立刻撥給 SELF_TEST_USERNAME 測試（只撥一通）
  wakeup.py --check [--target USER]     只做撥號前檢查，不撥號
  wakeup.py --date 2026-10-17 --at 06:00
                                        排程用：只在該日期（台北）執行，等到 06:00 整撥給 TARGET_USERNAME
"""
import argparse
import asyncio
import fcntl
import logging
import os
import platform
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from telethon import TelegramClient, events
from telethon import errors as tg_errors
from telethon.tl import types as tl
from telethon.tl.functions.users import GetFullUserRequest

from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import (
    CallBusy,
    CallDeclined,
    CallDiscarded,
    NotInCallError,
    TimedOutAnswer,
)
from pytgcalls.types import AudioQuality, CallConfig, ChatUpdate, MediaStream

BASE = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Taipei')
SESSION = BASE / 'wakeup'  # Telethon 會建立 wakeup.session
LOG_FILE = BASE / 'logs' / 'wakeup.log'
CODE_FILE = BASE / 'login_code'
PASSWORD_FILE = BASE / 'login_password'

# 這些 RPC 錯誤代表再撥也沒用，直接停止重撥
FATAL_CALL_ERRORS = {
    'USER_PRIVACY_RESTRICTED': '對方的隱私設定不允許這個帳號打給他',
    'USER_IS_BLOCKED': '對方封鎖了這個帳號（或這個帳號封鎖了對方）',
    'PARTICIPANT_VERSION_OUTDATED': '對方的 Telegram 版本太舊，不支援這種通話',
    'USER_ID_INVALID': '目標使用者 ID 無效',
    'CALL_PROTOCOL_FLAGS_INVALID': '通話協定參數無效',
}

log = logging.getLogger('wakeup')


def now_tw() -> datetime:
    return datetime.now(TZ)


def setup_logging(verbose: bool) -> None:
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
    logging.getLogger('telethon').setLevel(logging.INFO if verbose else logging.WARNING)
    logging.getLogger('pytgcalls').setLevel(logging.DEBUG if verbose else logging.INFO)


def load_settings() -> dict:
    settings = {}
    for name in ('config.env', '.env'):
        path = BASE / name
        if path.exists():
            settings.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    settings.update({k: v for k, v in os.environ.items() if k.startswith(('TG_', 'WAKEUP_'))})
    return settings


def parse_hhmm(value: str, base: datetime) -> datetime:
    hour, minute = (int(x) for x in value.split(':'))
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def acquire_lock():
    lock = open(BASE / '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error('已有另一個 wakeup.py 在執行（session 檔被占用），這次不執行')
        sys.exit(3)
    return lock


def make_client(settings: dict) -> TelegramClient:
    try:
        api_id = int(settings['TG_API_ID'])
        api_hash = settings['TG_API_HASH']
    except (KeyError, ValueError):
        log.error('.env 缺少 TG_API_ID / TG_API_HASH')
        sys.exit(2)
    return TelegramClient(
        str(SESSION), api_id, api_hash,
        device_model=f'tg-wakeup-call ({platform.node()})',
        system_version='Linux', app_version='wakeup 1.0',
    )


async def wait_for_file(path: Path, what: str, timeout: int) -> str:
    log.info('等待%s：請寫進 %s（最多等 %d 分鐘）', what, path, timeout // 60)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            value = path.read_text().strip()
            path.unlink()
            if value:
                return value
        await asyncio.sleep(1)
    raise TimeoutError(f'等{what}逾時')


async def do_login(settings: dict) -> int:
    phone = settings.get('TG_PHONE')
    if not phone:
        log.error('.env 缺少 TG_PHONE')
        return 2
    client = make_client(settings)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            log.info('已經登入：%s (@%s, id=%s)，不需要重新登入', me.first_name, me.username, me.id)
            return 0
        CODE_FILE.unlink(missing_ok=True)
        sent = await client.send_code_request(phone)
        log.info('驗證碼已送出到 %s，傳送方式：%s', phone, type(sent.type).__name__)
        for attempt in range(1, 4):
            code = await wait_for_file(CODE_FILE, '驗證碼', 1800)
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                break
            except tg_errors.PhoneCodeInvalidError:
                log.error('驗證碼錯誤（第 %d 次），請重新寫入正確的驗證碼', attempt)
            except tg_errors.PhoneCodeExpiredError:
                log.error('驗證碼已過期，請重新執行 --login')
                return 1
            except tg_errors.SessionPasswordNeededError:
                log.info('這個帳號有兩步驟驗證密碼')
                password = await wait_for_file(PASSWORD_FILE, '兩步驟驗證密碼', 1800)
                await client.sign_in(password=password)
                break
        else:
            log.error('驗證碼錯誤次數過多，放棄')
            return 1
        me = await client.get_me()
        log.info('登入成功：%s (@%s, id=%s)', me.first_name, me.username, me.id)
        return 0
    finally:
        await client.disconnect()
        session_file = SESSION.with_suffix('.session')
        if session_file.exists():
            session_file.chmod(0o600)


async def precheck(client: TelegramClient, me, username: str):
    """撥號前檢查。回傳 (user, 可撥號與否)。目標不存在等致命問題回傳 (None, False)。"""
    username = username.lstrip('@')
    try:
        user = await client.get_entity(username)
    except (tg_errors.UsernameNotOccupiedError, tg_errors.UsernameInvalidError, ValueError) as e:
        log.error('目標 @%s 不存在或無法解析：%s', username, e)
        return None, False
    if not isinstance(user, tl.User):
        log.error('目標 @%s 不是個人帳號（是 %s），不能打私人電話', username, type(user).__name__)
        return None, False
    if user.id == me.id:
        log.error('目標 @%s 就是登入帳號本身，Telegram 不能打給自己', username)
        return None, False
    if user.bot or user.deleted:
        log.error('目標 @%s 是 bot 或已刪除帳號，不能撥號', username)
        return None, False

    full = (await client(GetFullUserRequest(user))).full_user
    log.info(
        '目標 @%s：id=%s, 名稱=%s, phone_calls_available=%s, phone_calls_private=%s, blocked=%s',
        username, user.id, user.first_name, full.phone_calls_available,
        full.phone_calls_private, full.blocked,
    )
    ok = True
    if full.blocked:
        log.error('這個帳號封鎖了 @%s，撥號會失敗', username)
        ok = False
    if full.phone_calls_private:
        log.warning('@%s 的通話隱私設定限制了這個帳號（phone_calls_private），撥號可能被拒絕', username)
        ok = False
    if not full.phone_calls_available:
        log.warning('@%s 目前不接受通話（phone_calls_available=False）', username)
        ok = False
    return user, ok


async def run_calls(client, calls, user, opts) -> bool:
    """依設定撥號直到接通、達上限或遇到致命錯誤。回傳是否接通。"""
    stream = MediaStream(
        str(opts.alarm),
        audio_parameters=AudioQuality.HIGH,
        video_flags=MediaStream.Flags.IGNORE,
    )
    state = {'connected': False}
    ended = asyncio.Event()

    @calls.on_update(filters.stream_end())
    async def _loop_alarm(_, update):
        if update.chat_id == user.id and state['connected'] and not ended.is_set():
            log.info('音檔播完，重新播放')
            await calls.play(user.id, stream)

    @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
    async def _remote_left(_, update):
        if update.chat_id == user.id and state['connected']:
            log.info('對方掛斷了通話（%s）', update.status)
            ended.set()

    attempt = 0
    while True:
        attempt += 1
        if opts.give_up_at and now_tw() >= opts.give_up_at:
            log.warning('已到放棄時間 %s，不再撥號', opts.give_up_at.strftime('%H:%M'))
            return False
        log.info('第 %d 通：撥號給 @%s（響鈴上限 %d 秒）', attempt, user.username, opts.ring_timeout)
        try:
            await calls.play(user.id, stream, CallConfig(timeout=opts.ring_timeout))
        except TimedOutAnswer:
            log.info('第 %d 通：%d 秒內沒人接，已掛斷', attempt, opts.ring_timeout)
        except CallDeclined:
            log.info('第 %d 通：對方拒接或未接（通話被結束）', attempt)
        except CallBusy:
            log.info('第 %d 通：對方忙線中', attempt)
        except CallDiscarded:
            log.info('第 %d 通：通話在建立連線時被結束', attempt)
        except tg_errors.FloodWaitError as e:
            log.error('第 %d 通：Telegram 限流（FLOOD_WAIT %d 秒）', attempt, e.seconds)
            if e.seconds > 600:
                return False
            await asyncio.sleep(e.seconds)
        except tg_errors.RPCError as e:
            reason = FATAL_CALL_ERRORS.get(e.message)
            if reason:
                log.error('第 %d 通：撥號被 Telegram 拒絕 %s：%s，停止重撥', attempt, e.message, reason)
                return False
            log.error('第 %d 通：Telegram 錯誤 %s', attempt, e)
        except Exception:
            log.exception('第 %d 通：撥號時發生未預期錯誤', attempt)
        else:
            state['connected'] = True
            log.info('第 %d 通：已接通！開始播放鬧鈴 %d 秒', attempt, opts.play_seconds)
            try:
                await asyncio.wait_for(ended.wait(), timeout=opts.play_seconds)
            except asyncio.TimeoutError:
                log.info('已播放 %d 秒，掛斷', opts.play_seconds)
            ended.set()
            try:
                await calls.leave_call(user.id)
                log.info('已掛斷通話')
            except NotInCallError:
                log.info('通話已經結束（對方先掛斷）')
            except Exception:
                log.exception('掛斷時發生錯誤')
            return True

        if opts.max_redials is not None and attempt > opts.max_redials:
            log.warning('已達重撥上限（第一通 + 重撥 %d 次）', opts.max_redials)
            return False
        log.info('%d 秒後重撥', opts.retry_interval)
        await asyncio.sleep(opts.retry_interval)


async def run(opts, settings: dict) -> int:
    client = make_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            log.error('尚未登入（session 無效），請先執行 wakeup.py --login')
            return 2
        me = await client.get_me()
        log.info('登入帳號：%s (@%s, id=%s)', me.first_name, me.username, me.id)

        user, callable_ = await precheck(client, me, opts.target)
        if user is None:
            return 1
        if opts.check_only:
            log.info('檢查完成（--check，不撥號）：%s', '可以撥號' if callable_ else '可能無法撥號，見上方警告')
            return 0 if callable_ else 1

        call_ids = set()

        @client.on(events.Raw(tl.UpdatePhoneCall))
        async def _log_phone_call(event):
            call = event.phone_call
            # PhoneCallDiscarded 不帶參與者 id，只能用先前看過的 call id 比對
            if user.id in (getattr(call, 'participant_id', None), getattr(call, 'admin_id', None)):
                call_ids.add(call.id)
            elif call.id not in call_ids:
                return
            if isinstance(call, tl.PhoneCallWaiting) and call.receive_date:
                log.info('對方裝置已收到來電，正在響鈴')
            elif isinstance(call, tl.PhoneCallAccepted):
                log.info('對方按下接聽')
            elif isinstance(call, tl.PhoneCallDiscarded):
                reason = type(call.reason).__name__ if call.reason else 'None'
                log.info('通話結束，原因：%s，通話秒數：%s', reason, call.duration)

        if opts.at:
            wait = (opts.at - now_tw()).total_seconds()
            if wait > 0:
                log.info('已就緒，等待 %s 撥號（%.0f 秒後）', opts.at.strftime('%Y-%m-%d %H:%M:%S'), wait)
                await asyncio.sleep(wait)

        calls = PyTgCalls(client)
        await calls.start()
        answered = await run_calls(client, calls, user, opts)

        if answered:
            log.info('結果：已接通，任務完成')
            return 0
        log.warning('結果：沒有接通')
        if opts.fallback_message:
            try:
                await client.send_message(user, opts.fallback_message)
                log.info('已傳送文字訊息給 @%s：%s', user.username, opts.fallback_message)
            except Exception:
                log.exception('傳送文字訊息失敗')
        return 1
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description='Telegram 起床電話')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--login', action='store_true', help='首次登入')
    mode.add_argument('--now', action='store_true', help='立刻撥給 SELF_TEST_USERNAME 測試（預設只撥一通）')
    mode.add_argument('--check', action='store_true', help='只做撥號前檢查，不撥號')
    parser.add_argument('--target', help='撥給誰（username）；--now 預設 SELF_TEST_USERNAME，其餘預設 TARGET_USERNAME')
    parser.add_argument('--date', help='只在這個日期（台北，YYYY-MM-DD）執行；cron 沒有年份欄位，靠這個防止明年重複觸發')
    parser.add_argument('--at', help='等到台北時間 HH:MM 整才撥（讓 cron 提早一分鐘啟動並先連線）')
    parser.add_argument('--max-redials', help='第一通沒接後最多重撥幾次；unlimited = 不限')
    parser.add_argument('--ring-timeout', type=int)
    parser.add_argument('--retry-interval', type=int)
    parser.add_argument('--play-seconds', type=int)
    parser.add_argument('--give-up-at', help='台北時間 HH:MM 之後不再撥；none = 不限')
    parser.add_argument('--no-message', action='store_true', help='沒接通時不傳文字訊息')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    os.umask(0o077)
    setup_logging(args.verbose)
    settings = load_settings()
    lock = acquire_lock()  # noqa: F841  (持有到程式結束)

    if args.login:
        return asyncio.run(do_login(settings))

    start = now_tw()
    if args.date and start.date().isoformat() != args.date:
        log.info('今天（台北）是 %s，不是排定的 %s，不執行', start.date(), args.date)
        return 0

    opts = argparse.Namespace()
    opts.check_only = args.check
    opts.target = args.target or (settings.get('SELF_TEST_USERNAME') if args.now else settings.get('TARGET_USERNAME'))
    if not opts.target:
        log.error('沒有指定撥號對象（--target 或 config.env）')
        return 2
    opts.ring_timeout = args.ring_timeout or int(settings.get('RING_TIMEOUT', 45))
    opts.retry_interval = args.retry_interval or int(settings.get('RETRY_INTERVAL', 60))
    opts.play_seconds = args.play_seconds or int(settings.get('PLAY_SECONDS', 60))
    redials = args.max_redials if args.max_redials is not None else ('0' if args.now else settings.get('MAX_REDIALS', '3'))
    opts.max_redials = None if str(redials).strip().lower() in ('unlimited', '') else int(redials)
    give_up = args.give_up_at if args.give_up_at is not None else ('none' if args.now else settings.get('GIVE_UP_AT', ''))
    opts.give_up_at = None
    if give_up and give_up.lower() != 'none':
        opts.give_up_at = parse_hhmm(give_up, start)
        if opts.give_up_at <= start:
            opts.give_up_at += timedelta(days=1)
    opts.at = None
    if args.at:
        opts.at = parse_hhmm(args.at, start)
        if start - opts.at > timedelta(minutes=10):
            log.error('現在 %s 已超過排定時間 %s 十分鐘以上，不撥號', start.strftime('%H:%M'), args.at)
            return 1
        if opts.give_up_at and opts.give_up_at <= opts.at:
            opts.give_up_at += timedelta(days=1)
    opts.fallback_message = None if args.no_message else settings.get('FALLBACK_MESSAGE', '起床了')
    opts.alarm = BASE / settings.get('ALARM_FILE', 'alarm.mp3')
    if not opts.check_only and not opts.alarm.exists():
        log.error('找不到鬧鈴音檔 %s', opts.alarm)
        return 2

    log.info(
        '=== 啟動：模式=%s 目標=@%s 響鈴=%ds 重撥間隔=%ds 重撥上限=%s 放棄時間=%s 播放=%ds ===',
        'check' if args.check else 'now' if args.now else 'scheduled', opts.target.lstrip('@'),
        opts.ring_timeout, opts.retry_interval,
        '不限' if opts.max_redials is None else opts.max_redials,
        opts.give_up_at.strftime('%m-%d %H:%M') if opts.give_up_at else '不限', opts.play_seconds,
    )
    try:
        return asyncio.run(run(opts, settings))
    except Exception:
        log.exception('程式異常結束')
        return 1


if __name__ == '__main__':
    sys.exit(main())
