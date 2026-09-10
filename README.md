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
| 背景排程 | 定期同步新集數與證券主檔（`app/services/background_updates.py`） |

資料流程：`whatmkreallysaid.com 整包 / vocus.cc 免費集數 / SoundOn RSS + 本機 Whisper` → `episodes` 資料表 → 逐字稿切段（市場分析／Q&A）→ LLM 分析 → `episodes.tickers` → 彙整成 `company_insights` → Dashboard，並同步推送到 [X Stock Tracker 的 `x_company_views`](#與-x-stock-tracker-的整合)。

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

非機密設定（來源網址、Whisper 開關、台股別名清單、公司論點分析開關）在 `config/settings.yaml`，可直接編輯，不需要改程式碼。

## 初始化資料庫與首次同步

```powershell
python -m scripts.init_db
python -m scripts.sync_taiwan_securities
python -m scripts.sync_gooaye
```

`sync_taiwan_securities` 會從 TWSE 與 TPEx 官方 OpenAPI 抓上市／上櫃證券主檔（免金鑰）。有了這張表，股癌講到文曄、京元電這類不在種子清單裡的公司才有辦法還原成證券代號，而不是以原始中文字串存進資料庫。之後想更新掛牌清單時再跑一次即可。

首次同步預設只分析最新 20 集（`config/settings.yaml` 的 `sync.incremental_new_limit`），避免第一次就把近 700 集全部送進 LLM。若要把全部歷史集數都跑過分析（會呼叫 LLM 數百次，耗時較久）：

```powershell
python -m scripts.backfill_transcripts
```

### 重建公司觀點

改了 `config/settings.yaml` 的別名清單、或剛更新完台股證券主檔之後，不需要重跑逐字稿分析，只要重建公司觀點：

```powershell
python -m scripts.rebuild_company_insights
```

這會依 `analysis.company_analysis_batch_size` 分批呼叫 LLM，替每家公司寫出核心論點與優劣勢。只想更新機械彙整（立場、重要度、市場分類、提及次數、原文連結）而完全不呼叫模型時：

```powershell
python -m scripts.rebuild_company_insights --no-llm
python -m scripts.rebuild_company_insights --symbols 2330.TW NVDA
```

### 從舊版本升級

`init_db()` 只會建立不存在的資料表，不會替既有資料表補欄位。若資料庫是在 `company_insights.market` 這個欄位出現之前建立的，先跑一次遷移（可重複執行）：

```powershell
python -m scripts.migrate_company_market
```

這支腳本刻意只做兩件事：新增欄位、逐列填入 `market`。它**不會**動到 thesis、優劣勢、立場或提及次數，也不會刪任何公司 — 在分段資料表出現之前建立的資料庫，每一家公司都沒有對應的分段，若讓它跑完整 rebuild 就會被整批清掉。

想重新產生論點與優劣勢時，另外執行負責這件事的指令：

```powershell
python -m scripts.rebuild_company_insights
```

### 讓舊資料的代號跟上主檔

在同步台股證券主檔（或新增別名）之前分析過的集數，資料庫裡存的是模型當時給的字串，例如「文曄」而不是 `3036.TW`。公司觀點頁在讀取時就會歸併，所以那一頁不受影響；但集數卡上的代號標籤仍會顯示舊字串。要讓兩邊一致：

```powershell
python -m scripts.backfill_segment_tickers --dry-run
python -m scripts.backfill_segment_tickers
```

先用 `--dry-run` 看會改幾筆。這支腳本只做查表改寫，不呼叫模型，可重複執行。

## 執行網頁 Dashboard

```powershell
uvicorn app.main:app --reload --port 8100
```

開啟 http://localhost:8100 ，畫面分「最新集數」「公司觀點」「集數總覽」三個分頁。

- 「公司觀點」可依台股／美股／其他市場／待確認篩選（篩選鈕上的數字是各分類的公司數），每家公司顯示核心論點與優勢／劣勢兩欄，並可展開「分段」看逐段依據。列上的「重要度」是提及該公司的片段平均重要度，**不是**對立場的把握程度。
- 「集數總覽」可展開每集完整逐字稿，並在有 Whisper 比對紀錄時顯示相似度說明。

之後要抓新集數，重新執行：

```powershell
python -m scripts.sync_gooaye
```

## 背景排程

Dashboard 啟動後會自動跑兩個背景工作，不必自己排 Windows 工作排程器，也不必手動打 `POST /api/sync`：

| 工作 | 預設週期 | 做的事 |
| --- | --- | --- |
| `episode_sync` | 6 小時 | 跑一次完整同步（抓逐字稿站／RSS → 分析新集數 → 更新公司觀點 → 推送到 X Stock Tracker） |
| `taiwan_securities` | 24 小時 | 更新 TWSE／TPEx 證券主檔，然後**不呼叫 LLM**重建一次公司觀點，把先前存成「文曄」的提及歸戶成 `3036.TW` |

週期與開關都在 `config/settings.yaml` 的 `schedules`；`schedules.enabled: false` 就完全回到手動模式。`episode_sync_hours` 有 1 小時的下限——逐字稿站與 RSS 都是別人的網站，設定打錯不該變成連續下載迴圈。

相關 API：

```
GET  /api/schedules/status          # 兩個工作的狀態、上次結果、下次執行時間
POST /api/schedules/episodes/run-now  # 立刻跑一次（已在執行中會回 already_running，不會重複跑）
```

## 與 X Stock Tracker 的整合

X Stock Tracker（`E:\X_stock_tracker`）從 Alva @zet 的 X 貼文彙整公司觀點，Gooaye 從股癌逐字稿彙整公司觀點——同樣的問題、不同的證據。設定好之後，**Gooaye 每次更新公司觀點，就會把同一批公司寫進 X Stock Tracker 的 `x_company_views`**，那邊的「公司觀點」分頁就能把兩種看法並排比較。

兩邊的資料是**並存而不是合併**的：`x_company_views` 有一個 `source` 欄位（`x` ／ `gooaye`），唯一鍵是 `(source, symbol)`，所以 `2330.TW` 兩邊各一列，各自保有自己的立場、論點與出處連結。這個欄位同時也是互相保護的機制：

- X Stock Tracker 全量重建時只刪 `source='x'` 的列，碰不到 Gooaye 的資料。
- 本專案（`app/services/xstock_push.py`）只寫、只刪 `source='gooaye'` 的列，碰不到 X 的資料。
- 資料庫帳號只被授權讀寫 `x_company_views` 這一張表；`x_articles`（X Stock Tracker 的原始貼文）依然完全沒有權限。

### 設定步驟

1. 在 `.env` 設定 `XSTOCKTRACKER_DB_URL` 指向 X Stock Tracker 的資料庫。
2. 到 `E:\X_stock_tracker` 執行一次遷移，它會加上 `source` 欄位、把唯一鍵換成 `(source, symbol)`，並授權本專案的資料庫角色寫入 `x_company_views`：

   ```powershell
   python -m scripts.migrate_company_view_source
   # 角色名稱不是 gooaye 時：--writer-role 你的角色
   # DSN 不是 owner／superuser 時：--no-grant，再請 DBA 執行它印出來的 GRANT
   ```

3. 回到 `F:\Gooaye` 做一次全量推送（之後同步流程會自動增量推送）：

   ```powershell
   python -m scripts.push_to_xstocktracker
   python -m scripts.push_to_xstocktracker --symbols 2330.TW NVDA   # 只推特定幾家
   ```

推送寫進去的欄位對應：`thesis` 會加上 `［股癌 Gooaye · <市場> · 提及 N 集］` 開頭（X 的卡片跟自家 X 貼文列並排顯示，沒有這行會看起來像某則推文說的；`x_company_views` 也沒有 market 欄位，順便把 Gooaye 判定好的市場帶過去），`mention_count` 是提及的集數，`evidence_urls` 是逐字稿站的集數連結。

不想推送時，把 `config/settings.yaml` 的 `xstocktracker.push_company_insights` 設成 `false`——證券主檔的唯讀查詢仍然照常運作。沒設定 `XSTOCKTRACKER_DB_URL` 也會自動略過。

**推送永遠不會讓同步失敗。** 對方資料庫連不上、還沒遷移、或權限不足時，只會在結果裡回一個 `error` 並寫進 log；Gooaye 自己的集數與公司觀點早就已經寫進自己的資料庫了。下一次背景排程若發現沒有新集數，會自動改跑一次全量推送，把先前失敗的更新補上。

也可以手動觸發：

```
POST /api/xstocktracker/push                  # 全量推送（並清掉已失效的 gooaye 列）
POST /api/xstocktracker/push?symbols=2330.TW  # 只推特定幾家
```

## 測試

```powershell
python -m pytest
```

測試不需要網路、不需要 PostgreSQL，也不會呼叫任何 LLM（模型呼叫一律以 stub 取代），涵蓋：逐字稿切段（`chunking.py`）、Whisper 與正式逐字稿的取捨與比對分數（`sync.py` 的 `_apply_transcript`）、公司別名還原與市場判定（`entity_resolver.py`）、TWSE／TPEx 主檔解析與別名歧義處理（`taiwan_securities.py`）、公司論點分析與其失敗退回機制（`company_insights.py`）、推送到 X Stock Tracker 的來源隔離與失敗容錯（`xstock_push.py`）、背景排程的間隔下限與重複執行防護（`background_updates.py`）。

`tests/test_xstock_push_live.py` 是唯一會碰到真實資料庫的檔案：它只在 `XSTOCKTRACKER_DB_URL` 有設定且連得上時才執行，全部包在最後一定會 rollback 的交易裡（不會留下任何資料），用來驗證那些純單元測試看不到的東西——授權是否真的到位、`(source, symbol)` 唯一鍵是否真的允許同一檔股票兩邊各一列、以及 `x_articles` 是否仍然沒有權限。沒設定就整個跳過。

收集範圍與暫存目錄設定在 `pytest.ini`：只收 `tests/`（根目錄的 `test_output/` 是歷次 QA 產出的檔案，一起收集會在匯入階段就中斷整輪測試），並把 `tmp_path` 指向專案內的 `.pytest_tmp/`。後者是為了繞過系統暫存區裡權限已經壞掉、非管理員無法讀取或刪除的 `pytest-of-<使用者>` 目錄；若之後用系統管理員身分把該目錄刪掉，就可以移除 `pytest.ini` 裡的 `--basetemp` 設定。

## 已知範圍限制

- **vocus.cc**：只讀取沙龍首頁第一頁列出的免費文章，不翻頁、不登入、不繞過付費牆。只有頁面資料明確標示 `isPay=false` 的文章會被採用；標示為付費、值為 null 或根本沒有這個欄位時一律跳過 — 無法確認是免費的就當作付費處理。
- **Whisper 比對**：只用來補「逐字稿站還沒收錄的最新 1～3 集」空窗，**不會**對已有逐字稿的數百集歷史全部重跑語音辨識做比對（下載＋轉譯全部歷史音檔耗時耗空間，效益也低）。
- **分析失敗會自動重試**：模型呼叫失敗、或回傳內容完全無法使用時，該集不會被標記為已分析，也不會計入 `episodes_analyzed`，而是留在 `pending_analysis_backlog` 等下一次同步重跑。逐字稿內容變動（例如 Whisper 版本被正式逐字稿取代）同樣會讓該集重新排入分析佇列，即使變動當下因為每輪上限而沒輪到它。
- **台股代號還原**：依序查三層 — `config/settings.yaml` 的手動種子清單（俗稱、英文簡稱、非台股公司）、TWSE／TPEx 官方證券主檔、選用的 `XSTOCKTRACKER_DB_URL` 多市場主檔。台股主檔排在多市場主檔之前，因為股癌是台灣節目，四位數字代號絕大多數是台灣掛牌，而多市場主檔會把台／日共用的四位數代號當成歧義直接丟掉。跑 `scripts.sync_taiwan_securities` 之前，只有種子清單生效。公司分組是在**讀取時**重新解析代號的，所以補完主檔或改完別名後只要重建公司觀點就會生效，不需要先改資料。

- **舊名稱的公司列會自動併掉**：主檔補上「文曄」對應之後，同一家公司會先出現兩列（舊的「文曄」與新的 `3036.TW`）。不論是完整重建，或是同步時只針對受影響公司的局部重建，都會把解析後指向同一家公司的舊列刪除、併成一列。局部重建只處理這次範圍內的公司，範圍外那些沒有分段的舊列不會被順手刪掉 — 那是完整重建的職責。
- **主檔同步是全有全無**：`sync_taiwan_securities` 會先把整張表標記為停用再寫回當次結果，因此上市與上櫃兩個來源會各自檢查；只要任何一邊沒有回傳有效資料就整批中止並保留舊主檔。半套的主檔會讓某個交易所的公司全部無法解析，而且表面上看起來是成功的。
- **公司名稱**：先用 TWSE／TPEx 主檔的公司簡稱，再讓 `config/settings.yaml` 的種子清單覆寫。主檔還沒同步、種子清單也沒有的公司會直接顯示代號。
- **市場分類**：由代號後綴、主檔與種子清單判定，**不交給 LLM 猜**。純英文名稱不再僅憑長度推定美股（例如 Steam）；主檔或種子未確認的一律留在「待確認」。需要覆寫時在 `config/settings.yaml` 的別名項目加上 `market:`。
- **立場與論點的分工**：立場（多／空／中立）由逐段分析的多數決決定，看的是該公司的**每一個**片段；論點與優劣勢才交給 LLM，且只看重要度最高的幾段（`analysis.company_analysis_segment_limit`）。因此 LLM 失敗或以 `--no-llm` 跳過時，卡片會退回成逐段摘要拼接、優劣勢留空，而不是變成空白。畫面上的「目前沒有可列出的優勢或劣勢」是中性描述：模型本來就被要求「沒有依據就回空陣列」，所以空白不代表分析失敗。分析到底有沒有跑完，看同步或重建指令回報的 `narratives_completed` / `narratives_attempted`。
- **重要度不等於信心**：`company_insights.confidence` 存的是片段平均重要度。真正的「立場一致度」（例如 9 段看多 1 段看空 vs 5:5）需要另外開欄位，目前尚未實作。

### 10 分鐘實測後的分析契約修正

B.ai 不使用嚴格 `response_format` 時，提示仍會包含完整 JSON schema；回覆缺必要欄位或型別錯誤會重試，不能再把缺少 `companies` 的 JSON 當成功。
公司重建結果的 `narrative_failures` 會列出失敗公司的代號、錯誤類型，以及供應商回傳的 HTTP 狀態與錯誤代碼（若有）。供應商拒絕時保留逐段摘要 fallback，不把該公司計入 `narratives_completed`。

新分段分析會對每家公司產生獨立的立場與理由，存入 `segment_company_stances`。公司頁與分段展開使用這些個別立場，彙整仍是機械多數決，平票顯示中立；公司級 LLM 只產生文字，不修改立場或平均重要度。缺少某家個別立場時新資料採中立。

此為新增資料表，`python -m scripts.init_db` 或服務啟動的 `init_db()` 會建立，不需 ALTER 舊分段欄位。既有分段沒有個別立場時仍讀取舊資料，不會憑空推算；要修正歷史立場，請明確選取集數重新分析（會呼叫 LLM）：

```powershell
python -m scripts.reanalyze_episode_segments --episodes 694
```

這只使用資料庫中已存的逐字稿，不重新下載或轉錄。只執行公司 rebuild 不會生成缺少的逐段公司立場。市場判定收緊後，未收錄在種子或多市場主檔的有效美股也可能留在待確認，需補主檔或種子。
