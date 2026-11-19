# tg-wakeup-call

用 Telegram 私人通話（p2p call）在指定時間打電話叫人起床。接通後播放鬧鈴，沒接就重撥。指定群組的成員私訊帳號預約，管理者也可以在群組裡預約。

- 通話：py-tgcalls 3.0.0（ntgcalls 3.0.3），`play(user_id, MediaStream, CallConfig(timeout=響鈴秒數))` 正常返回 = 對方接通
- MTProto client：Telethon 1.45.0（py-tgcalls 3.0 只支援 Pyrogram 系與 Telethon；bridge 需要新 layer，Telethon 要 ≥ 1.40）
- 登入帳號：一般 Telegram 使用者帳號（不是 Bot API；session：`wakeup.session`，權限 600）
- 以 systemd 服務 `tg-wakeup-bot` 常駐，預設路徑 `/root/tg-wakeup-call`

## 檔案

| 檔案 | 說明 |
|---|---|
| `bot.py` | 常駐服務：監聽私訊與群組指令、按時撥號；也提供 `add` / `list` / `cancel` 管理指令 |
| `wakeup.py` | 單次執行工具：`--login`、`--check`、`--now`（要先停 bot 服務） |
| `.env` | 機密：`TG_API_ID`、`TG_API_HASH`、`TG_PHONE`（600，不要 commit；範本見 `.env.example`） |
| `config.env` | 一般設定：開放群組、管理者、群組預約規則、鬧鈴、播放秒數（不 commit；範本見 `config.env.example`） |
| `bookings.db` | 預約資料（SQLite） |
| `alarm.mp3` | 鬧鈴（880/1320 Hz 嗶聲，10 秒，會循環播放） |
| `tg-wakeup-bot.service` | systemd unit（已複製到 `/etc/systemd/system/`） |
| `logs/bot.log` | 服務 log：群組指令、預約、撥號、響鈴、接通、掛斷原因、錯誤，含台北時間戳 |

## 怎麼預約

### 私訊（一般成員）

私訊登入帳號。只有 `config.env` 的 `BOT_GROUP_IDS` 群組的成員可以用：

```
alarm 1025 0550          10/25 05:50 叫自己（日期 MMDD、時間 24 小時制 HHMM）
alarm 1025 0550 @某人    叫別人（只有管理者）
help                     教學
list                     自己預約的、或要叫自己的
cancel 12                取消（預約人、被叫的人、管理者）
```

- 不是指令的私訊（一般聊天）一律不回；Telegram 官方帳號 777000 的訊息會忽略。
- 非群組成員會收到「只開放給特定群組的成員」，同一個人 10 分鐘內只提示一次。成員資格成功的結果快取 10 分鐘、失敗的快取 1 分鐘；啟動時會拿管理者做一次成員檢查的自我測試並寫進 log。
- 私訊建立的預約，結果回覆在私訊裡。

### 群組（只限管理者）

`BOT_ADMIN_IDS` 的管理者在群組 tag 帳號：

```
@帳號 help              教學
@帳號 1025 0550         叫自己（前面加 alarm 也可以）
@帳號 1025 0550 @某人   叫別人
@帳號 list              本群還沒執行的預約
@帳號 cancel 12         取消
```

其他人在群組 tag 帳號會收到提示，請他改用私訊；回覆 bot 訊息但沒 tag 的不處理。

### 共通規則

- 預約時立即檢查：對方存在、是一般使用者、`phone_calls_available=True`、`phone_calls_private=False`、沒有封鎖。打不通就回覆原因與解法（把登入帳號加到通話隱私的「永遠允許」），請對方調整後重新預約。
- 時間到再檢查一次，然後撥號：每通響 `GROUP_RING_TIMEOUT`（45）秒，沒接隔 `GROUP_RETRY_INTERVAL`（60）秒重撥，最多 `GROUP_MAX_MINUTES`（30）分鐘。接通後播 `PLAY_SECONDS`（60）秒或到對方掛斷；結果回覆在原本的預約訊息下面，叫不醒會 tag 對方。
- 限制：一般成員最多 `GROUP_MAX_PENDING_PER_USER`（3）筆、最多預約 `GROUP_MAX_DAYS_AHEAD`（14）天內；同一個人的預約時段不能重疊。
- 同一時間只打一通；多筆預約撞期時會在重撥空檔輪流打。
- 撥號被 Telegram 拒絕（`USER_PRIVACY_RESTRICTED`、`USER_IS_BLOCKED`、`PARTICIPANT_VERSION_OUTDATED`…）會停止並回報原因。

## 主機上管理預約（不用停服務）

```bash
cd /root/tg-wakeup-call
.venv/bin/python bot.py list            # 還沒執行的；加 --all 看全部
.venv/bin/python bot.py add --target someone --at "2026-10-17 06:00" --until 08:00 --fallback 起床了
.venv/bin/python bot.py add --target your_username --at now --minutes 2     # 立刻測試撥號
.venv/bin/python bot.py cancel 12
```

`add` 的選項：`--until HH:MM`（撥到幾點為止）或 `--minutes N`（預設 30）、`--ring-timeout`、`--retry-interval`、`--fallback`（沒接通時私訊對方）、`--chat-id`（結果回報到哪個群組，預設不回報）。

## 服務

```bash
systemctl status tg-wakeup-bot
systemctl restart tg-wakeup-bot     # 改了 config.env / 換了 alarm.mp3 之後
systemctl disable --now tg-wakeup-bot   # 停用（預約會保留在 bookings.db）
tail -f /root/tg-wakeup-call/logs/bot.log
```

服務重啟時：撥號到一半的預約會重新開始撥（在期限內）；從沒開始、又錯過 10 分鐘以上的會標成 `missed` 並在群組通知。

## 安裝與首次登入

需要 Python ≥ 3.10、`ffmpeg` 與 `ffprobe`（在 PATH 上）。ntgcalls 只有 glibc x86_64 / aarch64 的 wheel（不支援 Alpine）。

```bash
cd /root/tg-wakeup-call
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # 填入 api_id / api_hash / 手機
cp config.env.example config.env          # 填入群組、管理者
systemctl stop tg-wakeup-bot              # session 同時只能給一個程式用
.venv/bin/python wakeup.py --login
```

`--login` 送出驗證碼後會等 30 分鐘。把驗證碼寫進 `login_code`（有兩步驟驗證密碼時再寫進 `login_password`），程式讀完立即刪除：

```bash
echo 12345 > /root/tg-wakeup-call/login_code
read -rsp '2FA 密碼: ' p; printf '%s' "$p" > /root/tg-wakeup-call/login_password; unset p
```

登入後 `systemctl start tg-wakeup-bot`。

## 隨機鬧鈴

把音樂放進 `alarms/`（`config.env` 的 `ALARM_DIR`）就好，**不用重啟服務**：

```bash
scp *.mp3 root@你的主機:/root/tg-wakeup-call/alarms/
```

- 每一通接起來時隨機挑一首；播完還沒到 `PLAY_SECONDS` 就再隨機換一首（不重複剛播的那首）。log 會記下這通用了哪個檔案。
- 支援 mp3、m4a、aac、wav、ogg、opus、flac、webm；隱藏檔（`.` 開頭）不會被選到。
- 每次挑選前會用 ffprobe 確認檔案有音訊，壞檔會略過並在 log 記錯誤。
- `alarms/` 是空的或沒有可用檔案時，改用 `ALARM_FILE`（`alarm.mp3`，嗶聲）。
- `alarms/` 裡的音樂不進 git（版權），各自放。
- 只播前 60 秒，挑一開始就夠大聲的段落效果最好。

`wakeup.py --now` 這個單次測試工具仍然固定使用 `ALARM_FILE`。
