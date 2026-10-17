# 設定步驟

程式已經寫好了，剩下三件事：建 LINE 官方帳號、開通對外網址、啟動服務。

---

## 1. 建立 LINE Messaging API channel

1. 到 https://developers.line.biz/console/ 用你的 LINE 帳號登入
2. **Create a new provider**（隨便取名，例如 `felix-home`）
3. 在該 provider 下建立 **Messaging API channel**
   - 現在的流程會先導到 **LINE Official Account Manager** 建官方帳號
   - 名稱例如「新竹附醫看診進度」，類別選醫療／健康
   - **地區一定要選台灣** — 這決定了免費方案是每月 200 則推播，且建立後不能改
4. **Basic settings** 分頁 → 複製 **Channel secret**
5. **Messaging API** 分頁 → **Channel access token (long-lived)** → Issue → 複製
   - 不要用 v2.1 的 token 流程，那需要 RS256 簽章，標準函式庫做不到
6. 同一頁把這些打開／關掉：
   - **Use webhook**：ON
   - **Webhook redelivery**：**ON** ← 這個很重要，原因見下面第 3 節
   - **Error statistics aggregation**：ON
   - **Allow bot to join group chats**：OFF
7. 到 LINE Official Account Manager 的「回應設定」：
   - **自動回應訊息**：停用（不然 LINE 自己的罐頭回覆會蓋掉 bot）
   - **加入好友的歡迎訊息**：停用或自訂
   - **聊天**：關閉人工聊天模式

把 secret 和 token 填進 `config.json`：

```json
{
  "channelSecret": "貼上 Channel secret",
  "channelAccessToken": "貼上 access token",
  "ownerUserId": "",
  "allowedUserIds": [],
  "dryRunLine": false
}
```

---

## 2. 開通對外網址

這台機器已經在跑 Tailscale Funnel（`/` 給 stock-dashboard 用），把 bot 掛在子路徑就不會互相影響：

```bash
tailscale funnel --bg --set-path=/cmu 8788
tailscale funnel status          # 應該同時看到 /  和  /cmu
```

Webhook URL 填回 LINE Console：

```
https://<your-host>.ts.net/cmu/callback
```

填完按 **Verify**，要回 200。

> **確認 Tailscale 有沒有吃掉路徑前綴**（目前未驗證，兩種情況程式都能處理）：
> ```bash
> curl -s https://<your-host>.ts.net/cmu/healthz
> ```
> 回應裡的 `rawPath` 會告訴你後端實際收到什麼。順便確認 `curl https://<your-host>.ts.net/` 的 stock-dashboard 沒被影響。

---

## 3. 為什麼一定要開 webhook redelivery

這台是 WiFi 連線，每天會重新連線 5–8 次。既有的 `funnel-watchdog.timer` 會在 Funnel 真的壞掉時做 `tailscale down && up` 修復，期間對外中斷約 50–120 秒。中斷時 LINE 送來的 webhook 如果沒開 redelivery 就永久遺失了——使用者會覺得 bot 已讀不回。

---

## 4. 啟動

**服務已經幫你設好並啟用了**，改完 `config.json` 之後重啟讓它讀新設定就好：

```bash
systemctl --user restart cmu-linebot.service
journalctl --user -u cmu-linebot -f     # Ctrl-C 離開
```

（`cmu-linebot.service` 和 `cmu-directory.timer` 都已經 enable，開機會自動起來。）

第一次要先把醫師目錄建起來（讓使用者能用醫師姓名查診間）。**這個指令必須在專案目錄下跑**：

```bash
cd ~/CMU_search
python3 -m bot.directory --full     # 約 560 個請求，1.2 秒間隔，十幾分鐘
```

之後每天早上 06:00 的 timer 只會掃白名單，快很多。

---

## 5. 加自己和家人

1. 用手機掃 Messaging API 分頁上的 QR code 加好友
2. 傳任何一句話給它 — 會收到「這是私人使用的服務」的拒絕（正常，還沒授權）
3. 在 server 上：

```bash
cd ~/CMU_search
python3 -m bot.admin pending          # 看到你的 LINE userId
python3 -m bot.admin allow U1234...   # 放行
```

每個家人重複一次。也可以直接把 userId 填進 `config.json` 的 `allowedUserIds`。

**這個白名單是刻意的**：陌生人拿到 bot 的 ID 就能燒掉每月 200 則推播額度，或者拿它當工具去試探別人的身分證號。未授權的人得到一句拒絕，碰不到醫院系統。

---

## 6. 用法

| 你傳 | Bot 回 |
|---|---|
| `115` | 該診進度（自動判斷時段）|
| `115 下午` | 指定時段 |
| `115 下午 38` | 直接設定叫號提醒 |
| `張巨成` | 該醫師今天的診間 |
| `A123456789 0304` | 用證號查你在哪一診（證號用完即丟，僅中國醫新竹）|
| `中國醫新竹` / `換醫院` | 切換醫院（之後的查詢都用這家）|
| `回診 11/20 上午 115` | 回診前一天晚上 7 點提醒；當天第一次傳訊息時也會提醒 |
| `領藥 10/17 28天 3次` | 慢性處方箋：可領下一次的第一天早上 9 點提醒，期限前 3 天再提醒一次 |
| `已領` | 記下這次領藥，排好下一次 |
| `我的提醒` | 看全部提醒，按鈕可個別取消 |
| `取消` | 取消今天的叫號提醒 |
| `刪除我的資料` | 刪除自己的提醒和查詢紀錄（會再確認一次）|
| `說明` | 用法 |

查詢用的是 LINE 的「回覆訊息」，**不計入每月 200 則額度**，可以隨便查。額度花在：

- 叫號提醒：每次看診最多 2 則
- 回診提醒：每次 1 則
- 領藥提醒：每次領藥 1–2 則，最後一次另有 1 則「記得回診」

當月剩不到 `reminderReserve`（預設 20）則時，回診和領藥提醒就會暫停，把剩下的額度留給叫號提醒。用 `python3 -m bot.admin quota` 可以看用量。

等待時間（「大約再 15–25 分鐘」）是從最近 40 分鐘的叫號速度算出來的。資料不夠時就不顯示，只顯示人數。

---

## 7. 圖文選單

選單是 3×2 的大格子：查進度、我的提醒、回診、領藥、換醫院、說明。重畫圖片需要 Pillow 和字型，這兩樣不在 repo 裡：

```bash
pip install --target tools/pylibs pillow
# 從 Google Fonts 下載 Noto Sans TC，把 Bold 存成 tools/NotoSansTC-Bold.otf
```

改了圖或格子之後：

```bash
cd ~/CMU_search
PYTHONPATH=tools/pylibs python3 tools/make_richmenu.py   # 重畫 tools/richmenu.png
python3 tools/setup_richmenu.py                           # 上傳並設為預設（會取代舊選單）
```

---

## 8. 新增一家醫院

詳細步驟見 [docs/hospitals.md](docs/hospitals.md)。簡單說：

1. 在門診時間調查那家醫院的看診進度頁，把結果記到 `docs/hospitals.md`，並把原始回應的**位元組**存到 `fixtures/<醫院 id>/`
2. 寫 `bot/hospitals/<醫院 id>.py`（繼承 `Adapter`），在 `bot/hospitals/__init__.py` 註冊
3. 補 fixture 測試，跑 `python3 -m unittest discover -s bot -t .`
4. 在 `config.json` 加上 `"enabledHospitals": ["cmu_hch", "<醫院 id>"]`，然後重啟服務

醫師目錄會自動對每家啟用的醫院各掃一次：`python3 -m bot.directory --full <醫院 id>`。
