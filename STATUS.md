# PC-MEF Research System — 進度真相

本檔是進度與交接的唯一真相來源。聊天訊息裡的說明不算完成。
刻意不另開 HANDOFF 檔：兩份文件必然漂移，屆時沒人知道該信哪一份。

最後更新：2026-08-30

---

## 接手指南（HANDOFF）

冷啟動接手時先看這一節，五分鐘內能跑起來。

### 環境

**方式一：Docker（推薦給交接的人）** —— 不必自己處理 LLVM-C.dll 那串坑。

```powershell
cp .env.example .env      # 填 PCMEF_ADMIN_TOKEN 與 PCMEF_SECRET_MASTER_KEY
docker compose build
docker compose run --rm pcmef version          # 一次性 CLI
docker compose run --rm --entrypoint python pcmef -m pytest -q
docker compose up console                      # http://127.0.0.1:8787
```

已實測：容器內 **1101 passed**、`sim smoke` 算得出 transient，
Empty 場景總能量 **3908.9**，與本機 Batch 5 記錄的 3909 一致 —— 容器重現本機數字。
manifest 會如實記下容器環境（`Linux-…-WSL2`）與釘死的模擬器版本。

| 注意 | 說明 |
|---|---|
| `data/` 掛成唯讀 | §38 要求 raw real 只讀備份；掛 `:ro` 讓誤覆寫在檔案系統層就不可能 |
| 模擬器版本釘死 | Dockerfile 內釘 mitsuba 3.8.0 / drjit 1.3.1 / mitransient 1.3.0，與本機相同；不釘的話容器會裝到 3.9.1，結果不可比 |
| console 需要 token | 容器內綁 0.0.0.0 才收得到轉發，§46 因此要求 admin token；缺了會被 `assert_network_policy()` 擋下 |
| 埠可換 | `PCMEF_CONSOLE_PORT=8791 docker compose up console` |

**方式二：本機**

```powershell
py -3.10 -m pip install -e ".[dev,simulation,admin,agents]"
winget install --id LLVM.LLVM          # drjit 的 LLVM 後端需要
$env:PATH = "C:\Program Files\LLVM\bin;$env:PATH"
```

**四個不查會踩到的坑：**

0. **本機跑 pytest 不會印結尾摘要行。** 跑完只看得到最後一行 `[100%]`，
   `1302 passed in Xs` 那行**不會出現** —— NOTE-012 的 drjit DLL-detach
   在 teardown 把它吃掉了。**判斷成敗一律看 exit code**（`$?` / `$LASTEXITCODE`），
   不要去找摘要行，也不要因為找不到就以為跑掛了。
1. **一律用 `py -3.10`。** PATH 上的 `python` 是 3.12 且沒裝相依。
2. **`LLVM-C.dll` 必須存在且在 PATH 上。** drjit 執行期動態載入它，
   但套件**不內含**（只有 `drjit-core.dll`）。缺了會看到
   `jitc_llvm_init(): LLVM API initialization failed`，
   且 `llvm_ad_rgb` variant 無法使用。
3. **本機無 NVIDIA GPU**（Intel Iris Xe），`cuda_ad_rgb` 永遠不可用，
   固定使用 `llvm_ad_rgb`。

### 常用指令

```powershell
$env:PATH = "C:\Program Files\LLVM\bin;$env:PATH"    # 少了會有一批模擬測試靜默 skip
$env:PCMEF_REQUIRE_SIMULATION = "1"                  # 讓「缺相依」變成失敗而不是 skip
py -3.10 -m pytest                                   # 全部測試（1681 條，約 3.5 分鐘）
py -3.10 -m pcmef.cli config check                   # 待教授裁決的 10 項
py -3.10 -m pcmef.cli params audit                   # 38 個參數的 formal 防線（exit 2 = 不可進 formal）
py -3.10 -m pcmef.cli locks status                   # 22 個 formal lock 的狀態
py -3.10 -m pcmef.cli sim smoke                      # M1 模擬 smoke
py -3.10 -m pcmef.cli surrogate smoke                # E1-G04 四特徵無 NaN 驗證
py -3.10 -m pcmef.cli split plan-real --source data/raw_real/edge_impulse_export
py -3.10 -m pcmef.cli provenance resolve-sigma `
    --paired-source data/raw_real/edge_impulse_export --export-decimals 4
py -3.10 -m pcmef.cli audit e1-gates                 # Batch 7：十二個 gate
py -3.10 -m pcmef.cli audit heldout-firewall         # Appendix B 洩漏防線
py -3.10 -m pcmef.cli erratum status                 # 勘誤層：重驗 lock/erratum/evidence
py -3.10 -m pcmef.cli locks resolve --lock initial_simulation   # formal 讀取入口
py -3.10 -m pcmef.cli calibration preregister --validate        # CP-01..CP-16
py -3.10 -m pcmef.cli calibration stage0 --out outputs/calibration/stage_0
                                                     # stage 0 可辨識性探測（純模擬）
py -3.10 -m pcmef.cli audit real-split-policy        # Appendix H1 政策契約
py -3.10 -m pytest tests/unit/test_amendments.py     # 協定修訂記錄（AMD-001）
py -3.10 -m pcmef.cli audit real-data --source data/raw_real/... --out data/inventory
```

LLM Admin（Part VI，全部只寫 draft registry）：

```powershell
py -3.10 -m pcmef.cli llm connection add --provider google --name "Gemini Formal" `
    --secret-ref env:GEMINI_API_KEY          # 只收參考，不收 key 明文（NOTE-019）
py -3.10 -m pcmef.cli llm connection fetch-models --connection <id>
py -3.10 -m pcmef.cli llm connection select-model --connection <id> --model <model_id>
py -3.10 -m pcmef.cli llm connection test --connection <id>   # 三項 probe 全過才算
py -3.10 -m pcmef.cli llm connection lock --connection <id>   # 鎖定後才能綁定
py -3.10 -m pcmef.cli llm binding set arbitration_agent --connection <id> --model <model_id>
py -3.10 -m pcmef.cli llm binding lock arbitration_agent      # draft 層確認鎖
py -3.10 -m pcmef.cli llm binding audit
py -3.10 -m pcmef.cli llm snapshot                   # 算 candidate hash 並列出未達前提
py -3.10 -m pcmef.cli llm snapshot --freeze          # 前提齊備才寫 lock，否則 exit 2
py -3.10 -m pcmef.cli llm cache audit --formal
py -3.10 -m pcmef.cli admin serve                    # 127.0.0.1:8787/admin/llm-setup
```

**無憑證也能把整條流程走完**（供交接驗證）：

```powershell
$env:PCMEF_STUB_MODELS = "m1:chat+vision+structured_json,m2:chat"
$env:MY_FAKE = "sk-notARealKey000000000"
py -3.10 -m pcmef.cli llm connection add --provider stub_offline --name Demo --secret-ref env:MY_FAKE
```

`stub_offline` 刻意**不在** `FORMAL_ELIGIBLE_PROVIDERS` 內：流程走得完，
但 snapshot 會拒絕把它凍進 formal identity（NOTE-017）。

### 目前卡在哪

| 阻塞 | 影響 | 解法 |
|---|---|---|
| ~~真實 ToF 只剩窗口 Mean/Std~~ | — | **已解除**：Edge Impulse export 復原 560 筆 500×4 |
| ~~Sigma register 未定~~ | — | **已解出 0x18**（排除檢驗，NOTE-010）；採集腳本仍未取得，取得後須複核 |
| ~~Real split 三值未裁決~~ | — | **已裁決並凍結** 392/168（NOTE-014） |
| ~~E1/E2 bootstrap 四值~~ | — | **已核定** 10000 / 20260826 / 20260827（NOTE-015） |
| ~~Sigma register 未定阻擋 E1-G08~~ | — | **已由 AMD-001 拆解**：channel/scale 為 CONFIRMED，位址獨立為 CONFLICT 且不再擋 gate（NOTE-028） |
| 10 項數值未核定 | 全部 formal run | `config check` 有完整清單與出處；其中 `gate.*`、`e2.final_n_per_class` 須由搜尋或 pilot 產出，不是「請教授給數字」 |
| **26 個參數未解決** | 全部 formal run | `params audit`（exit 2）有完整清單。**防線已上線**（NOTE-030），不再需要人工數。**校準協定已預註冊凍結**（NOTE-041），可以開始跑 |
| ~~CG-3 / Ambient 觀測量定義錯誤~~ | — | **已解除**：Ambient 改為獨立 ambient pass（NOTE-034） |
| ~~M2 場景保真度~~ | — | **已解除**：`initial_simulation.lock` 已凍結（NOTE-039） |
| ~~lock 的 environment 記錯~~ | — | **已解除**：ERR-001 勘誤層（NOTE-040） |
| ~~沒有校準目標規格~~ | — | **已解除**：CAL-PREREG-001 已凍結（NOTE-041） |

**E1 已完成（2026-08-30）。** 治理模式已由 AMD-005 改為 thesis-oriented
protected-final-test：heldout 168 筆在讀值之前切成 probe 56 / final 112，
校準守衛破線改為 `UPDATE_INHIBITED`（拒絕更新、保留原值、繼續下一階段）。
校準五階段跑完（`CALIBRATION_COMPLETE`），E1 開啟 FORMAL_E1_FINAL 一次並得到
`E1_SCIENTIFIC_PASS` —— **但通過的內容很窄，見下方 E1 專節的用字邊界。**

**下一步不是再跑一次校準。** 要讓 signal / sigma / distance 三個通道真的
校準得起來，必須先處理 spp=16 的 Monte Carlo 雜訊（stage 0 已記錄
sd/mean 0.17-0.67），而那會改變 `initial_simulation.lock`，屬 amendment。

**歷史關鍵路徑（已完成）：AMD-004 → CAL-PREREG-003 → 凍結 stage 0 →
第一次合法讀取 → AMD-005 → 校準 → E1。**
`locks status` 顯示 22 個 lock 已凍 2 個（`real_split_policy` /
`initial_simulation`）、`calibrated_simulation` 與 `metric_config` 為 pending、
其餘 18 個 BLOCKED。

**stage 0 已經跑過，結論是 `STAGE0_ADJUDICATION_REQUIRED`**（見下方專節）。
下一棒的順序**不得跳**：

1. 撰寫 `configs/calibration_preregistration.yaml` v0.3.0（依 AMD-004 的十項契約）
2. 凍結 AMD-004，再凍結 CAL-PREREG-003
3. 重跑 stage 0（約 50 分鐘），無 BORDERLINE / UNDETERMINED / 新簡併才凍結
4. stage 1 是本研究**第一次**真實 calibration access，必須另行明確裁決

> `execute_stage0()` 目前會**拒絕執行**，直到
> `freeze/preregistrations/CAL-PREREG-003.prereg.json` 存在。
> 程式已實作 AMD-004 語意，若讓它在只有 CAL-PREREG-002 的情況下跑，
> 產出的 artifact 會宣稱一份它其實沒有遵守的協定。

### 動手前必讀

- `docs/NOTES.md` — 15 則決策記錄。**改動前先查有沒有對應 NOTE**，
  許多看似多餘的設計都是刻意的（例如子行程隔離、檔名鍵對齊、
  `!required` sentinel）。
- `tests/test_repo_integrity.py` — 檔頭十欄位與 NOTE 引用的自動稽核。
  新增檔案沒補齊欄位、或 `驗證方式` 指向不存在的檔案，都會讓 CI 失敗。

### 三條不可協商的紅線

1. 未核定數值一律 `!required`，**禁止補預設值**（NOTE-005）。
2. Provider 只能看到 opaque evidence，class/condition/severity/檔名路徑
   一律不得進入 InferencePayload（NOTE-003、NOTE-004）。
3. Lock 不可覆寫、不可跳步；內容變更必須開新 run。

---

## 目前狀態

| 里程碑 | 狀態 | 說明 |
|---|---|---|
| M0 Data Audit | **資料齊備** | 560 筆 500×4 原始序列已復原；Vision 1200 張完整（NOTE-011） |
| M1 Simulation | **smoke 通過** | mitsuba 3.8.0 / drjit 1.3.1 / mitransient 1.3.0 已安裝 |
| M2 Surrogate + E1 | **`E1 CLOSED — partial calibration success`** | 五階段跑完、E1 開啟一次並得 PASS。**只有 Ambient Rate 真的改善**，其餘三個 feature 維持 initial physics-constrained state。見「E1 已完成」專節 |
| M3 Post-E1 Split | **`paired bridge smoke PASS / ready for perception`** | 成對 RGB-ToF 生成路徑已驗收（6/6 check PASS，identity `a00b3969…`）。synthetic split policy lock 本身仍未凍 |
| M4 Perception | 進行中 | torch 2.10.0+cpu 可用；tensorflow / scikit-learn 未安裝且**不需要** |
| M5 Reliability/Gate | 未開始 | 依賴 M4 |
| M6 Multi-Agent | 未開始 | 依賴 M3；**runtime 管理層（Part VI）已就緒**，缺的是 prompt 與 agent 本體 |
| M7 Pilot/Freeze | 未開始 | 依賴 M5、M6 |
| M8 Formal E2 | 未開始 | 依賴全部 |

Batch 進度依 SRC-SAI Appendix D「Recommended First Sprint」：

| Batch | 內容 | 狀態 |
|---|---|---|
| Batch 1 | core schema + SplitRole + InferencePayload + hashing/logging | **完成** |
| Batch 2 | LegacyCSVAdapter + nominal/usable count ledger + alignment | **完成** |
| Batch 3 | Sigma/timing provenance resolver | **完成** |
| Batch 4 | Mitsuba/mitransient optical transient smoke adapter | **完成** |
| Batch 5 | single-acquisition surrogate + 500-point temporal model | **完成** |
| Batch 6 | E1 metrics + dual-lock + scientific rule state machine | **引擎完成**（校準常數仍 placeholder，見下） |
| Batch 7 | E1-G01..G12 audit + heldout firewall + real split policy audit | **完成** |
| Batch 8 | post-E1 split generators + parent-family checks | 未開始 |

Part VI 依 SRC-SAI §52 的七步順序：

| 序 | 內容 | 狀態 |
|---|---|---|
| 1 | ProviderAdapter normalized contract + secret_ref 抽象 | **完成** |
| 2 | 五張表 + migration（另加 llm_cache_index） | **完成** |
| 3 | capability probes（chat + structured_json + vision；embedding optional） | **完成** |
| 4 | Flask local admin page（§42 圖 8 四張 card） | **完成** |
| 5 | llm_runtime snapshot + hash + state invalidation | **完成**（CLI 先行） |
| 6 | Agent artifact cache | **完成** |
| 7 | §51 十條 security/leakage/cache/resume 驗收 | **完成，10/10 PASS** |

---

## Batch 1 已完成內容（2026-08-25）

檔頭與 NOTE 規範：全部原始檔已改為十欄位維護契約格式，
決策記錄移至 `docs/NOTES.md` 並補齊五欄位；
兩者由 `tests/test_repo_integrity.py` 自動稽核（已用三種人為破壞驗證過會攔截）。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `core/constants.py` | SRC-D04、FR-002、§16 | CLASS_ORDER、TOF_SCHEMA、EPS、legacy label map |
| `core/numeric.py` | 式(1)-(8)、§16、FR-030 | 唯一的 stabilize_prob / JSD / entropy / support bridge |
| `core/hash.py` | NFR-01、§48 | canonical JSON + SHA-256 + array/file hash |
| `core/schema.py` | §6、Appendix A1 | CanonicalCase、七類 SplitRole、CountStatus、雙時間軸 |
| `core/ids.py` | §34 | 六種 immutable ID 格式與驗證 |
| `core/opaque_ids.py` | Appendix I1、G2 | run-scoped keyed HMAC opaque map |
| `core/inference_payload.py` | Appendix G2、I1、EI-P0-08 | truth firewall + quality cue allowlist |
| `core/config.py` | §33、Appendix A/F | !required sentinel + formal-blocking |
| `core/locks.py` | Appendix G3、§23 | 22 個 lock 的內容契約與前置順序 |
| `core/logging_setup.py` | NFR-08、LLM-SEC-01 | secret 遮蔽過濾器 |
| `cli.py` | FR-019、FR-043 | version / config show / config check / locks status |
| `tests/test_repo_integrity.py` | 檔頭與 NOTE 規範 | 十欄位齊全、驗證方式有效、NOTE 引用不失效 |
| `tests/secret/test_log_redaction.py` | NFR-08、LLM-SEC-01 | 四家 provider key 樣式的 log 遮蔽驗收 |
| `tests/test_cli.py` | FR-019 | formal 模式必須以非零 exit code 中斷 |

已建立但尚未使用的 artifact：
`schemas/arbitration_output_v1.schema.json`（依 §21 逐字複製，
formal_config.lock 將保存其 SHA-256）、`observation_brief_v1`、
`specialist_proposal_v1`（依 Appendix I5 的必要欄位建立）。

---

## SRC-NOTION 一手來源稽核（2026-08-25）

直接讀取 Notion「研究交接」的原始程式碼，與 Batch 1 逐項比對。

**確認正確（一手證據）**

| 項目 | 一手證據 | 結果 |
|---|---|---|
| 四特徵 canonical 順序 | `extract_features_from_window()` 明寫 `distance, ambient, signal, sigma`，並附作者註解「根據 Edge Impulse 訓練時的特徵順序一致」 | NOTE-001 成立 |
| 標籤映射 | `{'nowater':'Empty','water':'Water-filled','bubble':'Bubbly','smoke':'Misty'}` | 逐字相符 |
| 500×4 形狀 | `WINDOW_SIZE = 500`、`FEATURES_PER_SAMPLE = 4` | 相符 |
| 140 recordings/class | `TOTAL_WINDOWS = 140` | 相符（4×140=560 nominal） |
| physical > logical | 四 metric 分檔 → 560×4 = 2240 實體檔 | 設計正確 |
| 82ms vs 0.02s | 三份腳本皆 `SAMPLE_INTERVAL = 0.02`，且迴圈另有 `sleep(0.01)` | SRC-D03 屬實 |

**發現的 Batch 1 缺口（已修）**

| 缺口 | 事實 | 處置 |
|---|---|---|
| 無 metric 對齊 provenance | legacy 合併程式**純依排序位置**配對四個 metric 檔，無檔名比對；某資料夾少檔只記 missing 不中斷 → 該點之後全部靜默錯位 | 新增 `MetricAlignment`，real case 強制逐 metric 記錄原檔名/列數/SHA-256，四檔列數不一致即拒絕 |
| 無 Sigma provenance 強制 | `0x1E`（四參數、兩參數）與 `0x18`（單一參數）並存；scaling 三份一致為 `/65536.0` | real case 強制 `provenance.sigma_status`；`sigma_provenance` 兩項列入 formal-blocking |

決策記錄見 `docs/NOTES.md` NOTE-010。

---

## Batch 2 已完成內容（2026-08-26）

測試：**416 passed**。程式碼完成且以合成 fixture 全路徑驗證；
真實資料到位後直接指向 `data/raw_real/` 即可執行，**不需要改碼**。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `adapters/base.py` | §6 圖2、§7.8 | EvidenceSource 契約、SourceFile、AlignedMeasurement、封閉列舉的 ExclusionReason |
| `adapters/legacy_csv.py` | §7、§8、NOTE-010 | 盤點、檔名鍵對齊、五層計數、取樣間隔推導 |
| `cli.py audit real-data` | §32 CLI 契約、E1-G01 | 產出四份 M0 artifact |

**產出 artifact**：`source_inventory.csv`、`measurement_alignment.csv`、
`exclusion_ledger.csv`、`audit_report.json`。

**關鍵設計：對齊改用檔名編號，不用排序位置。**
以 10 筆/類、`smoke/sigma` 刻意缺第 2 筆的合成資料實測對照：

| 做法 | 結果 |
|---|---|
| legacy 位置配對 | `distance_004`+`sigma_005`、`distance_005`+`sigma_006`…**6 筆靜默污染**，無 NaN、不超 range、無症狀 |
| 本系統檔名鍵配對 | **乾淨排除 1 筆**（smoke/2），其餘 9 筆四個 metric 編號完全一致 |

**其他守衛**：四檔長度不一致在 formal 模式直接 ERROR（§7.5，不得靜默裁切）；
取樣間隔一律從該筆 CSV 的時間欄推導（`derived_from_column_0`），
推導失敗即拒絕載入，不得沿用 0.082 或 0.02 全域常數；
Sigma 未解析時 `e1_eligible_recordings` 強制為 0（E1-G08）。

~~**尚未實作**：`core/splits.py`（SplitRegistry）~~ → **已於 2026-08-26 補上**。
當初延後的理由（`real_split_policy` 的 creation_phase 是
`AFTER_M0_BEFORE_ANY_CALIBRATION`，必須先有真實 inventory）已滿足。
見「Real split 已凍結」與 NOTE-014。

---

## Batch 3 已完成內容（2026-08-26）

測試：**416 passed**。三個模組都在**真實資料**上實測過，不只是合成 fixture。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `adapters/legacy_kg.py` | NOTE-011 | (99,140) 彙總格式 reader；32 矩陣 → 560 recordings |
| `provenance/sigma.py` | SRC-D01/D02、E1-G08 | scaling 假設檢定 + register 狀態判定 |
| `provenance/timing.py` | SRC-D03、§7.7 | 雙時間軸分離 + 三來源間隔偏差量化 |
| `cli provenance resolve-sigma` / `audit-timing` | §32 CLI 契約 | 產出兩份證據 artifact |

### SRC-D02（Sigma scaling）→ **已解決**

以 **55,440 個真實觀測值**檢定三個 scaling 候選：

| 除數 | 隱含原始值域 | 佔 16-bit 量程 | 判定 |
|---|---|---|---|
| `/1` | 0.36–0.66 | — | ✗ 100% 觀測值非整數，暫存器讀值必為整數 |
| `/128` | 45.5–85.0 | **0.06%** | ✗ 需感測器全程只用量程角落 |
| **`/65536`** | **23311–43509** | **30.8%** | **✓ 唯一相容** |

### SRC-D01（Sigma register）→ **仍 UNRESOLVED**，但理由精確

0x18 與 0x1E 都是 16-bit 讀值、共用同一除數，**值域無法區分**。
唯一能解決的是**產生這批資料的採集腳本**——不是已知那三份推論腳本。
系統拒絕在缺此證據時標為 RESOLVED，四特徵 E1 primary continue 被 E1-G08 擋下。

### SRC-D03（取樣間隔）→ **已釐清：資料集內有兩種取樣率**

初次盤點時只看到偏移測試的實測值 0.0624 s，據此推論「0.082 沒有根據」。
**該推論已修正**——後續讀到 Edge Impulse 設定截圖：

| 證據 | 值 | 換算 |
|---|---|---|
| Edge Impulse `Frequency` | **12.19512 Hz** | → **0.082 s**，即 SRC-PLAN 記載值 |
| Edge Impulse `Window size` | **41,001 ms** | → 41001/82 = **500.01 ≈ 500 點** |
| `Training windows` × 41 s | 448 × 41 s | → 18,368 s = **5h 6m 8s**，與畫面吻合 |

**0.082 是主資料集的正確取樣率**；0.0624 屬於偏移測試那次獨立採集。
兩者是不同的 acquisition session，不是矛盾。

這個修正**強化**了逐 recording 推導間隔的設計：同一資料家族內真的存在
不同取樣率，任何全域常數都必然對其中一批是錯的。

---

## Batch 4 已完成內容（2026-08-26）

測試：**558 passed**。M1 Simulation smoke 通過，E1-G03 的 artifact 已產出。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `simulation/scenario.py` | §9 scenario.yaml | 幾何/介質/光源契約；formal 模式拒絕 placeholder 介質 |
| `simulation/mitsuba_adapter.py` | §9 MitsubaAdapter | RGB 算圖 + version/variant/seed/spp provenance |
| `simulation/mitransient_adapter.py` | §9 MiTransientAdapter | optical transient + 獨立時間軸檔 |
| `simulation/controller.py` | §9 SimulationController、E1-G03 | 編排 + smoke manifest |
| `cli sim smoke` | §32 CLI 契約 | 子行程隔離執行（NOTE-012） |

**實測結果**（四類各一場景，64×64、spp=16、128 bins）：

| 場景 | transient shape | bin 寬 | 總能量 |
|---|---|---|---|
| Empty | (64,64,128,3) | 9.43 ps | 712.7 |
| Water-filled | 同上 | 同上 | 831.7 |
| Bubbly | 同上 | 同上 | 842.1 |
| Misty | 同上 | 同上 | 850.6 |

時間軸 0.27–1.47 ns，與 5 公分場景的光飛行時間相符；
連續三次執行能量完全一致（842.1），**determinism 確認**（E1-G03 要求）。

**兩個關鍵設計決定**：

1. **場景單位用公尺不用毫米**。transient 的時間軸由光在場景中的行進距離決定，
   單位錯了時間軸會整整差三個數量級。
2. **binning 由幾何推導，不沿用 cornell_box 預設**。mitransient 範例是房間尺度
   （start_opl=3.5 m），本場景只有 5 公分，直接沿用會算出全零的 transient。

**已知環境問題**：drjit/mitsuba 在 Windows 連續算多場景後，於 DLL detach 階段
崩潰並回報非零 exit code（工作其實已全部完成）。已用子行程隔離處理，
父行程以 manifest 判定成敗但仍完整回報子行程 exit code。詳見 NOTE-012。

**claim boundary**：材質與介質參數**尚未校準**，本批次產物不得用於任何
physics fidelity 主張（E1-G12）。介質參數在設定檔中全部標記 `placeholder: true`，
formal 模式會直接拒絕載入。

---

## Part VI 已完成內容（2026-08-27）

測試：**1013 passed**（先前 617）。依 §52 七步順序實作，未跳步。
§51 的十條驗收全部 PASS，且由 `tests/llm_admin/test_acceptance_matrix.py`
自動確認每一條都有**會失敗的**對應測試 —— 少寫一條、或用 skip 蒙混，都會被擋。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `secrets/crypto.py` | §46 | Fernet + PBKDF2；缺 cryptography 時 fail-closed 不自製加密 |
| `secrets/vault.py` | §43、§46 | `env:` / `vault:` / `session:` 三種參考；HMAC 指紋 `****abcd` |
| `agents/provider.py` | §43 介面草案 4、§44 | normalized 契約 + Google/OpenAI 相容 + 離線 stub |
| `agents/cache.py` | §48、Appendix J2 | content-addressed cache；pair 專屬量寫入即拒 |
| `llm/registry.py` | §49 | 六張表 + migration；明文 key 與 session ref 進不去 |
| `llm/capabilities.py` | §44、§45 | 四個 task 的能力矩陣；declared 與 verified 分離 |
| `llm/verification.py` | §44 | probe 執行與不可覆寫的證據 artifact |
| `llm/snapshot.py` | §47、Appendix J1 | lock candidate + state invalidation + formal 解析 |
| `admin/{auth,services,routes_llm,app}.py` | §46、§50 | 網路邊界、共用服務層、八個端點 |
| `admin/templates/llm_setup.html` + `static/admin.css` | §42 圖 8 | 四張 card、1160 px、零 script |
| `cli.py llm * / admin serve` | §32 | 連線／模型／綁定／快照／快取／啟動頁面 |

**§51 驗收對照**（`tests/llm_admin/`、`tests/secret/`、`tests/cache/`）：

| ID | 落在 | 結果 |
|---|---|---|
| LLM-UI-01 | `test_admin_page.py` | PASS（response / HTML / log 三處掃 8 字元片段） |
| LLM-UI-02 | `test_admin_page.py` | PASS（embedding-only 不進 dropdown） |
| LLM-UI-03 | `test_admin_page.py` | PASS（structured-json FAIL → Bind 400） |
| LLM-UI-04 | `test_admin_page.py` | PASS（409 + dependency 清單） |
| LLM-UI-05 | `test_snapshot.py` | PASS（改 draft 後 lock hash 不動） |
| LLM-UI-06 | `test_snapshot.py` | PASS（手改 SQLite 後仍解析 lock 內身分） |
| LLM-SEC-01 | `test_global_secret_scan.py` | PASS（跑完整流程後逐位元組掃全部產物） |
| LLM-CACHE-01 | `test_agent_cache.py` | PASS（3 次查詢 → 1 次 provider call） |
| LLM-CACHE-02 | `test_agent_cache.py` | PASS（七個要素逐一變動皆 miss） |
| LLM-RESUME-01 | `test_agent_cache.py` | PASS（resume 呼叫 provider 即測試失敗） |

**實機驗證**（隔離的 scratch registry，離線 stub provider）：
CLI 五步全走通；瀏覽器開啟 `/admin/llm-setup` 後確認四張 card 齊全、
內容寬度 1160 px、`document.scripts.length === 0`、憑證顯示為 `****d1ff`、
四個角色的 dropdown 都只列出唯一能力相容的模型（embedding-only 與
chat-only 被正確排除）、Formal Snapshot 卡片顯示 `no active snapshot`
並列出 10 項未達前提。頁面 HTML 內找不到 key，也找不到 `env:` 參考。

**三個實作過程中被規格逼出來的修正**：

| 發現 | 事實 | 處置 |
|---|---|---|
| Bind 拒絕理由指錯地方 | 初版讓任一 probe 失敗就把 connection 標成 degraded；LLM-UI-03 確實被拒，但理由是「連線異常」而非「缺 structured_json」 | probe 失敗不再動 connection 狀態；連線層故障只由 `fetch_models` 判定（NOTE-018） |
| 原始碼被 .gitignore 吞掉 | `secrets/` 這條規則同時命中 `pcmef/secrets/`，secret_ref 抽象層整包不會進版控 | 改為 `/secrets/`，只排除 repo 根目錄的 vault |
| LLM-SEC-01 只有局部掃描 | 原本只在 registry 與 verification 各掃自己那一份，不是規格要求的「全域掃描」 | 新增 `test_global_secret_scan.py`，跑完整流程後掃描該次產生的每一個檔案 |

### 設定流程與版型對齊 LAVA setup（NOTE-023）

§42 規定了四張 card 與欄位，但沒規定**操作者要照什麼順序把一條線路設定好**。
roothinks（主要參考）與 rootmedicals-a 兩套系統在真實使用中收斂到同一個流程，
使用者也是同一個人，因此 PC-MEF 直接沿用而不自創第三種：

```
draft ──Fetch──► fetched ──Test──► connected ──Connect──► locked
                    ▲                   │                    │
                    └──── 換模型 ────────┘         只有 locked 能綁定
```

| 狀態 | 可做 | 不可做 |
|---|---|---|
| draft | Fetch、選模型 | Test、Connect |
| fetched | Test、換模型、重新 Fetch | Connect |
| connected | Connect、Test、換模型 | — |
| locked | Unlock、Delete | 換 vendor/key/模型、Fetch、Test |

衍生的三條規則：**只有 locked 的線路出現在綁定選單**（LAVA 的「僅顯示 Locked」）；
**一條 locked 的線路只提供它被檢查過的那一個模型**；**換模型會退回 fetched**
（先前的 Test 對新模型無效）。`llm_task_bindings.is_locked` 是 **draft 層的確認鎖**，
與 formal 的 `llm_runtime.lock` 是兩件事。

**與 roothinks 的三處刻意差異**：

| 項目 | roothinks | PC-MEF | 理由 |
|---|---|---|---|
| Test 內容 | 送一句話看回不回 OK | 跑 chat + structured_json + vision 三項 probe | §44 要求的能力只有實際 probe 才知道；回一句 OK 什麼都證明不了 |
| 憑證遮蔽 | key 後四碼 | HMAC 指紋 `****abcd` | §46 不准顯示 key prefix（NOTE-016） |
| 前端 | JS 驅動逐列更新 | server-rendered 表單，零 script | §42 明訂 minimal JS；狀態機與資訊架構完全相同，只是每個按鈕改為 POST + redirect |

CLI 有完整對等指令：`llm connection select-model / test / lock`、`llm binding lock`。

**實機驗證**：locked 那一列的 Fetch/Set/Test 皆 disabled 且只剩 Unlock/Delete；
draft 那一列 Fetch/Set 可用而 Test/Connect disabled；已鎖定的 arbitration_agent
不顯示 dropdown 而顯示「已鎖定，解鎖後才能更換」；`document.scripts.length === 0`。

**與 §31 目錄樹的兩處刻意差異**：`llm/bindings.py` 與 `llm/cost_ledger.py`
未建成獨立檔案。binding 的寫入與能力檢查已在 `registry.set_binding()` 與
`capabilities.py`，另開一個只做轉呼叫的模組會讓同一條規則有兩個入口；
費用帳由 `llm_cache_index` 這張表承擔，§49 本來就把它列為該表的用途
（"content-addressed lookup / cost audit"）。

**目前無法凍結 `llm_runtime.lock`，這是正確結果而非缺陷。**
`llm snapshot` 實測列出 10 項未達前提：四個角色的 prompt 檔屬 M6 尚未撰寫、
`agents.representation_mode` 與 `agents.retry.max_attempts` 仍待教授裁決、
且 lock 相依鏈要求 `agent_schema` → `synthetic_split_policy` → `e1_outcome`，
亦即必須先跑完 E1 與 M3。Part VI 的完成度不以能否凍結衡量（NOTE-020）。

---

## Phase A：Parameter registry（2026-08-28，configs/parameter_registry.yaml）

**先前人工數的「10 個未校準常數」嚴重低估。實際掃描結果是 38 個參數，
其中 29 個未解決。**

| 分類 | 數量 |
|---|---|
| **總參數** | **38** |
| fixed（物理常數／SRC-PLAN 錨點／rank-1 dataset） | 7 |
| topology（類別語意決定，非可調） | 1 |
| derived（由幾何決定） | 1 |
| **calibration_only** | **29** |
| **unresolved（PLACEHOLDER/UNKNOWN/RECONSTRUCTED）** | **29** |
| formal_blocking | 30 |
| class_specific | 5 |

### 已更正的 provenance 標記

`_FOIL_GAP_TO_BOTTLE_RATIO = 1.0` 標為 **RECONSTRUCTED / calibration-only
nuisance**，**不是** provenance-supported —— 箔片「存在」有依據，
它「在哪」沒有。

> 上表為 v0.1.0（8/28 凌晨）建立 registry 當下的數字。
> **當日稍晚 A1/A2 完成後已變動**，最新值見下一節。

---

## Phase A1/A2 完成（2026-08-28，NOTE-030 ~ NOTE-033）

registry 由**資料層**接上**真的會擋人的**防線；三組 confounding 全部裁決；
角度慣例由實測定案。

### A1 —— firewall / hash / tests

| 項目 | 狀態 |
|---|---|
| registry 接進 formal firewall | **完成**。`ScenarioConfig(formal=True)` 與 `SurrogateCalibration.assert_formal_ready()` 兩處都轉呼叫 `core.parameters.assert_formal_ready()` |
| `parameter_set_hash` | **完成**。`d6d30c7afe128454…`（v0.2.0） |
| formal artifact 記錄 registry/hash | **完成**。simulation manifest 新增 `parameter_registry` 區塊與 `run_identity_hash` |
| negative tests | **完成**。`tests/unit/test_parameter_registry.py` **28 passed** |

`py -3.10 -m pcmef.cli params audit` 實測（exit code **2** = 不可進 formal）：

```
parameters 38  unresolved 27  formal blockers 27
status: CONFIRMED=9 CONVENTION=2 PLACEHOLDER=19 RECONSTRUCTED=3 UNKNOWN=5
bindings: code=30  config=8  unbound=0        live value drift: 0
CG-1 RESOLVED / CG-2 RESOLVED / CG-3 BLOCKED
formal-ready: NO
```

**綁定涵蓋率誠實分三種**，不湊 100%：30 項綁到程式常數（可靜態比對）、
8 項由 scenario yaml 逐 run 供應（靜態比對無意義）、**0 項未綁定**。
`live_value_drift()` 對 30 項回報 **0 筆漂移** —— 這是 registry 沒有變成
第二份會漂移的文件的證據。

**四種必須 FAIL 的情況都已實測會 FAIL**（不是宣稱）：

| 破壞 | 失敗的測試 |
|---|---|
| registry 不見 | `test_missing_registry_fails` / `..._blocks_via_reasons` |
| 留著 placeholder | `test_unresolved_status_fails[PLACEHOLDER/UNKNOWN/RECONSTRUCTED]` |
| confounded group 未裁決 | `test_undecided_confounded_group_fails` |
| hash 對不上 | `test_hash_mismatch_fails` |

另做**三次實際破壞**確認測試會咬人：放寬 `FORMAL_ELIGIBLE_STATUSES`、
讓 `gauge_violations()` 回空、讓 `with_calibrated()` 不擋 derived ——
三者都造成對應測試 FAIL。`test_a_clean_registry_passes` 刻意保留：
少了它，其餘負向測試會在「防線永遠拒絕一切」的情況下全部通過。

### A2 —— 三組 confounding 裁決

| 組 | 裁決 | 固定 | 校準 |
|---|---|---|---|
| **CG-1** 消光係數 | **RESOLVED** | `_SIGMA_T_REFERENCE_PER_M` | 三個密度 |
| **CG-2** 絕對能量 | **RESOLVED** | `lighting.irradiance` | `signal_energy_to_mcps` |
| **CG-3** Ambient | **BLOCKED** | — | — |

**CG-2 的組成原本是錯的，實測推翻後才裁決：**

| 變動 | 總能量比 | 正規化波形逐 bin 差 |
|---|---|---|
| `irradiance` 1.0 → 2.0 | **2.0000** | **0.0（恰為零）** |
| `_FOIL_REFLECTANCE_940NM` 0.85 → 0.425 | 0.983 (Empty) / 0.768 (Water) | L1 **0.020 / 0.166** |

`irradiance` 是**精確全域增益**，與 `signal_energy_to_mcps` 完全簡併；
而 `_FOIL_REFLECTANCE_940NM` 只縮放箔片那一支回波、會改變波形**形狀**，
因此可分離 —— **已移出 CG-2**，改為獨立 calibration-only 參數。
原「三者相乘」的登記是錯的。

固定項的選擇不用「哪個好 fit」決定，用 provenance：CG-1 依 SRC-SAI §9
把密度列為 `<validation-frozen range>`（規格從未提及參考尺度）、且固定必須
是 shared 那一項否則三類互相耦合；CG-2 依 SRC-HANDOFF 未回收 VCSEL 光功率
（W/sr 的絕對值無一手來源可對），任意輻射尺度應落在 energy→MCPS 換算上。

**新增受限的 `CONVENTION` 狀態，並讓它是機器可檢查的。**
gauge fixing 與「偷給預設值」表面相同，差別在於前者對應**原理上不可辨識**的
自由度。因此 `CONVENTION` 設計成配對鎖：單獨改標不會放行，必須被某個
**RESOLVED** group 指名為 `fixed`，且該組必須寫出 `claim_boundary`。
已寫的兩條 claim boundary：校準後的三個密度**不得**解讀為絕對濁度／密度
（可宣稱的只有 σt）；`irradiance = 1.0` **不得**引用為 VCSEL 實際發射功率。

### A2 —— 角度慣例：兩個「25」意思差兩倍

**實測（非查文件）**：

| 量 | 設定值 | 實測 | 慣例 |
|---|---|---|---|
| perspective `fov` | 45 | 單邊半角 **22.4959°**、對角 **30.3562°** | **全角**，預設綁 x 軸 |
| spot `cutoff_angle` | 25 | 24.9° 有光、25.1° 為 **0** | **半角**，即 **50° 全角** |

三個直接後果：

1. 「光源 25° 比感測器 45° 窄」是**錯覺** —— 照明錐 50° 全角其實**寬於**
   接收視野 45° 全角。
2. **`sensor.fov_deg` 維持 45，不改成 25。** 25° 這個目標值在 SRC-PLAN 與
   SRC-HANDOFF 都查不到，也沒有記載它是 full-cone 還是 half-angle；
   拿慣例不明的數字去改慣例已知的數字，只是把不確定性藏進場景。
3. 若真改成 25（若那是全角），接收半角會壓到 12.5°，而瓶身對相機張角是
   21.3° —— 瓶身會超出視野。那是**場景層級變更**，不是修正筆誤。

三個硬編值已**參數化為具名常數**（`_SENSOR_FOV_DEG` / `_LIGHT_CUTOFF_ANGLE_DEG`
/ `_MAX_DEPTH` / `_FOIL_ORIENTATION`），**數值一個都沒改** ——
這只是讓防線攔得到它們。`_foil_transform()` 現在對未實作的朝向直接拒絕，
不再退回預設。

### A2 —— `optical_path_to_distance` 已離開 calibration

改標 `derived=True` / `placeholder=False` / `formal_blocking=false`，
並由 `SurrogateCalibration.with_calibrated()` **拒絕覆寫**。
先前它同時有兩個錯：formal 模式因為它而擋（理由是錯的），
且它在型別上仍是可被 calibration 寫入的旋鈕 —— SRC-SAI §10 禁止拿它逼近
真實均值，但一句註解攔不住任何人。

### 順帶修掉的 provenance 不實

`TransientResult.extra["integrator"]` 先前**硬編** `"transient_path"`，
但帶介質的場景實際用 `transient_prbvolpath`。四個 smoke 場景中**有三個**的
manifest 記載了它們沒有用過的積分器。manifest 是 provenance，記錯比不記更糟。

---

## B：Ambient 根因 —— 量到的是雷射多重反射（2026-08-28，NOTE-032）

**先更正問題敘述：Ambient 現在不是 0，而是「不是 ambient」。**

`background_energy := total − 主窗能量`。主窗外那些能量是**感測器自己打出去
的光**經箔片與內側介面回來的多重反射。實測（室內光比例 0.0 / 0.02 / 0.5）：

| | Empty | Water-filled |
|---|---|---|
| `background_energy`（室內光**關閉**） | **26408.47** | **200336.76** |
| `background_energy`（室內光 0.02，現行） | 26417.22 | 200344.72 |
| **室內光佔比** | **0.033%** | **0.004%** |
| **雷射多重反射佔比** | **99.967%** | **99.996%** |
| 純 ambient pass（移除 spot）總能量 | **14.17** | **12.32** |

三件事同時成立：

1. **ambient 從來不是 0。** 室內光完全關閉時 `background_energy` 仍是 Empty
   總能量的 7%。NOTE-026 預測的「monostatic 後 ambient 恆為 0」**沒有發生**，
   所以加 `constant` 室內光解決的不是它宣稱要解決的問題。
2. **真正的 ambient 訊號小三到四個數量級** —— 相差 1864 倍 / 16262 倍。
3. **`_ROOM_LIGHT_RATIO` 因此不可辨識**：比例從 0 拉到 0.5（25 倍於現值），
   總能量只變動 0.058%。

結構性旁證：純 ambient pass 只填滿 **23/128** 個 bin，前六個 bin 恰為 0。
真正的環境光 DC 速率應均勻鋪滿時間軸；`constant` environment emitter 在
transient 中只透過打到幾何再回來的路徑出現，本來就不是 DC pedestal。

**這是校準目標定義錯誤，不是證據不足。** 因此 CG-3 標 BLOCKED 而非給預設值。
解除路徑（寫進 registry 的 `unblock_requires`）：
獨立的 ambient pass（關閉 spot）→ 重新量 `_ROOM_LIGHT_RATIO` 的槓桿 →
本組退化為兩項簡併後再依 CG-2 同一套理由選固定項。

> 不得用「`background_energy` 減掉估計的多重反射量」來修 ——
> 那是用一個未校準的量去修另一個未校準的量。

---

## C：Distance estimator —— 設計已定，但**現在不能選**（BLOCKED）

**根因量化：模擬波形在真實感測器讀值的區間幾乎沒有能量。**
四類真實均值落在 79.51–113.87 mm。以 `outputs/scene_v2` 逐 bin 統計能量分佈：

| 場景 | 峰值 mm | 重心 mm | 40–55 | 55–75 | **75–120** | 120–200 |
|---|---|---|---|---|---|---|
| Empty | 147.39 | 140.47 | 6.66% | 0.18% | **0.15%** | 93.01% |
| Water | 162.63 | 142.45 | 2.83% | 18.07% | **0.03%** | 79.06% |
| Bubbly | 162.63 | 110.74 | 26.16% | 1.94% | **25.72%** | 46.16% |
| Misty | 45.77 | 84.52 | 31.74% | 23.69% | **15.73%** | 28.83% |

> **2026-08-28 更正（重要）：上表不是 M2 formal blocker，本節先前的框架是錯的。**
>
> 先前寫「75–120 mm 無能量 ⇒ scene physics 缺口 ⇒ estimator 不能設」。
> 這個推論把**校準階段的 mismatch 誤判成場景拓樸缺陷**，而且它會直接誘導出
> 被禁止的做法 —— 若把「讓能量進入 75–120 mm」當成 pre-calibration 的修正目標，
> 那個目標本身就是從真實類別均值反推出來的。
>
> 正確認定：**tracing-supported 的 far-side foil family 已經存在**
> （約 145–163 mm，NOTE-029 以 `max_depth` 遞增取得，非位置比對）。
> 它目前偏離真實讀值，可由**未校準的** `_FOIL_GAP_TO_BOTTLE_RATIO`、
> 其餘 foil 光學常數與介質參數解釋 —— 這正是 calibration 要處理的事。
> 這種 mismatch **應該保留在 `initial_simulation` 裡**，不得在 initial freeze
> 之前把場景調進該區間。
>
> **紅線**：不得以真實類別均值、或 75–120 mm 視窗，作為 pre-calibration 的
> 場景修正目標。initial simulation **不要求**貼近真實分佈。

**仍然成立、且必須保留的那一條**：`ENERGY_CENTROID` 在 Bubbly（110.74 vs
真實 105.57）與 Misty（84.52 vs 79.51）看起來很接近，Empty／Water 則差 40 mm。
若因為「四類中有兩類對得上」而選它，那就是拿真實類別均值挑 estimator，
SRC-SAI §10 明列為禁止做法。**本節記下這件事，就是為了讓下一棒不會踩進去。**

### 已定案的設計

*措辭一律為 VL53L0X-inspired / VL53L0X-like transient range estimator；
ST 未公開最終 range 的產生方式，不得宣稱重現 internal algorithm。*

| 要素 | 設計 | 為什麼滿足紅線 |
|---|---|---|
| 輸入 | `collapse_to_waveform()` 全像面加總 | 對應單一 SPAD 陣列，與類別無關 |
| 基線 | 減去**獨立 ambient pass** 的基線 | 不是可調常數；依賴 B 先修好 |
| 回波偵測 | 相對波形自身雜訊水準的門檻，取連續超閾區段 | **不是 mm 搜尋窗**，不含任何絕對距離邊界 |
| 合併 | 對**合格回波**做能量加權 | 單一函式、單一組參數 |
| 自由參數 | 僅門檻與合格判準，**全類共用**，登記於 registry 並帶 allowed_range | class-independent |

**選定準則必須預註冊且來自物理，不是來自資料擬合。** 建議準則：
**偏移量測試錨點**（實測 98.53 / 90.91 / 88.73 mm，與計畫書相差 < 0.15 mm）。
它同時滿足三件事 —— 來自真實硬體、是**幾何掃描而非類別比較**（因此
class-independent）、且屬於偏移測試那次**獨立採集**，不在 560 筆的
held-out 內（held-out 由那 560 筆抽出）。用它約束 estimator 不會碰到
類別均值，也不會碰到 held-out。

### 預註冊比較的實測結果（2026-08-28，NOTE-035）

預註冊檔以**獨立 commit**（`68b21eb`）先進版控，內容不含任何結果，
`preregistration_hash = 27ff55e2332ba8f7…`。之後才跑比較。

| 候選 | S1 幾何單調（50→60 mm，須落在 5–15） | S2 增益不變 | 結果 |
|---|---|---|---|
| PEAK | +11.62 mm | 0.0 | **存活** |
| ENERGY_CENTROID | **+16.96 mm** | 0.0 | **淘汰** |
| LEADING_EDGE | +9.70 mm | 0.0 | **存活** |
| STRONGEST_RETURN_CENTROID | +12.21 mm | 0.0 | **存活** |

**硬門檻淘汰的正是先前標記的那個陷阱。** `ENERGY_CENTROID` 跟著整條波形的
一階矩跑而非量距離，被 S1 擋下 —— 而它正是在 Bubbly／Misty 上看起來最接近
真實均值的那一個。用物理判準淘汰它，與「因為看起來準所以選它」方向相反。

**結果為 `TIE_BREAK_REQUIRED`，estimator 尚未選定。**
`real_data_consulted: false` —— stage 1/2 完全沒有碰真實資料。

**stage 3（偏移錨點）經實測判定不可執行**，三個獨立理由：

1. **自變數量級對不上**：`configs/base.yaml` 記為 ±0.1 cm（±1 mm），
   但瓶半徑 28.5 mm 上橫移 1 mm 的弓形高只有 **0.018 mm**，
   比觀測到的 7.6–9.8 mm 小三個數量級。
2. **符號不可能**：兩側**都變小**（Δ −7.62 / −9.80 mm）。凸面前表面橫移
   只會讓最近點變遠，不可能兩側都變近；場景鏡像對稱，產生不出這個型態。
3. **沒有鑑別力**：實測橫移 ±1 mm 時三個存活候選的估計值變化**全為 0.00 mm**
   （低於 3.12 mm 的 distance bin 寬）。

強行使用它只能靠挑一個能讓某候選勝出的位移量 —— 那正是預註冊要防的事。
**因此本 session 不選定 estimator，也不放寬門檻重跑。**

> 順帶量到但**不得用於本次選定**：橫移 +5 mm 時 PEAK 由 145.88 跳到 45.82 mm，
> `STRONGEST_RETURN_CENTROID` 同樣跳 −101 mm，而 −5 mm 幾乎不動。
> 場景鏡像對稱卻不對稱，代表這是 **path family 模式切換**而非幾何響應。
> 它不在預註冊判準內，事後拿來選就是發明新規則；記錄供未來 amendment 引用。

### 解除 C 的前置（順序不得跳）

1. B 的 ambient 觀測量改正（獨立 ambient pass）—— **已完成，NOTE-034**
2. **預註冊**候選 estimator 與其可調參數，再比較 —— **已完成，NOTE-035**
3. 依預註冊的判定規則選定，只用 physics / synthetic sanity /
   獨立 offset anchors，**不得用四類真實均值**

---

## D：M2 closure —— 六條 closure 條件，五條通過

判準改依 M2 closure 的六個條件（**不含**「貼近真實分佈」），
由 `py -3.10 -m pcmef.cli audit initial-simulation` 判定（NOTE-036）：

| ID | 條件 | 現況 |
|---|---|---|
| IS-01 | physical scene topology 合理 | **PASS** 空瓶為殼＋空氣＋殼；ToF 光路無純視覺幾何 |
| IS-02 | foil return family 存在 | **PASS** 四場景皆有非零回波（lineage 見 NOTE-029） |
| IS-03 | Ambient observable 定義正確 | **PASS** 四項分離檢查全過（NOTE-034） |
| IS-04 | Distance estimator 定義固定 | **FAIL** 尚未選定，見 C |
| IS-05 | 所有 calibration knobs 被 registry 管住 | **PASS** 38 項；code=30 config=8 unbound=0；drift=0；三組全裁決 |
| IS-06 | 未校準值明確保留為 placeholder/nuisance | **PASS** 26 項未校準，全部有狀態與搜尋邊界 |

```
PASS 5  FAIL 1  NOT_PRODUCED 0     initial_simulation freezable: NO   -> exit 2
```

**IS-06 與 `params audit` 刻意不同，這是本節的關鍵。** 前者問「pre-calibration
狀態可否凍結」，後者問「可否進 formal run」。26 項仍是 placeholder 讓
`params audit` 回 exit 2 是正確的，同時讓 IS-06 PASS 也是正確的 ——
initial freeze 發生在 calibration **之前**，要求「校準完才能凍結校準前的狀態」
會是循環（NOTE-036）。

IS-06 上線第一次跑就抓到一個真實缺口：`resolution` 是 `calibration_only`
卻沒有 `allowed_range`，等於一個沒有搜尋邊界的可校準參數。已補為逐軸 [32, 512]。

**端到端重跑**（`outputs/phaseBC_verify` / `phaseBC_surrogate`，四類全 ok）：

| | Empty | Water | Bubbly | Misty |
|---|---|---|---|---|
| distance (mm) | 147.5 | 162.7 | 96.63 | 145.8 |
| **ambient (MCPS)** | **14.19** | **12.34** | **5.946** | **9.596** |
| signal (MCPS) | 2.505e+05 | 4.824e+05 | 5.082e+04 | 4.132e+04 |

Ambient 由改動前的 12,111–173,643 降到 5.9–14.2 —— 三到四個數量級，
與 NOTE-032 量到的「真 ambient 比多重反射小 1864–16262 倍」一致。
**這不是變好或變差，是改成量另一個東西。**

---

## E：`initial_simulation.lock` —— **已凍結**（2026-08-28）

```
payload hash       dc15c9543a3aecacafcd1cbc110c48fc5fdd08d28457d4859d2a0bf573cb8335
code_version       56c54dd7abe907a72fd4b1c57d3fcbca8ce28c9e   （工作區乾淨）
scene_hash         f60b2a0ce12792fd43a03e26ba629b435584e02b65269f24ed7142e2c6f7d2b6
parameter_set_hash 3bd65b50264413e6249d1d19cc900a8487673f073a4e531b3c456b760a39ff69
surrogate_hash     62be141b213605dad6ccca29e9438b908af4bfa88927a5bb44ab35560b64a713
estimator          leading_edge  (preregistration 172b82058460bdb0)
amendments         AMD-001 553e2dfa… / AMD-002 9742eab8…
readiness          IS-01..06 全 PASS
reproducibility    RP-01..04 全 PASS，容忍值全 0.0
parameter_ranges   26 項
```

凍結前**重新執行**兩份稽核而非採信既有 artifact，並要求工作區乾淨
（NOTE-039）。lock 內含 claim boundary：**這是 pre-calibration 模型，
不宣稱接近真實分佈**。

### 已知缺陷 → **已裁決並更正（ERR-001，2026-08-29）**

首版 freeze 在 freeze 行程內重新探測相依版本，而該行程沒有 `set_variant`，
mitransient 因此被記成 `"unavailable"` —— 但被凍結的 run 實際用的是 **1.3.0**。
NOTE-039 留下的兩條路已裁決：**保留 lock 並登記勘誤**，見下一節。

---

## ERR-001 —— 勘誤層（2026-08-29，NOTE-040）

**`freeze/initial_simulation.lock.json` 不刪除、不重凍，一個位元都沒動。**
磁碟上那一份仍記著 `"unavailable"`，`payload_hash` 仍是 `dc15c954…`。

```
erratum        ERR-001   363b06c1de6ec9613178074689dc23d2da443ac9657b62740023b141cbed4c15
target lock    initial_simulation  dc15c9543a3aecac…
field          environment.mitransient
recorded       "unavailable"   ->   corrected "1.3.0"
evidence       4 sources, all re-hashed and bound to the frozen run
scientific_state_changed  false
```

`scientific_state_changed=false` 是可檢查的而非宣稱的：`environment` 不進
`scene_hash`、不進 `parameter_set_hash`、不進 `surrogate_hash`，也不參與
IS-01..06 或 RP-01..04 的任何一項。更正前後 RP-03 仍是 12/12 bitwise 相同。

**證據綁回「被凍結的那一次 run」，不只是「某一次 run」。** 每一條證據帶
`binds` 斷言：manifest 的 `content_hash` 必須等於 lock 的 `scene_hash`、
`run_identity_hash` 必須相同、scenario 層級另有兩條 `scenario_hash` 綁定。
少了 binds，任何一份用 1.3.0 跑出來的 manifest 都能當證據。

**formal 讀取一律走 `locks resolve` / `load_formal_lock()`**，每次載入重驗
original lock + erratum + source evidence 三者。lock 身分永遠是原始
`payload_hash`；勘誤**不產生**新的 lock hash。

```powershell
py -3.10 -m pcmef.cli erratum status
py -3.10 -m pcmef.cli locks resolve --lock initial_simulation `
    --field environment.mitransient
  payload_hash : dc15c9543a3aecac…   (original, unchanged)
  original  environment.mitransient = 'unavailable'
  resolved  environment.mitransient = '1.3.0'
```

**可更正範圍是成對鎖。** 白名單目前只有 `environment.*`；禁區另涵蓋
parameter / estimator / seed / scene / config，且**禁區檢查先跑、完全不看
白名單** —— 放寬白名單是一行改動，那一行不該足以讓改參數偽裝成修筆誤。
`tests/unit/test_errata.py` **31 條**，含八種禁區路徑的 parametrized 拒絕。

---

## Phase E —— 校準**預註冊已凍結**，校準本身未開始（2026-08-29，NOTE-041）

| 階段 | 狀態 | 真正的 blocker |
|---|---|---|
| E0 校準預註冊 | **已凍結**（CAL-PREREG-002） | — |
| E0.5 stage 0 可辨識性探測 | **已執行，需裁決** | 見「Stage 0」一節的五項發現 |
| E0.6 AMD-004 / CAL-PREREG-003 | **AMD-004 已擬定未凍；CAL-PREREG-003 未撰寫** | stage 0 重跑與凍結都擋在這裡 |
| E calibration | **未開始，且目前 NOT_READY** | stage 0 未凍結；wall-clock 成本未裁決 |
| F 校準後重現 | 未開始 | 依賴 E |
| G metric/E1 前置 lock | 未開始 | `metric_config` / `e1_scientific_rule` 未凍；依賴 E |

**操作版本是 CAL-PREREG-002。** CAL-PREREG-001 保留但**不得執行** ——
它記錄的 v0.1.0 協定帶有 AMD-003 所列的三項缺陷（見下一節）。

```
CAL-PREREG-002   e7e2a298cfa364eb210ac56a414d72183e33a7f4b6974867fe06cb20482db99e
protocol_hash    c6a0de86e866cc0dbd426e77fe407d9659f46fc8888f2955e02820abd6a2316d
code_version     6370482382d81ca422b64d612f0b0b4ccb05515c   （工作區乾淨）
supersedes       CAL-PREREG-001  a17dd93982e496b7…（protocol 77365cc30413d4ea…）
amendments       AMD-003  612dabf1b323a730…
calibration_set  4bda77f6412afbaa…      parameter_set   3bd65b50264413e6…
initial lock     dc15c9543a3aecac…      ERR-001         363b06c1de6ec961…
bounds_hash      c71c39995d7a64ab…      optimizer seed  20260829
heldout access   0                      calibration access  0
CP-01..CP-16     全 PASS
```

### 目標函數

`J = Σ_{c,f} w[c,f] · NW(real[c,f], sim[c,f])`，逐 class × feature 共 16 項。
`NW = W1 / s_f`，`s_f` 為 **calibration split 真實值**的 pooled IQR，
**只算一次並凍結**（逐階段重算會讓 optimizer 靠放大模擬離散度稀釋誤差）。
權重事前固定為 **UNIFORM = 1.0**。

**四類 real class mean 不得出現在 `pcmef/` 或預註冊檔內**，CP-04 逐行掃描。
每階段只最佳化自己那一組項，但 16 項全部記錄 —— 否則「修好 signal、
悄悄弄壞 distance」不會有人看見（回歸容忍值 0.10）。

### 五個階段與其解耦依據

| 階段 | 參數 | 解耦依據 | 性質 |
|---|---|---|---|
| 1 Ambient | 2 | ambient pass 的 VCSEL 是關的；實測 irradiance 1.0→4.0 時 ambient 3.603355→3.603355 | **精確** |
| 2 Signal scale | 2 | 增益作用在 estimator **之後**；S2 實測距離變化 0.0 | **精確** |
| 3 幾何/表面/箔片 | 7 | **Empty 沒有介質** —— `build_scene_dict()` 只在 `medium_preset != "empty"` 時建介質，故只用 Empty 擬合 | **程式碼可驗證** |
| 4 參與介質 | 4 | 幾何已由 Empty 釘死；三類各有自己的密度與 albedo，無交叉項 | 結構性 |
| 5 sigma 映射 | 3 | 波形已固定，只擬合由波形算 sigma 的映射 | 結構性 |

**stage 0 可辨識性探測**（純模擬，不碰真實資料）先於全部擬合：
leverage 以 **σ_MC**（模擬自身的種子間標準差，8 組種子）為單位，
掃過整個登記範圍造成的變化 < **K = 10** 者**gauge-fix 而非擬合**，
落在 [5, 20] 者停止等待裁決。這是 NOTE-032 的教訓 ——
`_ROOM_LIGHT_RATIO` 比例拉 25 倍、總能量只變 0.058%，
那是「可以 fit、但 fit 出來由雜訊決定」。

> v0.1.0 的分母原為 s_f，而 s_f 需要先讀 calibration partition ——
> 一個宣稱 simulation-only 的階段在原理上不可能執行。已由 AMD-003 更正，
> 見下一節。

**18 項進入擬合，8 項宣告不擬合**：`spp` / `resolution` / `temporal_bins` /
`max_depth` / `bounce_budget` / `_LEADING_MARGIN_BINS` 是**數值與離散化設定**
不是物理量（spp 調高只是變異變小，不是更像真實感測器）；
`foil_orientation` 只有一個朝向有實作；`distance_offset_mm` 是 CG-4 的 gauge。

### 順帶裁決的新簡併：CG-4_absolute_distance

`_FOIL_GAP_TO_BOTTLE_RATIO` 與 `distance_offset_mm` 都讓四類的 distance
**一起平移**，在 distance 位置上精確簡併 —— registry 的 CG-1/2/3 都沒涵蓋它。
依 CG-1/CG-2 同一套理由（固定沒有物理內容的那一個）裁決：
**固定 `distance_offset_mm = 0.0`，擬合 `_FOIL_GAP_TO_BOTTLE_RATIO`**。
一個非零的 distance offset 等於宣稱「光程算對了但讀數要平移」。

另記下一組**近似**簡併：`signal_energy_to_mcps` 與三個箔片振幅參數在
signal 位準上難以區分，唯一區分來自 distance 通道（post-hoc 增益不移動
distance，物理振幅則經 LEADING_EDGE 的 range walk 移動它）。這個區分**很弱**，
因此三者能否進入擬合交由 stage 0 的門檻決定，不是假設。

### optimizer

**單一求解路徑**：每個階段一律 `differential_evolution`
（`seed=20260829`、`init=sobol`、`popsize=15`、`maxiter=100`、
**`polish=false`**，polish 走 L-BFGS-B、對雜訊目標取數值梯度沒有意義）。
初始族群第 0 個個體**強制**為 lock 的 initial 值，
因此「資料沒有要求任何改變」是可達成的結局。

預算由公式導出並由程式強制（`EvaluationBudget` 在第 budget+1 次評估中止）：

```
P(N) = 2**ceil(log2(popsize*N))     per_restart = P(N)*(maxiter+1)
per_stage = restarts * per_restart
```

| 階段 | 維度 | P | /restart | /stage |
|---|---|---|---|---|
| AMBIENT | 2 | 32 | 3,232 | 9,696 |
| SIGNAL_SCALE | 2 | 32 | 3,232 | 9,696 |
| GEOMETRY_SURFACE_FOIL | 7 | 128 | 12,928 | 38,784 |
| PARTICIPATING_MEDIA | **6** | 128 | 12,928 | 38,784 |
| SENSOR_SURROGATE | 3 | 64 | 6,464 | 19,392 |
| **合計** | **20** | | | **116,352** |

約 65 小時單執行緒（每次評估約 2 秒）。這個成本寫進協定，是為了讓
「預算不夠用」在開始之前就被看見。`PARTICIPATING_MEDIA` 是 6 維而非 4 維，
因為 `_ALBEDO_BY_PRESET` 是三個 class-specific 純量。

同階段內全部評估共用同一組模擬種子（CRN，1001/1002/1042/1004），
收斂後另以一組**驗證種子**（2001/2002/2042/2004）重算一次，
檢出種子專屬過擬合。重啟固定三次、種子由 `seed + 1000k` 決定。
平手（相對差 < 1e-3）取**在正規化邊界空間中離 initial 值最近**的那一組 ——
資料分不出來的時候就不要動。

**NOT_CONVERGED 是合法結局**，此時記錄 best-so-far 但
`calibrated_simulation` 不得凍結。不得放寬 tol、邊界或權重重跑。

### 驗證器上線第一次跑抓到的四個缺口

| 缺口 | 事實 |
|---|---|
| `distance_offset_mm` 被歸類兩次 | 同時在 stage 3 與 not_fitted，會被 fit 兩次 |
| 守衛清單自己含有 real class mean | CP-04 掃到自己的定義，若不處理會被整份停用 |
| artifact 欄位名混入中文說明 | 欄位清單同時是比對對象，混入說明後比對失去意義 |
| `_as_floats()` 用 `float(v)` 救字串 | `float("1.0e6")` 會成功，於是 lock 內三個**字串**上界被安靜接受，`declared_numeric_interpretations` 形同虛設 |

最後一項是 lock 內的既有事實：`ambient_energy_to_mcps` /
`signal_energy_to_mcps` / `sigma_width_to_mm` 的上界在
`parameter_registry.yaml` 寫成 `1.0e6`，而 YAML 1.1 需要 `1.0e+6` 才解析為
float。**該值已凍進 lock，且 `parameter_ranges` 在勘誤的禁區內**（改它就是
改實驗本身），因此只能由預註冊逐項宣告數值解讀，並要求解讀與凍結字面值
表示同一個十進位數。

### 本次**沒有**做的事

- 未執行 stage 0，未執行任何一階段的擬合
- 未讀取 calibration partition 的任何數值（`raw_data_hash` 留待執行當下計算）
- 未開啟 held-out（access count 仍為 0）
- 未凍結 `calibrated_simulation` / `metric_config` / `e1_scientific_rule`

---

## E1 已完成 —— `E1_SCIENTIFIC_PASS`，但**通過的內容很窄**（2026-08-30，AMD-005）

```
outputs/e1/e1_final_report.json
outcome E1_SCIENTIFIC_PASS      claim_mode tof_physics_calibrated
FORMAL_E1_FINAL 開啟 1 次（112 筆），發生在六個 gate lock 全部凍結之後
heldout_access_count 1          probe_access_count 0
```

### 三條凍結判準都過了

| 條件 | 結果 |
|---|---|
| macro-mean Delta CI 下界 > 0 | **PASS** — 167.23（點估計 167.68，B=10000，seed 20260826，28 個 scenario） |
| 每個 feature 的 macro Delta ≥ 0 | **PASS** |
| distance_trend_consistency | **NOT_APPLICABLE**（見下） |

### 但這三條加起來能宣稱的，比 `tof_physics_calibrated` 這個標籤少很多

**16 格裡只有 4 格改善，而且全部是同一個 feature。**

| feature | macro Delta | 讀法 |
|---|---|---|
| `ambient_rate_mcps` | **+670.71** | 四類全部大幅改善（908→1.6、783→0.13、377→3.0、619→0.17） |
| `distance_mm` | 0.000 | **恰為 0** |
| `signal_rate_mcps` | 0.000 | **恰為 0** |
| `sigma_like` | 0.000 | **恰為 0** |

那三個 0 **不是「校準後沒有變差」，是「根本沒有變」**：它們的參數更新
被抑制了，所以 Initial 與 Calibrated 在這些通道上是**同一個模擬器**，
逐位元相同。第二條 PASS 條件（`>= 0`）因此在四分之三的 feature 上是
空過，不是證據。

**校準後 `signal_rate_mcps` 仍然差得離譜**：

```
Water-filled  NW 418654  (W1 516787 MCPS)      Empty  NW 87462  (W1 107963)
Bubbly        NW  66408  (W1  81974)           Misty  NW 49811  (W1  61486)
```

第三條（趨勢）是 `NOT_APPLICABLE`：FORMAL_E1_FINAL 只有 baseline 一個分層，
±offset 序列來自 CAL-PREREG-003 明列禁用的獨立採集。artifact 內
`must_not_be_read_as` 已具名記下「這不是通過，是沒有證據」。

### 因此論文可以寫什麼、不可以寫什麼

`e1_outcome.lock` 依凍結對照表寫下 `claim_mode: tof_physics_calibrated`。
**那個標籤對本結果過強**，不得原樣搬進論文。可以宣稱的是：

> 在四個 ToF 特徵中，**ambient 通道**經校準後與真實感測器的分佈距離
> 顯著縮小（macro Delta 670.7，成對重抽 CI 下界 167.2 > 0）；
> distance、signal 與 sigma-like 三個通道的參數更新因獨立證據不支持而被
> **抑制**，維持凍結初值，其分佈距離與 initial model 完全相同。
> signal 通道的殘差仍達 NW 5x10^4 - 4x10^5 量級。

不得宣稱整個 ToF 模擬器已 physics-calibrated；不得把三個 0 說成「無退步」；
不得把趨勢的 NOT_APPLICABLE 說成趨勢一致。

### E1 結論的正式限縮

> **E1 證明的是：Ambient Rate 通道經校準後顯著改善。**
> 其餘三個 feature（distance_mm、signal_rate_mcps、sigma_like）維持
> **initial physics-constrained state** —— 它們的參數更新被獨立證據否決，
> 因此模擬器在這三個通道上就是 `initial_simulation.lock` 凍結的那個模型，
> 既沒有被校準，也沒有退步。

---

## Cross-stage observable dependency limitation（2026-08-31）

**這是本次校準最重要的方法論發現，而且它不是實作缺陷。**

CAL-PREREG-003 的分階段設計假設每個階段可以在自己宣告的 observable 上
獨立擬合。實跑之後這個假設在 stage 1 不成立：

```
stage 1 宣告的 observable：Empty|{distance_mm, signal_rate_mcps, sigma_like}
stage 1 可動的參數：       sensor.fov_deg（唯一 admitted）
```

`Empty|signal_rate_mcps` 在起始點的 NW 是 **202,906**，另外兩項加起來
**不到 18**。也就是說 stage 1 的目標函數 99.99% 是由 signal 通道決定的 ——
而 signal 通道的**增益**（`signal_energy_to_mcps`）是 **stage 4** 的參數，
stage 1 完全動不到它。

於是 stage 1 面對的是一個它結構上解不了的問題：它被要求最小化一個
主要由「別的階段才能修的量綱錯誤」構成的目標。

### Stage 1 的 FOV candidate 是什麼、不是什麼

optimizer 在這個地形上做了它唯一能做的事：找一個讓**模擬訊號塌掉**的
fov（57.79°），藉此壓低那個 5 個數量級的殘差。三次重啟都收斂到同一點
（相對全距 1.6e-4），所以這不是搜尋失敗。

> **`sensor.fov_deg ≈ 57.79` 是一個 dependency-confounded harmful update：**
> 它是為了補償「另一個階段才能修的增益錯誤」而產生的，
> 且被 verification-seed guard（劣化 8.26）與 regression guard（+28.5%）
> 兩道獨立證據攔下。
>
> **不得**把它解讀為「FOV 本身不可校準」或「FOV 沒有可辨識性」。
> stage 0 實測 `sensor.fov_deg` 的 own-stage leverage **通過**了入場判定；
> 它可辨識。被拒絕的是**這一個特定的候選值**，理由是它在當前的
> 階段相依結構下有害，不是理由是這個參數不可校準。

### 這個限制的範圍

| | |
|---|---|
| 受影響 | stage 1（FOV）、stage 4（signal）、stage 5（sigma）—— 三者都被抑制 |
| 未受影響 | stage 3（ambient）。ambient 通道的量綱在起始點就已經接近，且它的 seed 雜訊只有 sd/mean ≈ 0.011，因此它在自己的階段裡是可解的 |
| 根因 | 兩件事相乘：(a) 階段順序把 mapping 增益放在 scene 之後；(b) spp=16 下 signal / sigma 通道的 Monte-Carlo 雜訊達 sd/mean 0.17-0.67 |

要解除它，必須同時處理階段相依與模擬解析度 —— 兩者都會改動
`initial_simulation.lock` 或 CAL-PREREG-003 的階段結構，屬 amendment 範圍。
**本階段（2/3 Perception）不處理，也不得因為模型表現而回頭改它。**

---

## Formal Calibration（AMD-005 之後）—— `CALIBRATION_COMPLETE`（2026-08-30）

五個階段全部走完，1.89 小時，17488/33936 次評估，**0 次失敗評估**。

| stage | outcome | verification 劣化 | regression |
|---|---|---|---|
| SCENE_GEOMETRY_SURFACE_FOIL | **UPDATE_INHIBITED** | 8.26 | +28.5% |
| SCENE_PARTICIPATING_MEDIA | NO_FREE_PARAMETERS | — | — |
| MAPPING_AMBIENT | **CONVERGED** | **0.0071** | 0 |
| MAPPING_SIGNAL | **UPDATE_INHIBITED** | 0.329 | ~0 |
| MAPPING_SIGMA | **UPDATE_INHIBITED** | 1.505 | 0 |

七個參數只有兩個真的動了：

```
ambient_energy_to_mcps    1    -> 0.00557953
ambient_jitter_relative   0.05 -> 0.0636823
其餘五個保留凍結初值（sensor.fov_deg 45、signal_energy_to_mcps 1、
noise_relative_sigma 0.01、sigma_width_to_mm 1、sigma_multipath_weight 1）
```

**這個分佈本身就是那句假設的檢定。** 唯一 seed 穩定的通道
（ambient，stage 0 記錄的 sd/mean ≈ 0.011）校準得起來、而且跨種子成立；
三個 sd/mean 落在 0.17-0.67 的通道校準不起來，抑制規則把它們留在原地。
根因是 spp=16 下 Monte Carlo 雜訊蓋過了參數效應 —— 那是
`initial_simulation.lock` 的解析度問題，不是 optimizer 的問題
（三次重啟每次都收斂，相對全距 1.6e-4）。

> **報告用字的邊界**：`UPDATE_INHIBITED` 是**功能性啟發**的工程控制規則 ——
> 獨立證據指出更新會傷害系統層級穩定度時，抑制該次狀態變更。
> **不主張、不示範、也不構成任何生物神經抑制機制的證據。**

### 已知的報告缺陷（不影響判定）

`parameter_delta.boundary_report` 把 `ambient_energy_to_mcps`、
`sigma_width_to_mm`、`signal_energy_to_mcps` 標成 AT BOUNDARY，那是
**假陽性**。預註冊的規則是「距邊界 < 1% 範圍寬」，而這三個參數的登記範圍是
[1e-6, 1e6]，1% 就是 10000 —— 於是任何小於 10000 的值都會被標記。
規則是凍結的、實作是照字面做的，因此不改；但三者實際上都離邊界很遠。

---

## Formal Calibration（AMD-005 之前）—— 曾停在 stage 1（2026-08-30，已由 AMD-005 取代）

**五個階段只跑完第一個就依凍結規則停下。這不是失敗，是預註冊的守衛在做它該做的事。**

```
outputs/calibration/calibration_report.json
code_version c1f6f9d   runner calibration_formal v0.1.0
prereg CAL-PREREG-003 fb9638d7…   stage0 CAL-STAGE0-001 810222 80…
s_f CAL-SF-001 8e71171c…（載入，未重算）
raw_calibration_data_hash 926eef72…（每次載入重算並斷言相符）
calibration access ledger 3        heldout access 0
wall clock 0.22 h                  720 / 4848 次評估，0 次失敗
```

### stage 1 `SCENE_GEOMETRY_SURFACE_FOIL`：optimizer 沒問題，結果不可用

optimizer 本身乾淨：三次重啟**全部收斂**到 `sensor.fov_deg` ≈ 57.79，
目標值相對全距 1.6e-4（遠低於 0.05，非 MULTIMODAL），失敗評估 0 次，
只用掉 720 次預算中的 4848 次。平手規則正確地在三個統計上分不出來的解裡
取了離凍結初值最近的那一個（restart 2）。

**但兩道凍結守衛同時破了：**

| 守衛 | 量到的值 | 容忍度 | 判定 |
|---|---|---|---|
| `verification_seeds` | 相對劣化 **8.26**（826%） | 0.10 | **SEED_OVERFIT** |
| `regression_guard` | 未最佳化項 NW 總和 **+28.5%** | 0.10 | **SIDE_EFFECT_REGRESSION** |

`outcome` 欄只有一格，記的是 `SEED_OVERFIT`；另一道在
`stage_summary.regression_guard.violated = true`。
（v0.1.0 的 `flags` 當時沒有兩道都記，事後已修，但**不重跑**——
artifact 就是它產生時的樣子，改它比留著一個不完整的標籤更糟。）

**SEED_OVERFIT 的意思**：同一組參數下，CRN 種子給 J = 11,210，
verification 種子給 J = 103,797。那個 94.5% 的「改善」
（202,923 → 11,210）整個是 CRN 種子專屬的。

**SIDE_EFFECT_REGRESSION 的意思**：stage 1 只擬合 Empty，代價由另外三類付：

```
Misty|signal_rate_mcps    +366%      Bubbly|signal_rate_mcps  +180%
Bubbly|ambient_rate_mcps   +58%      Misty|ambient_rate_mcps   +24%
```

### 為什麼會這樣（診斷，不是改協定的提案）

兩件已凍結的事實相乘：

1. **`signal_rate_mcps` 在 spp=16 下大半是雜訊。** CAL-STAGE0-001 自己記著
   它八組種子的 sd/mean 是 **0.24–0.67**（`distance_mm` 是 ~0.000）。
   實測同一 fov 換種子，active cube 總能量差 2–6 倍，峰值 bin 從 5 跳到 64。
2. **它同時把目標函數吃掉四個數量級。** `signal_energy_to_mcps` 還停在
   placeholder 1.0 —— 那是 **stage 4** 的參數。於是 stage 1 的 J 裡
   `Empty|signal_rate_mcps` ≈ 202,906，其餘兩項加起來 < 18。

stage 1 因此在最佳化一個「它修不了、而且大半是雜訊」的通道，
唯一能壓低它的辦法就是找一個讓模擬訊號塌掉的 fov —— 那正是種子專屬的。
CAL-PREREG-003 stage 4 的 `known_residual_confound` already 預告了增益混淆，
但沒有預料到它會在 stage 1 就把整個目標函數蓋掉。

### 現在不得做的事

- **不得凍結 `calibrated_simulation.lock`。** 只產出了 candidate
  （`eligible_to_freeze: false`）。
- 不得為了讓它過而放寬 tol / 容忍度 / 邊界，或改用 best-so-far。
- 不得繼續 stage 2–5：後面每一階段的前提都是 stage 1 已經釘住場景。
- 不得開 held-out。

### 要往下走，只有 amendment 一條路

候選方向（**都必須經 amendment，且都不是這次執行可以自己決定的**）：
提高 spp 讓 signal 通道的 sigma_MC 降到可擬合、
把 mapping 增益移到 scene 階段之前、
或把 `signal_rate_mcps` 移出 stage 1 的 declared observables。
三者都會改變已凍結的實驗設計。

---

## ~~Stage 0 —— 結論為 `STAGE0_ADJUDICATION_REQUIRED`~~ → **已作廢（2026-08-30）**

> **本節記錄的是 AMD-004 之前那一次 stage 0 的結果，已不再有效。**
> stage 0 已依 AMD-004 重跑並凍結為 CAL-STAGE0-001
> （payload hash `810222803e6555e3…`，outcome `STAGE0_COMPLETE`，
> 七個 admitted 參數）。下面的判定與待辦**全部過期**，保留純為血緣可稽核。


**stage 0 已完整跑過，但沒有凍結，而且不應該凍結。** 它的用途正是在燒掉
116,352 次評估之前把設計缺陷逼出來，而它一次逼出了五個。

```
outputs/calibration/stage_0/stage0_identifiability.json
prereg  CAL-PREREG-002 e7e2a298…   protocol c6a0de86…   bounds c71c3999…
initial lock dc15c954…             parameter_set 3bd65b50…
spp 16 / 64x64 / 128 bins / 500 samples / 140 scenario renders
mitsuba 3.8.0  mitransient 1.3.0   estimator leading_edge
calibration access 0               heldout access 0
```

| 分類 | 數 | 成員 |
|---|---|---|
| ADMITTED | 14 | 四個 `_FOIL_*`、`_BOTTLE_SURFACE_ALPHA`、`sensor.fov_deg`、`light.cutoff_angle_deg`、三個密度、`ambient_energy_to_mcps`、`signal_energy_to_mcps`、`sigma_width_to_mm`、`sigma_multipath_weight` |
| GAUGE_FIXED | 1 | `_ALBEDO_BY_PRESET.water`（2.708） |
| BORDERLINE | 3 | `_ALBEDO_BY_PRESET.bubbly` 7.835、`.misty` 8.812、`sigma_snr_weight` 17.16 |
| UNDETERMINED | 2 | `ambient_jitter_relative`、`noise_relative_sigma` |
| NOT_FITTED | 8 | 與預註冊相同 |

### 可重現性：同 seeds／settings 下**逐位元**相同

同一組凍結設定與種子跑兩次（run1 / run2，各 140 次算圖），逐項比對：

| 比對項目 | 結果 |
|---|---|
| σ_MC（32 個 observable） | **32/32 逐位元相同**，最大絕對差 `0.0` |
| CRN baseline（32 個） | **32/32 逐位元相同** |
| 完整 leverage 矩陣（20 參數 × 32 observable） | **576/576 逐位元相同** |
| 每參數 `max_leverage` | **20/20 逐位元相同** |
| 每參數 outcome | **20/20 相同** |
| 共線性餘弦（39 對） | **39/39 逐位元相同** |
| 分類計數 | 14 / 1 / 3 / 2 / 8，兩次相同 |
| outcome | 兩次皆 `STAGE0_ADJUDICATION_REQUIRED` |

**一致不等於可以凍結。** 可重現性證明的是「這個判定不是種子運氣」，
不是「這個判定沒有問題」。結論仍是需要裁決，因此**未凍結**。

### 五個必須在 first access 之前處理的發現

**1. Ambient 不是與 active pass 無關 —— stage 順序因此不成立。**
CAL-PREREG-002 stage 1 寫「Ambient 只由 AMBIENT_ONLY pass 取得，因此它與
active pass 的**任何**參數無關」。那句話的證據（VCSEL 1.0→4.0 時 ambient
完全不變）只證明了它與**VCSEL 發射參數**無關。AMBIENT_ONLY pass 是**同一個
場景**，室內光照樣打在箔片、瓶壁與介質上。實測 scene/media 參數在 ambient
通道上的槓桿是 **69–129 σ_MC**，而且對多數參數而言那是它們**最大**的通道。
先擬合並凍結 ambient 映射，等於把一個增益釘在一個後面還會被移動兩個數量級
（以其自身雜訊為單位）的觀測量上。

**2. Signal 同樣受後續 scene 參數強烈影響，只是被巨大的雜訊底線蓋住了。**
（本輪依裁決要求補做的稽核。）以**相對變化**看，scene 參數掃過登記範圍時
signal median 變動 `sensor.fov_deg` **1349.7%**、`medium.bubble_density`
**1119.1%**、`_FOIL_GAP_TO_BOTTLE_RATIO` 330.8%、`light.cutoff_angle_deg`
191.4%、`medium.turbidity` 171.5%、`_FOIL_SURFACE_ALPHA` 91.3%、
`_FOIL_REFLECTANCE_940NM` 85.9%。但換算成 σ_MC 只有 0.0006–16.6，因為

| 通道 | σ_MC / |值| |
|---|---|
| `distance_mm` median | **4.6e-4** |
| `ambient_rate_mcps` median | 1.1e-2 |
| `sigma_like` median | 0.12 – 0.52 |
| **`signal_rate_mcps` median** | **0.35 – 1.25** |

也就是說在凍結的 spp = 16 下，**Signal 觀測量的種子間離散度與它自己的值同量級**。
兩件事同時成立：順序必須改，而且 Signal 通道在目前取樣密度下幾乎不可用。

**3. 入場判定會靠「該階段從不最佳化的通道」發出入場券。**
`_FOIL_SIZE_TO_DIAMETER_RATIO` 以 Water-filled 的 ambient 取得槓桿 **101.7**
而 ADMITTED，但它在 stage 3 自己宣告的通道（Empty 的 distance/signal/sigma）
上只有 **0.0006**。預註冊的字面規則是「對全部 observable 取 max」，
所以這個判定合乎規則 —— 規則本身要修。

**4. CG-5：三組 density ↔ albedo 近乎精確簡併**（CG-1~CG-4 都沒涵蓋）。

| 對 | \|cos\| |
|---|---|
| `medium.bubble_density` ↔ `_ALBEDO_BY_PRESET.bubbly` | **0.9995** |
| `medium.turbidity` ↔ `_ALBEDO_BY_PRESET.water` | **0.9987** |
| `medium.mist_density` ↔ `_ALBEDO_BY_PRESET.misty` | **0.9980** |

另有 stage 3 內的四對（三個 `_FOIL_*` 互相 0.9945–0.9986、
`_BOTTLE_SURFACE_ALPHA` ↔ `light.cutoff_angle_deg` 0.9948）與 stage 5 的
`sigma_multipath_weight` ↔ `sigma_snr_weight` 0.9959。**只回報，未改參數群。**

**5. 兩個登記界線在物理上不可行。**
`ambient_jitter_relative` 下界 0.0 被 `map_ambient_rate` 的 `jitter <= 0`
斷言拒絕；`noise_relative_sigma` 上界 0.5 使距離映射得到 **−4.915 mm**。
兩個界線都在 `initial_simulation.lock` 的 `parameter_ranges` 內，屬勘誤禁區，
**不得縮小**。

### σ_MC ≈ 0 的處理

協定對此**沒有規定**，這本身是缺口。實作一律**不回傳 inf**：本輪
`sigma_mc_degenerate_observables` 為**空**，32 個 observable 的 σ_MC 全部大於 0，
因此該分支未被觸發。缺口已回報並在 AMD-004 補上三個決定性分支（NOTE-044）。

### 成本：與宣稱相差 30–42 倍

CAL-PREREG-002 記「每次評估約 2 秒 → 116,352 次約 65 小時」。
實測同一組凍結設定：孤立探針 **60.8 s/次**、stage 0 那 140 次算圖平均 **84 s/次**。

| 依據 | 單次 | 116,352 次 | 相對宣稱 |
|---|---|---|---|
| 孤立探針 | 60.8 s | **1,966 h ≈ 82 天** | 30.2× |
| stage 0 實跑 | 84 s | 2,715 h ≈ 113 天 | 41.8× |

**未擅自調 spp / maxiter / restarts 把數字弄小** —— 那是拿實驗解析度去遷就時程。
登記為 stage 1 的具名 blocker。

---

## ~~AMD-004 —— 尚未凍結~~ → **已凍結（2026-08-30）**

> AMD-004 payload hash `f298816d97e88b3f…`，其後 CAL-PREREG-003、
> stage 0 重跑、正式校準與 E1 均已完成。本節的「待辦」欄位過期。


`configs/amendments/AMD-004.yaml` 已寫好，涵蓋八項契約變更：
stage 重排（physical scene/media → measurement mappings）、
入場範圍改為該階段宣告的通道、CG-5 裁決、σ_MC 三分支、
`ambient_jitter_relative` 定義域、可行域推導規則、
`sigma_snr_weight` 的 borderline 次級判準（S0-B1..B5，**先註冊後跑**）、
以及 wall-clock 成本登記。

**CG-5 裁決**：固定三個 albedo 為 **CONVENTION**（water 0.60 / bubbly 0.92 /
misty 0.88），擬合三個密度。理由與 CG-1／CG-2 同一套 —— 固定**provenance
較弱**的那一個，不是固定「比較好 fit」的那一個：albedo 在 SRC-PLAN 與
SRC-HANDOFF 都查不到一手來源（RECONSTRUCTED），而密度被 SRC-SAI §9 明列為
`<validation-frozen range>`，本來就是 validation 該定的量。
搜尋空間 20 → **17** 維。

> **Claim boundary**：擬合出的密度只能稱為**校準後模擬器的 effective-medium
> 消光參數**，**不得**宣稱為真實的絕對濃度／濁度／數量密度 —— 它吸收了被固定的
> albedo 的誤差。與 CG-1「只能宣稱 σt」是同一條界線。
> 推翻本裁決需要 **identifiability evidence**（一個能分開密度與 albedo 的
> observable），不是偏好。

**為什麼還沒凍結**：amendment 一旦凍結即不可覆寫，而 AMD-004 的
`changed_contracts` 描述的是 **CAL-PREREG-003 的內容**。CAL-PREREG-003 尚未
撰寫，先凍 AMD-004 等於把一份還沒寫出來的協定的描述鎖死。
AMD-003 → CAL-PREREG-002 的既有順序是「amendment 先凍、prereg 後凍」，
但那次的 prereg 內容在凍 amendment 時已經定稿。

---

## AMD-003 —— CAL-PREREG-001 的三項 pre-execution 缺陷（2026-08-30，NOTE-042）

全部在**第一次讀取 calibration partition 之前**處理完畢。
`AMD-003` payload hash `612dabf1b323a730…`，九條契約變更。

| # | 缺陷 | 裁決 |
|---|---|---|
| **P0-1** | stage 0 宣告 `reads_calibration_partition: false`，判準卻是 `\|Δo\|/s_f`，而 s_f 是 calibration split 真實值的 pooled IQR —— **照字面執行不可能** | 取 **A**：分母改為 σ_MC，stage 0 真的只用模擬 |
| **P0-2** | `max_evaluations_per_stage: 3000` 與 `maxiter: 200` **在任何階段都不可能同時成立**；實測低估 **8.6–25.7 倍** | 預算改由公式導出並由程式強制 |
| **P1-3** | stage 3 的 Empty 措辭與 NOTE-029 描述**缺陷**的句子雷同 | 稽核確認**程式正確**，僅更正措辭 |

### P0-1：為什麼選 A 不選 B

| | 選 B（把 stage 0 正名為會讀資料的階段）的後果 |
|---|---|
| **Leakage** | 「要擬合哪些參數」會由真實資料決定 —— 一個位於所有階段上游的**資料相依模型選擇**。現有守衛攔不到它：verification seeds 查的是種子過擬合，不是選擇偏誤。而且會把 first access 提前卻換不到科學上的好處。 |
| **Identifiability** | stage 0 要問的是「optimizer 看不看得見這個參數」。對 MC 模擬器而言雜訊底線是它自己的種子間離散度，不是真實資料的 IQR。s_f 回答的是另一個問題（對 J 重不重要）—— 適合當診斷，不適合當入場券。 |

σ_MC = 在凍結 initial 值上以 8 組獨立種子各算一次的樣本標準差。
K = 10（約 10√8 ≈ 28 個平均值標準誤）。共線性檢查同樣改以 σ_MC 逐分量正規化
—— 原本對不同單位的分量取餘弦，那個數字本來就沒定義好。
s_f 的問題保留為 stage 1 開始時計算、**明文禁止 gating** 的診斷。

### P0-2：實測數字

| N | 族群 P | nfev（單次重啟） | 相對宣稱的 3000 |
|---|---|---|---|
| 2 | 32 | 4,192 | 1.4× |
| 3 | 64 | 11,584 | 3.9× |
| 4 | 64 | 12,864 | 4.3× |
| 7 | 128 | **25,728** | **8.6×**（三次重啟 25.7×） |

根因：`popsize` 是**乘數**不是族群大小，且 `init='sobol'` 會把族群補到 2 的冪。
順帶消掉兩個相關歧義 —— `scalar_stages.applies_to` 列的是**參數**而非階段
（整條路徑移除），`_ALBEDO_BY_PRESET` 佔 1 還是 3 個維度
（新增 `dimension_expansion` 明文宣告）。

### P1-3：Empty 拓樸稽核結論

```
bottle_interior 無條件建立，四類皆然（只有 interior["interior"] = medium 是條件式）
_DENSITY_KEY_BY_PRESET 沒有 "empty" 鍵 -> _medium_dict() 回 None
_INTERIOR_BASE_IOR["empty"] = "air"
實測：四類 r_in 26.5 mm / r_out 28.5 mm（壁厚 2.0 mm）
      Empty  int_ior=air  ext_ior=bk7  has_participating_medium=False
```

**Empty 是玻璃殼＋空氣＋玻璃殼，沒有回歸。** 不觸發 STOP，
`initial_simulation.lock` 不受影響。`tests/simulation/test_empty_topology.py`
逐次確認，不靠記憶。

### bounds 唯一性

`resolve_numeric_bounds()` 是**唯一**路徑：原生數值取自 lock，字串界線必須
有宣告解讀且 `float(凍結字面值)` **恰好等於**宣告值。20 個維度，
`bounds_resolution_hash = c71c39995d7a64ab…`，凍進 AMD-003 與 CAL-PREREG-002。
不存在第二套未凍結的 bounds。

### calibration access ledger

held-out 有 access count，calibration 沒有，於是「還沒讀」只是一句話。
新增 append-only `data/splits/calibration_access_ledger.json`，明訂什麼算
一次 access（讀 recording **數值**算；對 id 集合取雜湊不算）、
first access 在 stage 1 開始，以及順序規則。**目前為 0。**

---

## ~~已知的既有測試缺陷~~ → **已修正（2026-08-30，NOTE-043）**

`tests/simulation/test_simulation.py::test_surrogate_can_consume_the_rendered_transient`
先前在 **LLVM 在 PATH 上時失敗**，沒有 LLVM 時則靜默 skip —— 因此一直沒被發現。
已確認在 commit `2129153`（該輪工作之前）同樣失敗，非回歸。

```
CalibrationError: leading_edge defines its detection threshold in units of the
measured ambient noise level, so it requires a dedicated ambient pass (NOTE-034).
```

該測試只餵 active pass 就呼叫 `surrogate.observe()`，而 NOTE-034 已要求
Ambient 必須來自獨立 ambient pass、NOTE-037 選定的 LEADING_EDGE 門檻又以
ambient 雜訊為單位 —— 測試從那兩個決策之後就沒更新過。**拒絕是正確行為，
測試才是舊的。**

### 修法：接上本來就已經存在的 ambient pass

`render_transient()` 從 NOTE-034 起就**已經**把 ambient pass 寫進 manifest 的
`outputs.optical_transient_ambient`，只是這個測試沒有去讀它。因此修正是把
既有產物接上，**production surrogate 契約一個字都沒有放寬，也沒有恢復
任何 fallback**：

```python
ambient = np.load(outputs["optical_transient_ambient"])
observables = surrogate.observe(transient, axis, ambient_transient=ambient)
recording = TemporalModel(surrogate).generate_recording(..., ambient_transient=ambient)
```

另新增**成對測試** `test_the_production_surrogate_contract_still_refuses_an_active_only_call`：
拿掉 ambient pass 必須仍然拋出 `CalibrationError`。少了它，「修好測試」與
「放寬 production 契約」在測試結果上長得一模一樣 —— 兩者都會讓上一條變綠。

### 假綠燈已關閉：skip 現在可以被強制為 fail

**問題不在於這個測試錯了，而在於它錯了整整一輪都沒有人看得到。**
LLVM 不在 PATH 上時該檔全部算圖測試靜默 skip，pytest 的 exit code 仍是 0，
於是「測試沒過」與「測試沒跑」在判定上完全相同。

新增環境變數 `PCMEF_REQUIRE_SIMULATION=1`：設定後，缺相依不再是 skip
而是**明確失敗**，訊息直接指出 drjit 執行期動態載入 `LLVM-C.dll` 而套件不內含。
實測兩側都確認過，不是宣稱：

| 情境 | 範圍 | 結果 | exit code |
|---|---|---|---|
| LLVM 在 PATH，`PCMEF_REQUIRE_SIMULATION=1` | 全套 | **1681 passed，0 failed，0 skipped** | 0 |
| LLVM 在 PATH，`PCMEF_REQUIRE_SIMULATION=1` | `test_simulation.py` | 25 passed | 0 |
| LLVM **不在** PATH，`PCMEF_REQUIRE_SIMULATION=1` | `test_simulation.py` | **1 failed** + 11 skipped，訊息具名指出缺 `LLVM-C.dll` | **1** |

也就是說**修正後不再存在「一邊 fail、一邊靜默 skip」的不對稱**：
兩側現在是 pass 與 explicit-fail，而不是 fail 與 silence。

> **交接提醒**：跑全套測試時請**把 LLVM 放進 PATH 並設
> `PCMEF_REQUIRE_SIMULATION=1`**。
> ```powershell
> $env:PATH = "C:\Program Files\LLVM\bin;$env:PATH"
> $env:PCMEF_REQUIRE_SIMULATION = "1"
> py -3.10 -m pytest
> ```

---

---

## AMD-001：E1-G08 協定修訂（2026-08-27 深夜，NOTE-028）

**這是本專案第一次修改 gate 判準本身。** 記錄凍結於
`freeze/amendments/AMD-001.amendment.json`（payload_hash
`553e2dfa2eb1fcc5…`），不可覆寫。

### 起因

取得 rank-1 raw dataset evidence（560 筆 Edge Impulse export，280,000 列）後
發現原 E1-G08 存在**範疇錯誤**：它把三件可觀測性根本不同的事綁成同一個
`RESOLVED` 旗標。

| 事實 | 可否由資料驗證 |
|---|---|
| Sigma channel 語意（非測距值） | **可** —— 280,000 列直接檢定 |
| Sigma numeric scale（/65536） | **可** —— 量化關係檢定 |
| 歷史暫存器位址（0x18 / 0x1E） | **原理上不可** —— 匯出的是浮點數，看不到 I²C 位址 |

原 v1 的推論「0x1E 是 final range → sigma 不是 range → 排除 0x1E →
故為 0x18」有三個問題：排除檢定證成的是 channel semantics 而非位址身分；
候選集不窮盡（產生本 dataset 的採集腳本不在已知三份之中）；
且它把 rank-6 的死常數（0x18 只出現在**不使用 sigma** 的單參數程式）
升格為結論，正是 SRC-HANDOFF §8 禁止的低位階覆寫高位階。

原結論還與系統自己的紅線矛盾：NOTE-001 早就規定第四欄不得被稱為
「真實 VL53L0X internal Sigma」——既然不能這樣宣稱，把 E1 資格綁在
「知道它讀自哪個暫存器」上，從一開始就內部不一致。

### 修訂內容

Sigma provenance 由單一旗標改為**四個獨立 facet**：

| facet | 狀態 | 位階 | E1-G08 要求 |
|---|---|---|---|
| `channel_semantics` | **CONFIRMED** | 1 | **是** |
| `numeric_scale` = /65536 | **CONFIRMED** | 1 | **是** |
| `register_address` | **CONFLICT** | 5 | 否 |
| `original_acquisition_method` | **UNKNOWN** | 2 | 否 |

E1-G08 契約升至 **v2**：只要求前兩者 CONFIRMED。位址為 CONFLICT/UNKNOWN
**不影響判定，也不使 dataset 失效**。只有 `CONFIRMED` 能滿足 gate，
`RECONSTRUCTED` 刻意排除。

### 為什麼這不是「看到結果後改判準」

凍結時的前提證據是**當下實測**而非引述，且已寫進 amendment：

```
FW-02 PASS   heldout_access_count = 0
freeze/e1_outcome.lock.json 不存在
```

held-out 從未開啟、E1 沒有任何結果 —— 沒有可以被回頭迎合的數字。
這是預註冊修訂。**E1_four_feature_primary_ready 仍為 false，
held-out E1 仍 FORBIDDEN**，理由是 M2 場景保真度未完（見下節），
與 Sigma 無關。

### 順帶修掉的 timing 證據位階倒置

`timing_provenance.json` 原本**完全沒有收錄** dataset 自帶的
`interval_ms = 82.00001312`（rank-1），卻以偏移測試副產物 CSV 的
0.0624 s 當 baseline，於是把 rank-1 證據寫成「偏離 31%」。改正後：

| 值 | 定位 | 相對 baseline |
|---|---|---|
| **82.00001312 ms** | **rank-1 dataset provenance（baseline）** | — |
| 0.082 s | SRC-PLAN 記載值 | **−0.0%**（幾乎完全吻合） |
| 0.0624 s | 偏移測試副產物，**另一次採集** | −23.9% |
| 0.02 s | deployment 迴圈 sleep 設定 | −75.6% |

### 防止橡皮圖章

放寬 gate 最大的風險是它從此不會再擋下任何東西。
`tests/audit/test_e1_gates.py` 有一組成對的 `test_g08_fails_when_*`：
channel 非 CONFIRMED、scale 非 CONFIRMED、required facet 為 RECONSTRUCTED、
required facet 缺漏、以及**沒有 facets 的 v1 舊 artifact** ——
五種情況都必須 FAIL。少了它們這次修訂就只是把關卡拆掉。

### 重跑

```powershell
py -3.10 -m pcmef.cli provenance resolve-sigma `
    --paired-source data/raw_real/edge_impulse_export --export-decimals 4
py -3.10 -m pcmef.cli audit e1-gates
py -3.10 -m pytest tests/unit/test_amendments.py tests/provenance tests/audit -q
```

---

## M2 場景保真度（2026-08-27 晚間，NOTE-026 / NOTE-027）

**這是目前唯一的關鍵路徑。** 交接時請從這一節開始看。

### 一句話狀態

場景原本**結構上無法讓瓶子回光**，根因已定位並修掉兩層；四類的
signal / ambient / sigma 都恢復了鑑別力，**distance 只修到一半**。

### 已修

| # | 問題 | 根因 | 處置 |
|---|---|---|---|
| 1 | 加介質前後總能量到小數點都相同 | `transient_path` **靜默忽略** interior medium | 有介質時自動改用 `transient_prbvolpath`（NOTE-026） |
| 2 | `optical_path_to_distance = 0.5` 無物理依據 | 光源離軸，光程不是單程的兩倍 | 光源與相機共置（monostatic spot），係數由幾何成立（NOTE-026） |
| 3 | `ambient` 欄恆為 0 | 共置後場景只剩感測器發光 | 另加 `constant` 室內光（NOTE-026） |
| 4 | **瓶子完全不回光** | **SDS**：delta 光源配 delta BSDF，鏡面路徑採樣機率為零 | 瓶壁改 `roughdielectric`（NOTE-027） |
| 5 | FWHM 恆為 0，Sigma 映射整條路不可用 | 前緣餘裕是相對比例，在 5 cm 場景只有 **0.73 個 bin** | 餘裕改以 bin 數表示，可解析（NOTE-027） |
| 6 | 三分之二解析度落在沒有能量的區間 | `bounce_budget = 7` 是在舊場景量的 | 改 3.0，對實測末端仍有 31% 餘裕（NOTE-027） |

第 4 項是整串的核心。實測證據：有能量的像素只有 **234/4096（5.7%）**，
全部在畫面最左右兩條窄邊；中央 16×16（瓶身正中）能量佔比 **0.02%**。
唯一回到感測器的是背景板，所以四類 distance 全等於 261.28 mm。

### 修完的數字

| 量 | 修前 | 修後 |
|---|---|---|
| 四類總能量 | 325 / 294 / 283 / 298（落差 0.8%→13%） | **37,248 / 346,095 / 119,154 / 56,049** |
| `nonzero_bin_ratio` | 0.086（低於 0.1 守衛） | **0.21 – 0.375** |
| distance | 四類全為 261.28 mm | 45.35（Empty、Misty）／285.6（Water、Bubbly） |
| FWHM | 0（surrogate 直接拒絕輸出） | 可計算 |

### 峰值分解（2026-08-28，下一棒的起點）

把 `outputs/rough_win_2140` 的 transient 逐峰對照場景幾何。介面預期位置
（單程距離，`distance = 0.5 × OPL`）：

| 介面 | 幾何位置 |
|---|---|
| 瓶外壁前緣 | 50.00 mm |
| 瓶內壁前緣 | 52.00 mm |
| 瓶內壁後緣 | 105.00 mm |
| **瓶外壁後緣** | **107.00 mm** |
| 背景板（軸上） | 249.50 mm |

各類前五大峰：

| 場景 | 45.3 mm | 中段峰 | 背景板峰 |
|---|---|---|---|
| Empty | **67.4%** | 142.0 mm（3.0%） | 288.6 mm（11.7%） |
| Water | 6.6% | **60.9 mm（42.2%）** | 285.5 mm（**47.4%**） |
| Bubbly | 21.1% | 85.9 mm（15.3%） | 285.5 mm（**54.8%**） |
| Misty | **51.1%** | 120.2 mm（4.6%） | 291.7 mm（33.0%） |

**目前能宣稱到什麼程度（措辭刻意保守）：**

| 成分 | 可宣稱 | **不可**宣稱 |
|---|---|---|
| 45.32 mm | bottle **front-related** component | 已完成的 front-surface path lineage |
| 285–292 mm | **background-board-related** component | — |
| 中段 | **content-sensitive** component | 「= far-side bottle/foil return」 |

45.32 mm 與瓶外壁前緣幾何 50.00 相差 0.75 個 bin，四類位置完全相同、
與內容物無關；285–292 與背景板軸上幾何 249.5 相差 36–42 mm，方向與
離軸／折射一致。這兩者的歸屬有幾何支持。

**中段成分不得宣稱為遠壁回波。** 四類位置 Empty 142.0 / Water 60.9 /
Bubbly 85.9 / Misty 120.2 mm 差距過大：若是同一個反射面因介質改變而平移，
加水應使光程變**長**，但 Water 反而最短。這代表中段很可能是**數個不同
optical-path family 混在一起**，而不是單一 reflector。path lineage 尚未建立。

**水的 OPL sanity check（2026-08-28 修正）**：monostatic 往返通過厚度 L
的水，額外 OPL = `2L(n−1)`；ToF 除以 2 後表觀距離增量為
**`Δd = (n−1)L = 0.333 × 53 ≈ 17.65 mm`**。

> 先前本節記載 8.82 mm 是**錯的** —— 該值把 `L(n−1)` 又除了一次 2。
> 錯誤值曾寫入 commit 684d457 的訊息，該處無法更正，以本節為準。

實測 `Water − Empty = +12.96 mm`，與理想全水路徑 +17.65 mm 同號同量級，
但**小於**它。可能原因（**皆為 hypothesis，不得選一個當解釋**）：有效光程
未完整穿越 53 mm、多路徑混合、瓶壁折射、接收 FoV 加權。

**因此問題定位為 scene physics mismatch，不是 estimator 選擇問題。**
Empty 的 45 mm 成分佔 67.4%，任何前緣偵測器都會先抓到它；為了跳過它而加入
minimum distance / ignore-first-reflection 之類的規則，就是用 estimator
去補場景的錯 —— 那與「用 real class mean 建搜尋窗」是同一種偷渡。
**修場景在前，設 estimator 在後。**

### Scene/path decomposition（2026-08-28）

**發現 A —— Empty 的瓶子被建成實心玻璃柱，不是玻璃殼。** 這由程式碼直接可驗，
不依賴任何峰值比對：`build_scene_dict()` 的 `bottle_interior` **只在
`medium_preset != "empty"` 時建立**，因此 Empty 場景裡半徑 28.5 mm 的
`bottle_wall` 是一根**實心 bk7 圓柱**，光要穿過 57 mm 的玻璃。
真實的空瓶應該是「2 mm 殼 + 53 mm 空氣 + 2 mm 殼」。

| Empty 的遠側表觀距離 | 值 |
|---|---|
| 現行（實心玻璃 57 mm） | 136.46 mm |
| **正確（殼＋空氣＋殼）** | **109.07 mm** |
| 實測中段成分 | 142.0 mm（距實心玻璃預期 +5.54 mm ≈ 1.8 個 distance-bin） |

bin 寬為 3.12 mm（OPL bin 6.237 mm 的一半），所以 1.8 bin 的殘差**不算緊密吻合**，
只能說「與實心玻璃假設相容、與殼假設不相容」。

**發現 B —— 四個中段成分確實是不同的 path family，不是同一反射面平移。**

| 場景 | 實測中段 | 最接近的路徑假設 | 殘差 |
|---|---|---|---|
| Empty | 142.0 | 實心玻璃遠側 136.46 | +5.5 mm |
| Water | 60.9 | **玻璃→水 前介面 53.03** | +7.9 mm |
| Water | 60.9 | （殼＋水＋殼遠側 126.72） | **−65.8 mm** |

Water 的中段成分靠近**內側前介面**，離遠側差 65.8 mm。Empty 的則靠近遠側。
兩者不可能是同一個 reflector 因介質改變而平移 —— 這證實了先前
「中段峰 = far-side return」的說法必須撤回。

**發現 C —— 背景板無 provenance，且 ToF 光路終結在它上面。**
場景只有一個 `backdrop`（diffuse rectangle、reflectance 0.5、z = 171 mm），
它是為了讓 RGB 有背景才加的。SRC-PLAN 與 SRC-HANDOFF 都沒有支持
「該位置有一塊漫反射板」。反而 SRC-HANDOFF §0 把
「aluminum-foil 的 offset / orientation / BRDF / 940 nm reflectance」
列為**未回收**，這正表示真實裝置的遠端目標是**鋁箔**。
目前 285–292 mm 成分在 Water/Bubbly 佔 47–55% 能量 —— 一個純 RGB 佈景物件
主導了 ToF 通道。

**尚未查明 —— 45 mm front 成分為何在模擬中如此強（Empty 佔 67.4%）。**
待查清單（**不得先用 estimator 繞開**）：瓶身曲率與法線分佈、
`_BOTTLE_SURFACE_ALPHA` 的 GGX lobe 寬度、bk7 IOR 在 940 nm 的適用性、
spot 的 `cutoff_angle` 與接收端 FoV 加權、以及該成分的 specular path lineage
（單次反射 vs 多次）。目前只知道它與內容物無關且四類位置相同。

### Scene correction 結果（2026-08-28，NOTE-029）

三項修正皆已落地：backdrop 移出 canonical physical scene 並改建鋁箔反射體、
Empty 改為殼結構、Misty 基底由水改為空氣。

**bounce lineage（以 `max_depth` 遞增取得，非位置比對）**

| 成分 | 首次出現 | 交互作用 | 身分 |
|---|---|---|---|
| 44.9 mm | **max_depth=2** | 1 | **前玻璃單次反射**（該深度佔 98%） |
| 60.2 mm | max_depth=5 | ~4 | 內側介面（玻璃↔內容物） |
| 146–162 mm | max_depth=12 | ~10 | **箔片**（穿過整個瓶子來回） |

`max_depth=2` 只允許 sensor → 一個表面 → sensor，因此 45 mm 的 front-glass
身分現在有 **tracing 證據**，可從 `front-surface-associated` 升級為
front-glass single-bounce return。

**修正前後對照**

| 項目 | 修正前 | 修正後 |
|---|---|---|
| 主導回波 | 背景板（**無 provenance**）47–55% | 箔片（provenance-supported）27–89% |
| Empty 的 45 mm 佔比 | 67.4% | **6.6%** |
| 四類共同 far-side family | **不存在** | **存在**（147.4 / 162.6 / 162.6 / 145.7 mm） |
| Empty 瓶身 | 實心玻璃柱（拓樸錯誤） | 殼＋空氣＋殼 |
| 無 provenance 的 dominant return | **有** | **無** |

**箔片 family 的類別相依性**（相對 Empty，單位 mm）

| | 模擬 | 真實 |
|---|---|---|
| Water | **+15.20** | +12.96 |
| Bubbly | +15.20 | +4.66 |
| Misty | −1.70 | −21.40 |

Water 的模擬位移 +15.20 落在真實 +12.96 與理想全水路徑
`Δd=(n−1)L=17.65` 之間，方向與量級皆一致。
Bubbly 與 Misty 的量級差距大，但**那是未校準的介質參數所致**，
屬 calibration 範圍，不得在此以調參處理。

**仍未處理**：`_FOIL_*` 四個常數加上 NOTE-026／027 的六個，
共十個未校準建模常數仍在 formal firewall 之外。

**下一棒的順序（不得跳過）**：
1. 移除或隔離純 RGB 需求造成的 ToF background artifact
2. 建立 provenance-supported 的 far-side aluminum-foil reflector，
   光學常數一律標記為 **calibration-only nuisance parameters**
3. 修正 Empty 的殼結構（發現 A）
4. 查明 45 mm 成分過強的原因
5. 確認修正後是否存在**四類共同、物理一致**的 far-side return family

以上完成後才設計 estimator，且措辭一律為
**VL53L0X-inspired / VL53L0X-like transient range estimator** ——
ST 公開的是 `RangeMilliMeter` / Signal / Ambient / RangeStatus 與
Signal Fail、Sigma Fail、Range Ignore Threshold 等門檻機制，
**沒有公開「最終 range 由 ranging window 內的 leading-edge + centroid 得出」**。
不得宣稱為 internal algorithm 的重現。

### 還沒解決的那一半

distance 已經會隨內容物變化，但仍是**雙峰**，而真實是連續分佈：

| | Empty | Water | Bubbly | Misty |
|---|---|---|---|---|
| 模擬 | 45.35 | 285.6 | 285.6 | 45.35 |
| **真實** | **100.91** | **113.87** | **105.57** | **79.51** |

真實四類落在 79–114 mm，對應瓶子**遠壁**（幾何 107 mm），不是前緣（50 mm）
也不是背景板（249.5 mm）。也就是說真實 VL53L0X 取的**不是全域峰值**，
而模擬取的是。這一步屬於峰值判定策略，**尚未裁決**。

`surrogate/distance.py` 已有 `PEAK` 與 `ENERGY_CENTROID` 兩種模式；
第三種可能是「首個超過閾值的回波」或感測器 FOV 加權。三者都是建模決策，
不是可調係數 —— 不得為了讓數字對上真實均值而挑其中一個（SRC-SAI §10）。

### ~~一個尚未處理的守衛缺口~~ → **已於 2026-08-28 關閉（NOTE-030）**

`_BOTTLE_SURFACE_ALPHA`、`_LEADING_MARGIN_BINS`、`bounce_budget`、
`_ROOM_LIGHT_RATIO`、`_SIGMA_T_REFERENCE_PER_M`、`_ALBEDO_BY_PRESET`
先前不在 `surrogate/calibration.py` 的 placeholder 清單內，formal 防線攔不到。

**現已全部納入 `configs/parameter_registry.yaml` 並由 `core.parameters` 攔截**，
另加上先前連具名常數都不是的 `sensor.fov_deg` / `light.cutoff_angle_deg` /
`max_depth` / `foil_orientation`。
綁定涵蓋率 code=30 / config=8 / **unbound=0**，live drift **0**。
新增建模常數而忘了登記，`test_an_unbound_registry_entry_is_reported_not_ignored`
會失敗。詳見「Phase A1/A2 完成」一節。

### 重跑指令

```powershell
$env:PATH = "C:\Program Files\LLVM\bin;$env:PATH"   # 少了會看到 LLVM-C.dll 錯誤
py -3.10 -m pcmef.cli sim smoke --config configs/simulation/smoke.yaml `
    --out outputs/rough_win_2140
py -3.10 -m pcmef.cli surrogate smoke `
    --simulation-out outputs/rough_win_2140 --out outputs/rough_win_2140_surrogate
py -3.10 -m pytest tests/simulation tests/surrogate -q
```

`outputs/` 底下的對照組（**不要刪，是上述數字的出處**）：
`baseline_2116`（修前）、`rough_2130`（只改 BSDF）、`rough_win_2140`（現況）。

### 交接注意

- 2026-08-27 早上有一份 WIP 斷在額度上限，**只存在工作區、沒有 commit**。
  已備份為 tag `wip-backup-20260827-2116`（`git show` 可取回）。
  本節的第 1–3 項就是那份 WIP 的內容，已驗證並保留。
- 上一棒的 `mitsuba_adapter.py` 最後編輯於 09:03，但最後一次模擬跑於 09:00 ——
  **那次編輯從未被跑過**。接手時不要相信任何沒有對應 artifact 的數字。

---

## Console 已完成內容（2026-08-27）

網頁執行台：**一鍵跑模擬 → 即時逐行看輸出 → 看曲線與數據**。

```powershell
py -3.10 -m pcmef.cli admin serve      # http://127.0.0.1:8787/console
docker compose up console              # http://127.0.0.1:8801
```

| 頁面 | 內容 |
|---|---|
| `/console` | 三個 preset 一鍵開跑；進階參數摺疊（解析度/spp/bins/seed/偏移/光源/類別）；12 個 gate 燈號；執行歷史 |
| `/console/runs/<id>` | 即時 log（SSE 逐行推送）、transient 曲線、各場景能量、四特徵表、這次用的完整設定 |

**實機驗收**（瀏覽器實際操作）：按下「開始模擬」後 log 逐行出現，
結束時徽章由 running 轉 succeeded 並顯示 exit code，重新整理後四條 transient
曲線與能量長條圖正確呈現。四類能量 **3908.9 / 3898.9 / 3879.0 / 3875.9**，
與 CLI、Docker 三處完全一致 —— 畫面與 artifact 沒有分岔。

**界線與 Part VI 相同**：console 只跑探索性指令，`_assert_not_formal()`
硬性拒絕任何含 `formal` 的參數。formal run 一律走 CLI（§208），
本頁不產生任何 lock。

**minimal JS 的量化定義**（§42 允許 minimal JS、禁大型前端依賴）：
首頁 `document.scripts.length === 0`（進階摺疊用原生 `<details>`）；
執行頁**恰好一段內嵌腳本、零外部來源**，內容是 `EventSource` 接收。

**三個實作缺陷**（詳見 NOTE-025）：

| 缺陷 | 症狀 |
|---|---|
| `classes or CLASS_ORDER` | 四個核取方塊全部取消 → 靜默跑全部四類，與按下去的意思相反 |
| `spp or preset["spp"]` | 輸入 0 → 靜默變成 preset 值 |
| `var(--chart-N)` 未定義 | **曲線畫得出來但完全透明**，畫面只剩圖例，看起來像沒資料 |

前兩個是 NOTE-005「falsy 被 `or` 吸收」的同一病灶；第三個不會有任何測試失敗，
只有實際打開瀏覽器才看得到。

---

## Batch 6 已完成內容（2026-08-27）

測試：**1163 passed**（新增 62 條 E1 測試）。E1-G06 由 NOT_PRODUCED 轉 **PASS**，
gate 現況 **7 PASS / 0 FAIL / 5 NOT_PRODUCED**。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `stats/metrics.py` | §11 | raw W1、s_f（calibration-only pooled IQR）、NW、mean/SD、temporal、class ordering、trend |
| `stats/bootstrap.py` | Appendix I2 | 成對重抽：同一組索引同時套用到兩個候選 |
| `experiments/e1.py` | §11、Appendix H2/B | 逐 class×feature 算 Delta；檢查 lock 順序與 matched design |
| `experiments/e1_outcome.py` | §12.1 | 三條 AND 判定 → PASS/DEGRADED → `e1_outcome.lock` → claim_mode |
| `cli.py e1 metrics-evidence` | §12 E1-G06 | 產出 `tests/e1_metrics.xml` |

**兩個量化到數字的設計理由**（NOTE-024）：

*跨特徵不得平均 raw W1* —— distance 差 5 mm（s_f=10）與 signal 差 0.005 MCPS
（s_f=0.010），raw W1 相差 **1000 倍**，但失真程度其實相同（NW 都是 0.5）。
直接平均會讓 distance 完全主導，signal 的改善與退步都看不見。

*成對重抽會改變結論* —— 60 個 scenario 的對照實測：共用同一組重抽時
95% CI 寬度比各自獨立窄 **5 倍以上**，而且**下界的符號不同**
（配對後為正 → 判有改善；獨立後跨 0 → 判沒有）。這條寫成
`test_paired_resampling_is_narrower_than_independent`，因為沒有它，
改成各自獨立重抽不會有任何測試失敗。

**測試抓到的實作缺陷**：趨勢一致性初版用「相對變化的絕對差 ≤ 0.5」判幅度。
真實相對變化只有 0.1 量級時，「合成完全沒變化」的絕對差是 0.1，照樣通過 ——
而那正是最該擋下的（方向沒錯只因為沒有反向）。已改為比值判定。

**尚不能跑 formal E1，這是正確狀態。** surrogate 的九個校準常數仍全部是
placeholder（formal 模式直接拒絕載入），因此 E1-G07/G10/G11/G12 四個 lock
無法凍結。Batch 6 交付的是**引擎**，不是可執行的 formal E1 ——
要跑得起來還缺 M2 的實際校準。

---

## Batch 7 已完成內容（2026-08-27）

測試：**1099 passed**。稽核器先於 Batch 6 建立是刻意的 —— 它就不會在事後
被寫成剛好符合已產出的結果。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `audit/result.py` | — | 四種狀態；NOT_PRODUCED 與 FAIL 分開 |
| `audit/e1_gates.py` | §12 E1 Experiment-Ready Gate | 十二個 gate 逐項判定 |
| `audit/firewall.py` | Appendix B、Appendix H1 | FW-01..05 洩漏防線 + SP-01..06 政策契約 |
| `cli.py audit e1-gates / heldout-firewall / real-split-policy` | §32 | 三個指令，可落 JSON 報告 |

**目前實測結果**：

```
e1_gates            PASS 6  FAIL 0  NOT_PRODUCED 6
heldout_firewall    PASS 4  FAIL 0  NOT_PRODUCED 1
real_split_policy   PASS 6  FAIL 0  NOT_PRODUCED 0
```

六個 NOT_PRODUCED 是 G05（Batch 8）與 G06/G07/G10/G11/G12（Batch 6）；
FW-03 的 NOT_PRODUCED 是「尚無 calibration artifact，時序無從比較」。
**這些都是正確輸出，不是失敗**（NOTE-022）。不帶 `--require` 時 exit code 為 0；
`--require G01:G12` 會如實擋下 —— E1 確實還不能跑。

**稽核器上線第一次跑就抓到一個真實不一致**：

`provenance/sigma_resolution.json` 說 `status=UNRESOLVED`、`register=null`，
但 `configs/base.yaml` 與本檔都宣稱 sigma 已解出 `0x18`。追查後確認
**結論是對的，證據是舊的** —— NOTE-010 v2 的排除法當時以臨時腳本完成，
而產生 E1-G08 證據的 `provenance resolve-sigma` 從未實作那條路徑：
它只讀彙總格式的 55,440 筆 sigma 值，沒呼叫 `rule_out_range_register()`，
也沒傳 `export_decimals`。

修正後重跑，獨立重現了 NOTE-010 v2 記載的數字：

```
py -3.10 -m pcmef.cli provenance resolve-sigma \
    --paired-source data/raw_real/edge_impulse_export --export-decimals 4
→ n=280000  /128 殘差 0.4992 > 容差 0.0064（排除）  register 0x18  status RESOLVED
```

排除法需要**同一列**的 sigma 與 distance 配對，彙總格式已把時間軸摺成窗口
統計量做不到，因此新增 `EdgeImpulseAdapter.stacked_values()`。

**另一個順帶修掉的缺陷**：稽核報告用了 `∅` `−` `≥` 三個不在 cp950 裡的符號，
在繁中 Windows console 上會讓整份報告印到一半崩潰。已換成 ASCII，
並讓 CLI 對無法編碼的字元退化成替代字元而非崩潰 —— 一份跑到一半才掛掉的
報告，比一份有幾個問號的報告糟得多。

---

## 規格對照稽核（2026-08-26）

以 SAI §31 目錄樹、E1-G01..G12、Appendix D Batch exit criteria 逐項盤點。

> 本節為 8/26 的稽核。**8/27 Part VI 落地後，§31 目錄樹由 17/62 升至 31/62**
> （新增 agents 2、llm 4、admin 6、secrets 2）；其餘欄位未變。

| 面向 | 現況 | 說明 |
|---|---|---|
| §31 目錄樹 | **17/62 檔** → 8/27 為 **31/62** | 缺的全在 Batch 6-8 與 M4-M8 範圍（`core/splits.py` 已於 8/26 補上） |
| E1 gates | **6/12 有證據** | G01/G02/G03/G04/G08/G09 |
| Batch exit | **1-5 完成**，6-8 未開始 | |
| 22 formal locks | 全數登錄；**1 個已寫** | `real_split_policy`（`186d307571b39ce3`）；其餘 21 個仍 pending |

> 上表為 2026-08-26 首次稽核後、當日稍晚 real split 落地的更新值。
> 首次稽核時為 16/62 檔、4/12 gate、0 個 lock。

**稽核抓到的缺口（已補）**：

| 缺口 | 性質 | 處置 |
|---|---|---|
| E1-G01 artifact 不存在 | Edge Impulse adapter **從未接上 CLI**，盤點只跑在臨時腳本 | 新增 `--source-format edge-impulse`，產出四份 artifact |
| `nominal_logical_recordings` 寫死 0 | 五層計數失去對照基準 | 由 config 帶入，現為 560 vs 實際 560 |
| sigma 已解出但 CLI 讀不到 | E1-eligible 恆為 0 | config 已凍結時才讀（未凍結仍須 CLI 提供，避免循環） |
| E1-G04 artifact 不存在 | surrogate 能跑但無指令 | 新增 `pcmef surrogate smoke` |

**真實 M0 五層計數（`data/inventory/`）**：

```
nominal_logical_recordings : 560   [configs/base.yaml]
physical_source_files      : 560
canonical_recordings       : 560
valid_recordings           : 560
e1_eligible_recordings     : 560
exclusions: none
```

**仍缺 artifact 的 6 個 gate 各自的阻塞**：G05 需 scenario generator（Batch 8）；
G06/G07/G10/G11/G12 屬 Batch 6-7。G02/G09 原本的阻塞（`core/splits.py` 與
3 項教授裁決值）已於 2026-08-26 解除，見下節。

---

## Real split 已凍結（2026-08-26，NOTE-014）

教授裁決三項數值後，`core/splits.py` 建成並實際執行。**這是 M0 之後、任何校準
之前**的切分，符合 SRC-PLAN §3.1 的時序要求。

| 裁決項目 | 值 |
|---|---|
| allocation | calibration 70% / heldout_real 30% |
| minimum_per_class | 100 支 e1-eligible recordings |
| seed | 20260826 |
| 任一 class < 100 | **BLOCK**（不是警告） |
| lock 後 redraw | **禁止** |

執行結果與裁決時的預測完全一致：

| class | eligible | calibration | heldout_real |
|---|---|---|---|
| Bubbly / Empty / Misty / Water-filled | 各 140 | 各 98 | 各 42 |
| **TOTAL** | **560** | **392** | **168** |

ID 碰撞 0 筆；heldout access count 於凍結時為 0。
`freeze/real_split_policy.lock.json`，payload hash `186d307571b39ce3`。
以 `--set real_split_policy.seed=99999` 重凍已實測被拒（exit 1，locks are immutable）。

**兩份 artifact 的版控分工**：`data/splits/split_registry.json` 進版控，
`freeze/*.lock.json` 不進（lock 屬於個別 run）。registry 因此也帶出三組
set hash，與 lock 同名欄位互為對照，已實測相符
（`f76776e4` / `4bda77f6` / `8421a54e`）。
注意 repo 單獨保有的資訊足以**稽核**切分，但不足以**重算** ——
重算需要同樣不進版控的 `data/raw_real/`（560 個 JSON）。

**group_rule = `seeded_stratified_recording`，不是 group-disjoint**，理由記錄在
policy 與 lock 內：Edge Impulse 匯出的 560 筆樣本 payload 只有
`device_type / interval_ms / sensors / values`，沒有任何 session 欄位；
唯一的時間戳 `protected.iat` 全體只橫跨 46 秒（12:21:19–12:22:05），
而實際採集 560×41 秒需 6 小時 22 分 → 那是匯入時間，不是採集時間。
裁決說「若有 session/time grouping 以 group-disjoint 優先」，前提不成立。
**若日後取得採集腳本或 session log，須依 NOTE-014 走新 run，不得就地重抽。**

---

## M0 資料齊備（2026-08-26）

`data/raw_real/` 三個來源，`adapters/` 各有對應 reader：

| 來源 | 內容 | reader | 狀態 |
|---|---|---|---|
| `edge_impulse_export/` | **560 筆 500×4 原始序列**（448 train / 112 test） | `edge_impulse.py` | 560/560 valid、0 排除 |
| `vision/` | 1200 張 .jpg，4 類各 300 | 待 M4 | 完整 |
| `tof_aggregated/` | 窗口 Mean/Std（獨立來源，用於交叉驗證） | `legacy_kg.py` | 560 recordings |

**兩來源交叉驗證**：16 個 class×feature 組合全部吻合到小數第四位（相對差 0.00%）。
四類距離平均 Empty 100.91 / Water 113.87 / Bubbly 105.57 / Misty 79.51 mm，
與 SRC-PLAN §2.1 錨點相符。

**Sigma provenance 已解出**（NOTE-010 v2，280,000 列全資料）：

| 項目 | 結論 | 依據 |
|---|---|---|
| divisor | **/65536** | `sigma×128` 殘差 0.4992 > 容差 0.0064，`/128` 排除；`/1` 因非整數排除 |
| register | **0x18** | `sigma×65536` 與 distance 零匹配、相關 −0.476、量級差 298 倍 → `0x1E`（final range）排除 |

先前「四參數腳本用 0x1E 所以 0x1E 可信」的推論**方向相反**：
那代表那幾支推論腳本把 final range 當 sigma 讀，是 bug。
採集腳本仍未取得，取得後須複核（`acquisition_script_obtained: false`）。

---

## 待教授裁決事項（formal-blocking，共 10 項）

執行 `py -3.10 -m pcmef.cli config check` 可隨時取得最新清單。
這些數值依 SRC-PLAN Appendix A 與 SRC-SAI Appendix F **禁止實作端自行補值**，
目前全部以 `!required` 標記，任何程式讀到即中斷。

| 分類 | 待裁決項目 |
|---|---|
| Perception | `training_seed_pairs` |
| Reliability | `crossfit_folds` |
| Gate | `alpha`、`beta`、`gamma`（須由 validation 搜尋選出後 freeze） |
| Agents | `representation_mode`、`retry.max_attempts` |
| E2 | `final_n_per_class`（只能由 e2_pilot 依預註冊 sizing rule 決定）、`severity_allocation` |
| Conflict | `delta` |

**已核定並寫入 config（不再 blocking）**：

| 批次 | 項目 | 值 | 記錄 |
|---|---|---|---|
| 2026-08-26 | real split allocation / minimum / seed | 70-30 / 100 / 20260826 | NOTE-014 |
| 2026-08-26 | `e1.scientific_rule.bootstrap_replicates` / `_seed` | 10000 / 20260826 | NOTE-015 |
| 2026-08-26 | `statistics.bootstrap_replicates` / `_seed` | 10000 / 20260827 | NOTE-015 |

E1 那組核定時 held-out access_count 為 0 且尚無任何 E1 結果 —— 這是預註冊的
前提條件，不是行政程序。**「換個 seed 看看」在任何情況下都不是除錯手段**
（NOTE-015 維護邊界）。

上表其餘項目對應 SRC-PLAN Appendix A 的教授討論題目；`gate.alpha/beta/gamma`
與 `e2.final_n_per_class` 性質不同 —— 它們不是「請教授給個數字」，
而是必須由 validation 搜尋或 pilot 依預註冊規則產出後才凍結。

---

## 資料現況（2026-08-26 取得 Drive「實驗交接」後更新）

已下載並封存於 `data/raw_real/`（159.5 MB，已被 .gitignore 排除）：

| 模態 | 狀態 | 內容 |
|---|---|---|
| **RGB / Vision** | ✅ **完整** | 1200 張 .jpg，4 類各 300 張，資料夾名已是 canonical 標籤；另含 keras/pytorch checkpoint |
| **ToF 四特徵** | ⚠️ **只有衍生統計量** | `KG_<class>_<metric>_<Mean\|Std>.csv`，各 **(99 窗口 × 140 recordings)**，無時間欄 |
| ToF 原始 500 點 | ❌ **幾乎不存在** | 僅 3 檔、且**只有 Distance**（偏移測試的最低信心窗口副產物） |
| 偏移錨點 | ✅ 已驗證 | 實測 98.53 / 90.91 / 88.73 mm，與計畫書 98.61 / 91.05 / 88.76 相差 < 0.15 mm |
| 推論輸出 | ✅ | classification_results 含完整四類機率向量 |

**三個關鍵後果**（詳見 NOTE-011）：

1. `CanonicalCase.tof_sequence` 的 (500,4) 契約**無法由真實資料滿足**。
   E1 若要進行，synthetic 側必須套用完全相同的滑動窗口後才比較，
   且此變更要寫進 `e1_scientific_rule.lock`，不得沿用「500 點分佈比較」措辭。
2. **取樣間隔實測為 0.0624 s**——既非計畫書的 0.082 也非腳本的 0.02。
   若當初硬編 0.082，所有時間統計會偏約 31%。Batch 2 從 recording 自身推導的
   設計因此被反向驗證為必要。
3. `adapters/legacy_csv.py` **讀不了**這個 (99,140) 矩陣格式。它的對齊、
   排除帳與五層計數契約仍正確，但需要第二個 reader；在補上前不得宣稱 M0 完成。

Perception 訓練不受影響：依 SRC-PLAN §3.1，`perception_train` 用的是
**synthetic** Clean/Nominal scenarios，真實資料只作 E1 的 calibration 與 held-out。

---

## 環境阻塞

| 項目 | 狀態 | 影響里程碑 |
|---|---|---|
| 逐 recording 的原始 ToF CSV | **未取得**；最可能在樹莓派 `/home/pi/` | E1 的 500 點契約 |
| `mitsuba` / `drjit` / `mitransient` | 已安裝（另需 LLVM toolchain 提供 LLVM-C.dll） | — |
| `tensorflow` / `scikit-learn` | 未安裝 | M4、M5 |
| ~~`jsonschema`~~ | **已安裝 4.26.0**（2026-08-27） | structured_json probe 需要它才能真的驗證 |
| `Flask` / `httpx` / `cryptography` | 已安裝（3.0.3 / 0.28.1 / 43.0.3） | Part VI |

Python 執行環境：`py -3.10`（3.10.11，numpy 2.2.6 / scipy 1.15.3 / pandas 2.3.3
/ PyYAML 6.0.3 / pytest 9.0.3 已就緒）。
注意 PATH 上的 `python` 指向 3.12 且缺相依，一律使用 `py -3.10`。

---

## Batch 5 已完成內容（2026-08-26）

測試：**558 passed**。整條鏈路已在**真實 mitsuba 輸出**上跑通。

| 模組 | 對應規格 | 內容 |
|---|---|---|
| `surrogate/features.py` | §10 | 峰值時間、能量重心、主/背景能量、FWHM、SNR、多路徑突起度 |
| `surrogate/calibration.py` | §10、§21 | 校準常數容器；formal 模式拒絕 placeholder |
| `surrogate/{distance,signal_rate,ambient,sigma}.py` | §10 對照表 | 四欄各自獨立映射 |
| `surrogate/single_acquisition.py` | §10 介面草案 2 | 一次 transient → **恰好一筆** [4] 觀測 |
| `surrogate/temporal_model.py` | §10 | (500,4) recording；取樣間隔必填 |

**端到端實測**（Empty 場景的真實 transient）：

```
物理量: FWHM 0.99 ns | SNR 7.8 | multipath 0.56 | total_energy 3909
recording: (500, 4) | 41.0 s | measurement_time | 每欄 SD 均 > 0
```

**三條禁止做法都有自動防線**：

| 禁止做法（SRC-SAI §10） | 防線 |
|---|---|
| `tof_recording = transient_bins[:500]` | `n_samples` 與 bins 無關，改 bins 不改 recording 長度 |
| 預設取樣間隔為 0.082 s | `sample_interval_s` 為必填關鍵字，且需附來源 |
| 只用 Distance 推算 Signal | 固定回波時間只改振幅，Signal 變 10 倍而 Distance 變動 < 5% |

**整合時抓到 Batch 4 的缺陷**（NOTE-013）：transient 時間窗在峰值抵達前就關窗
（實測峰值 OPL 0.465 m，初版窗尾僅 0.442 m），導致 FWHM 恆為 0。
當時的 `nonzero_bin_ratio = 0.719` 看起來完全正常，因為截斷後仍有七成 bin 帶能量；
真正的徵兆是「峰值貼在窗邊」而初版沒有量。修正後總能量 712 → 3909。
已加入截斷偵測，這類錯誤現在會在產出當下中斷。

**尚未校準**：九個校準常數全部標記 placeholder，formal 模式直接拒絕。
目前輸出的 Distance 約 230–257 mm（真實為 100–114 mm），
因為場景光源與相機**非共置**而 `optical_path_to_distance = 0.5` 假設共置。
正確解法是把光源移到相機位置，**不是**調係數去湊距離（§10 禁止做法）。

---

## E1 claim：已不需要妥協

先前因真實 ToF 只剩窗口統計量，曾規劃「彙總統計量 + 相同滑動窗口」的替代路線，
並列出三項代價（措辭改寫、temporal 指標降級、平均使分佈變窄致 fidelity 較易通過）。

**Edge Impulse export 復原 560 筆 500×4 原始序列後，此妥協已不需要。**
E1 可依原規格在 500 點序列上進行，`e1_scientific_rule.lock` 沿用原措辭即可。

---

## ~~下一步（2026-08-27 晚間更新）~~ → **已全部完成（2026-08-31）**

> 本節寫於校準預註冊之前。其後 M2 已 closed（E1 partial calibration
> success）、成對資料橋已驗收。保留為歷史記錄，**不是待辦清單**。


外部阻塞已全部解除：資料齊備、sigma 解出、real split 凍結、E1/E2 bootstrap 核定。
**Part VI、Batch 6、Batch 7 皆已完成**。

> **2026-08-28 更新**：下列第 2 項（把建模常數納入 formal 防線）**已完成**，
> 見「Phase A1/A2 完成」。第 1 項（distance 峰值判定）**已改判為 BLOCKED**，
> 理由不是「還沒裁決」而是模擬波形在真實讀值區間沒有能量，
> 見「C：Distance estimator」。最新順序見本節末尾。

**接下來唯一的關鍵路徑是 M2 場景保真度**（見上方同名章節）。順序：

1. **裁決 distance 的峰值判定策略** —— 目前模擬取全域峰值得到雙峰
   45.35／285.6 mm，真實是 79–114 mm 連續分佈、對應瓶子遠壁。
   這是 M2 校準的最後一塊，也是九個 surrogate 校準常數能否離開
   placeholder 的前提。
2. **把建模常數納入 formal 防線** —— 六個未校準常數目前不在
   `calibration.py` 的 placeholder 清單內，formal 模式攔不到。
   凍 `initial_simulation.lock` 前必須處理。
3. **Batch 8** —— post-E1 split generators，補上 G05。
4. 仍待核定的 10 項（見下）。
5. 採集腳本仍未取得（見下）。

### 2026-08-28 之後的順序（取代上表第 1、2 項）

1. **查明 75–120 mm 為何無能量。** 幾何上瓶外壁後緣在 107 mm，
   但 NOTE-029 的 bounce lineage 只找到 45（前玻璃單次）／60（內側介面）／
   145–162（箔片）三支 path family，**沒有遠壁那一支**。
   這是 distance 對不上真實的直接原因，也是 estimator 能否設計的前提。
2. **改正 Ambient 觀測量**（獨立 ambient pass，關閉 spot），
   完成後 CG-3 才能從 BLOCKED 退回可裁決。
3. 上述兩項完成後，依預註冊的偏移錨點準則選 estimator。
4. **Batch 8** —— post-E1 split generators，補上 G05。
5. 仍待核定的 10 項、採集腳本（見下）。

**判定參數面可否進 formal，一律以 `params audit` 的 exit code 為準**
（0 = 可，2 = 不可），與 gate 一律以 `audit e1-gates` exit code 為準同理。

判定 E1 可否開跑，一律以 `audit e1-gates --require G01:G12` 的 exit code 為準，
不以任何文件敘述為準。

**為什麼不先做 M4/M5。** `locks status` 顯示 22 個 lock 只凍了 1 個、
19 個 BLOCKED，鏈頭是 `initial_simulation`。M4 之後的模組即使寫完也
一個 lock 都凍不了，且 `tensorflow` / `scikit-learn` 都還沒安裝。
先做 M2 才會讓鏈條往前動。

---

## 附：2026-08-26 版的「下一步」（第 1 項已完成，其餘仍有效）

保留原文供對照 —— 特別是 §52 的七步表與 §51 的十條驗收條件，
它們是 Part VI 完成與否的判準，不該因為做完了就從檔案裡消失。

### 1. Part VI —— Admin LLM Setup UI（SAI §38、§42、§46、§50、§51、§52）→ **已完成**

`pyproject.toml` 的 `admin = ["Flask>=3.0"]` 就是為此預留。
**§52 明訂實作順序，不得跳步**：

| 序 | 內容 | 硬性規則 |
|---|---|---|
| 1 | `ProviderAdapter` normalized contract + `secret_ref` 抽象 | **禁止 UI 直接碰 provider SDK** |
| 2 | `llm_connections` / `llm_models` / `llm_task_bindings` / verification / audit 五張表 + migration | |
| 3 | capability probes：先 chat + structured_json，再 vision；embedding 只作 optional | |
| 4 | Flask local admin page，版型依 §42 圖 8 | 四張 card：Add Connection / Connections / Task Bindings / Formal Snapshot |
| 5 | `llm_runtime` snapshot + hash + state invalidation | **先讓 CLI 走完全流程，再接 UI 的 Bind** |
| 6 | Agent artifact cache | Formal runner 只走 lock + cache/ProviderAdapter |
| 7 | security/leakage/cache/resume acceptance tests | **跑完才准標 DoD** |

**§42 視覺規則**：白底 admin console、內容最大寬約 1000–1200 px、
三張 card 垂直排列、狀態與 capability 用 compact badge、危險操作紅色、
主要操作深藍/青色。**第一版 server-rendered HTML + minimal JS，
不引入 React/Vue 等大型前端依賴。**

**v0.5.0 核心裁決（§52 結語）**：這個頁面可以「可寫入」，但它只寫
**draft** LLM registry。provider/model/prompt/schema/runtime identity 一旦
進入 Formal，就由 immutable `llm_runtime.lock` 接管。
**UI 永遠不能成為繞過 freeze 的第二條設定通道。**

驗收條件是 §51 的十條，不是「畫面看起來對」：

| ID | PASS 條件 |
|---|---|
| LLM-UI-01 | 新增 connection 後 response / HTML / logs 均找不到完整 API key |
| LLM-UI-02 | embedding-only model 不出現在 observation/arbitration agent 的可綁 dropdown |
| LLM-UI-03 | Arbitration binding 若 structured-json probe FAIL，Bind 直接拒絕 |
| LLM-UI-04 | 仍被 task 使用的 model profile Delete → 409，dependency 列表正確 |
| LLM-UI-05 | snapshot 產生後改 live binding，既有 `llm_runtime.lock` hash 不變 |
| LLM-UI-06 | SQLite binding table 被手改後，formal runner 仍用 lock 中的 model identity |
| LLM-SEC-01 | manifest / report / DB plaintext 全域掃描無 secret |
| LLM-CACHE-01 | 相同 evidence/config 在 3 checkpoint pairs 只產生 1 次 provider call |
| LLM-CACHE-02 | prompt/schema/model/revision/representation/evidence 任一 hash 變 → cache miss |
| LLM-RESUME-01 | 已完成 formal case resume 時只讀 frozen response，不重呼叫 provider |

安全邊界（NFR-09、§46）：預設只 bind `127.0.0.1`；API key 寫入後不回傳，
UI 只顯示 masked fingerprint（`****abcd`）；跨網段才要求 admin auth + CSRF + TLS。
**所有 formal run 一律無 UI、走 CLI**（§208）。

### 2. Batch 7 —— 稽核系統

E1-G01..G12 audit + heldout firewall + real split policy audit。
目前 6/12 gate 有 artifact；稽核系統會如實回報其餘 6 個「尚未產出」，
**那是正確輸出而不是失敗** —— G05 屬 Batch 8，G06/G07/G10/G11/G12 屬 Batch 6。

先做稽核再做 Batch 6 是刻意的：稽核器先於被稽核的產物存在，
就不會在事後被寫成剛好符合已產出的結果。

### 3. 仍待核定的 10 項

`config check` 有完整清單。注意其中 `gate.alpha/beta/gamma` 與
`e2.final_n_per_class` **不是「請教授給數字」** —— 前者須由 validation
worst-condition Macro-F1 搜尋選出後才寫入 `gate.lock`，後者只能由 e2_pilot
依預註冊 sizing rule 決定（禁止查看 G5−G4 delta 後回填）。

### 4. 採集腳本仍未取得

`acquisition_script_obtained: false`。Sigma 已用排除法解出 0x18，不阻塞任何
工作，但腳本取得後須依 NOTE-010 複核；若與 0x18 不符，`sigma_provenance`
之後的所有 surrogate 校準都要重跑。
