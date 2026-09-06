# Gooaye Tracker

以《股癌》（Gooaye，主持人謝孟恭）Podcast 內容為訊號來源，整理台股相關的公司提及與立場，做法比照 `F:\Serenity Stock Tracker`（美股／Serenity 訊號）的架構，但這是完全獨立的專案：**不與該專案共用任何程式碼或資料庫**，只是設計理念相同（本機 LLM 分析、本機語音辨識、不外送資料）。

本專案不是股癌或謝孟恭官方產品，內容僅供研究與資訊整理，不構成投資建議。

## 資料來源與範圍

股癌沒有官方逐字稿，本專案整合三個來源：

1. **whatmkreallysaid.com**（非官方 AI 聽寫逐字稿站，主要來源）
   實測確認其首頁載入一個公開、無需登入、`Access-Control-Allow-Origin: *` 的整包 JSON（brotli 壓縮），目前含 690+ 集完整逐字稿。這是最主要、可整批同步的來源，但通常會落後最新 1～3 集。
2. **SoundOn RSS**（`https://feeds.soundon.fm/podcasts/954689a5-...xml`，股癌真正的音檔來源）
   Apple Podcasts／Spotify 背後其實都是這個公開 RSS。用來判斷「目前實際存在哪些集數」與取得每集的 mp3 網址，並在逐字稿站還沒收錄最新集數時，作為本機語音辨識的音檔來源。
3. **vocus.cc 逐字稿沙龍**（付費訂閱制，選用、預設關閉）
   只讀取「未登入即可見的免費文章」（透過頁面內嵌的 `__NEXT_DATA__` 判斷 `isPay=false`），**不會**做付費牆繞過或帳密登入爬取。實測發現部分最新集數會提前在這裡免費公開，甚至比逐字稿站更新更快。

當上述兩個文字來源都還沒收錄某一集時，本機用 **faster-whisper** 直接下載該集 mp3、在本機轉出逐字稿（不留存音檔、不外送），先讓資料「有得看」；之後正式逐字稿到位時，會自動計算與 Whisper 版本的文字相似度，寫入 `comparison_notes`／`comparison_score`，這就是「音檔 vs 逐字稿」的比對紀錄。

## 系統架構

| 元件 | 用途 |
| --- | --- |
| FastAPI | API 與 Dashboard 靜態檔案（`app/main.py`） |
| PostgreSQL | 集數逐字稿與公司觀點（獨立新資料庫，不與 Serenity Stock Tracker 共用） |
| Ollama（預設）或相容 OpenAI API | LLM 分析逐字稿片段，抽取公司提及與立場 |
| faster-whisper + ffmpeg | 本機語音轉文字（僅用於補逐字稿站的最新集數空窗） |

資料流程：`whatmkreallysaid.com 整包 / vocus.cc 免費集數 / SoundOn RSS + 本機 Whisper` → `episodes` 資料表 → 逐字稿切段（市場分析／Q&A）→ LLM 分析 → `episodes.tickers` → 彙整成 `company_insights` → Dashboard。

## 安裝

```powershell
cd F:\Gooaye
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

**faster-whisper 需要 `ffmpeg`**（解碼下載下來的 mp3）：

```powershell
winget install --id Gyan.FFmpeg
```

安裝後重開一次終端機，確認 `ffmpeg -version` 有輸出。

Whisper 會在獨立的 worker 子程序中執行；Windows 工作排程器即使遇到
CTranslate2／Intel runtime 的 native abort，也只會讓該集 Whisper 失敗，
不會中止同一批同步的逐字稿寫入與 LLM 分析。

### 五分鐘 Whisper 驗證

若要只測試一集音檔的前五分鐘，避免執行完整集數的長時間轉譯：

```powershell
python -m scripts.test_whisper_5min --episode 694
```

結果會放在 `test_output\whisper_5min\EP694`。預設保留
`episode.json`、`transcript.txt`、`result.json` 與 `test.log`，並在結束時刪除
下載的原始音檔與五分鐘切片。除錯時可加上 `--keep-audio` 保留音檔。

## 設定

```powershell
Copy-Item .env.example .env
```

編輯 `.env`：

- `POSTGRES_DSN`：指向**新建立**的資料庫（例如 `gooaye_tracker`），不要跟 Serenity Stock Tracker 共用同一個 database。若電腦上已有本機 PostgreSQL 服務（Serenity Stock Tracker 也用同一套服務），只需要另外建一個新 database：
  ```powershell
  psql -U postgres -c "CREATE DATABASE gooaye_tracker;"
  ```
- `LLM_PROVIDER=ollama`（預設，完全本機推論）時，需要本機已安裝 Ollama 並 pull 好模型：
  ```powershell
  ollama pull qwen3:30b
  ```
  若電腦資源有限，可在 `.env` 換成較小的模型，但公司抽取品質可能下降。
- `LLM_PROVIDER=bai` 時，用 `BAI_API_URL`／`BAI_API_KEY`／`BAI_API_MODEL` 呼叫雲端服務；若已經在跑 Serenity Stock Tracker，可以沿用同一組 BAI 金鑰（兩個專案各自獨立呼叫，不共用程式或資料庫，只是共用同一組帳號額度）。BAI／GLM 系列模型對嚴格 JSON schema 的支援不穩定，此路徑改用「先照 prompt 要求輸出 JSON、格式不對就重試一次」的方式，而不強制 `response_format`。
- 若改用 `LLM_PROVIDER=api`，需另外填 `LLM_API_URL`／`LLM_API_KEY`／`LLM_API_MODEL`（任何其他相容 OpenAI Chat Completions 格式的服務，會強制要求 JSON schema）。

非機密設定（來源網址、Whisper 開關、台股別名清單）在 `config/settings.yaml`，可直接編輯，不需要改程式碼。

## 初始化資料庫與首次同步

```powershell
python -m scripts.init_db
python -m scripts.sync_gooaye
```

首次同步預設只分析最新 20 集（`config/settings.yaml` 的 `sync.incremental_new_limit`），避免第一次就把近 700 集全部送進 LLM。若要把全部歷史集數都跑過分析（會呼叫 LLM 數百次，耗時較久）：

```powershell
python -m scripts.backfill_transcripts
```

## 執行網頁 Dashboard

```powershell
uvicorn app.main:app --reload --port 8100
```

開啟 http://localhost:8100 ，畫面分「最新集數」「公司觀點」「集數總覽」三個分頁；「集數總覽」可展開每集完整逐字稿，並在有 Whisper 比對紀錄時顯示相似度說明。

之後要抓新集數，重新執行：

```powershell
python -m scripts.sync_gooaye
```

（可以自行用 Windows工作排程器排定週期執行；本專案本身不像 Serenity Stock Tracker 那樣內建常駐排程器。）

## 測試

```powershell
python -m pytest
```

測試只涵蓋不需要網路／資料庫的純函式邏輯：逐字稿切段（`chunking.py`）、Whisper 與正式逐字稿的取捨與比對分數（`sync.py` 的 `_apply_transcript`）、公司別名還原（`entity_resolver.py`）。

## 已知範圍限制

- **vocus.cc**：只讀取沙龍首頁第一頁列出的免費文章，不翻頁、不登入、不繞過付費牆。付費集數完全不處理。
- **Whisper 比對**：只用來補「逐字稿站還沒收錄的最新 1～3 集」空窗，**不會**對已有逐字稿的數百集歷史全部重跑語音辨識做比對（下載＋轉譯全部歷史音檔耗時耗空間，效益也低）。
- **台股代號還原**：目前用 `config/settings.yaml` 裡手動維護的公司別名種子清單（`company_aliases`），不是完整的 TWSE／TPEx 證券主檔。之後若要更準，可以比照 Serenity Stock Tracker 的做法，另外寫一個同步 TWSE／TPEx OpenAPI 的模組來源。
- **公司觀點的 thesis**：目前是直接取最重要的幾集摘要拼接，不像 Serenity Stock Tracker 那樣另外呼叫一次 LLM 生成客製化優劣勢；效果比較陽春但省一次 LLM 呼叫。
