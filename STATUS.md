# PC-MEF Research System — 進度真相

本檔是進度與交接的唯一真相來源。聊天訊息裡的說明不算完成。
刻意不另開 HANDOFF 檔：兩份文件必然漂移，屆時沒人知道該信哪一份。

最後更新：2026-08-27

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
py -3.10 -m pytest                                   # 全部測試（1302 條，約 6 分鐘）
py -3.10 -m pcmef.cli config check                   # 待教授裁決的 10 項
py -3.10 -m pcmef.cli locks status                   # 22 個 formal lock 的狀態
py -3.10 -m pcmef.cli sim smoke                      # M1 模擬 smoke
py -3.10 -m pcmef.cli surrogate smoke                # E1-G04 四特徵無 NaN 驗證
py -3.10 -m pcmef.cli split plan-real --source data/raw_real/edge_impulse_export
py -3.10 -m pcmef.cli provenance resolve-sigma `
    --paired-source data/raw_real/edge_impulse_export --export-decimals 4
py -3.10 -m pcmef.cli audit e1-gates                 # Batch 7：十二個 gate
py -3.10 -m pcmef.cli audit heldout-firewall         # Appendix B 洩漏防線
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
| **M2 場景保真度** | `initial_simulation.lock`，其下游 19 個 lock | **關鍵路徑。** 見下方「M2 場景保真度」一節 |

**唯一的關鍵路徑是 M2。** `locks status` 的鏈頭是 `initial_simulation`（pending），
22 個 lock 只凍了 1 個、19 個 BLOCKED。繞過 M2 去做 M4/M5 只會得到一堆
跑得動但一個 lock 都凍不了的模組，與「lock 不可跳步」的紅線方向相反。

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
| M2 Surrogate + E1 | **引擎完成，場景保真度未完** | signal/ambient/sigma 已有鑑別力；**distance 只修到一半**，見「M2 場景保真度」 |
| M3 Post-E1 Split | 未開始 | 依賴 E1 outcome（synthetic split 不得早於此） |
| M4 Perception | 未開始 | 需先安裝 tensorflow / scikit-learn |
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

### 一個尚未處理的守衛缺口

`_BOTTLE_SURFACE_ALPHA`、`_LEADING_MARGIN_BINS`、`bounce_budget`、
`_ROOM_LIGHT_RATIO`、`_SIGMA_T_REFERENCE_PER_M`、`_ALBEDO_BY_PRESET`
全都是**未校準的建模常數**，但它們不在 `surrogate/calibration.py` 的
placeholder 清單內，因此 **formal 模式的自動防線攔不到**。
凍結 `initial_simulation.lock` 前必須一併納入防線。

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

## 下一步（2026-08-27 晚間更新）

外部阻塞已全部解除：資料齊備、sigma 解出、real split 凍結、E1/E2 bootstrap 核定。
**Part VI、Batch 6、Batch 7 皆已完成**。

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
