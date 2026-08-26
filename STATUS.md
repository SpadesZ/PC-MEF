# PC-MEF Research System — 進度真相

本檔是進度與交接的唯一真相來源。聊天訊息裡的說明不算完成。
刻意不另開 HANDOFF 檔：兩份文件必然漂移，屆時沒人知道該信哪一份。

最後更新：2026-08-26

---

## 接手指南（HANDOFF）

冷啟動接手時先看這一節，五分鐘內能跑起來。

### 環境

```powershell
py -3.10 -m pip install -e ".[dev,simulation]"
winget install --id LLVM.LLVM          # drjit 的 LLVM 後端需要
$env:PATH = "C:\Program Files\LLVM\bin;$env:PATH"
```

**三個不查會踩到的坑：**

1. **一律用 `py -3.10`。** PATH 上的 `python` 是 3.12 且沒裝相依。
2. **`LLVM-C.dll` 必須存在且在 PATH 上。** drjit 執行期動態載入它，
   但套件**不內含**（只有 `drjit-core.dll`）。缺了會看到
   `jitc_llvm_init(): LLVM API initialization failed`，
   且 `llvm_ad_rgb` variant 無法使用。
3. **本機無 NVIDIA GPU**（Intel Iris Xe），`cuda_ad_rgb` 永遠不可用，
   固定使用 `llvm_ad_rgb`。

### 常用指令

```powershell
py -3.10 -m pytest                                   # 全部測試（約 80 秒）
py -3.10 -m pcmef.cli config check                   # 待教授裁決的 10 項
py -3.10 -m pcmef.cli locks status                   # 22 個 formal lock 的狀態
py -3.10 -m pcmef.cli sim smoke                      # M1 模擬 smoke
py -3.10 -m pcmef.cli surrogate smoke                # E1-G04 四特徵無 NaN 驗證
py -3.10 -m pcmef.cli split plan-real --source data/raw_real/edge_impulse_export
py -3.10 -m pcmef.cli provenance resolve-sigma       # SRC-D01/D02 證據
py -3.10 -m pcmef.cli audit real-data --source data/raw_real/... --out data/inventory
```

### 目前卡在哪

| 阻塞 | 影響 | 解法 |
|---|---|---|
| ~~真實 ToF 只剩窗口 Mean/Std~~ | — | **已解除**：Edge Impulse export 復原 560 筆 500×4 |
| ~~Sigma register 未定~~ | — | **已解出 0x18**（排除檢驗，NOTE-010）；採集腳本仍未取得，取得後須複核 |
| ~~Real split 三值未裁決~~ | — | **已裁決並凍結** 392/168（NOTE-014） |
| ~~E1/E2 bootstrap 四值~~ | — | **已核定** 10000 / 20260826 / 20260827（NOTE-015） |
| 10 項數值未核定 | 全部 formal run | `config check` 有完整清單與出處；其中 `gate.*`、`e2.final_n_per_class` 須由搜尋或 pilot 產出，不是「請教授給數字」 |

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
| M2 Surrogate + E1 | **surrogate 完成、split 已凍結** | E1 metrics 待 Batch 6 |
| M3 Post-E1 Split | 未開始 | 依賴 E1 outcome（synthetic split 不得早於此） |
| M4 Perception | 未開始 | 需先安裝 tensorflow / scikit-learn |
| M5 Reliability/Gate | 未開始 | 依賴 M4 |
| M6 Multi-Agent | 未開始 | 依賴 M3 |
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
| Batch 6 | E1 metrics + dual-lock + scientific rule state machine | 未開始 |
| Batch 7 | E1-G01..G12 audit + heldout firewall + real split policy audit | 未開始 |
| Batch 8 | post-E1 split generators + parent-family checks | 未開始 |

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

## 規格對照稽核（2026-08-26）

以 SAI §31 目錄樹、E1-G01..G12、Appendix D Batch exit criteria 逐項盤點。

| 面向 | 現況 | 說明 |
|---|---|---|
| §31 目錄樹 | **17/62 檔** | 缺的全在 Batch 6-8 與 M4-M8 範圍（`core/splits.py` 已於 8/26 補上） |
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
| `jsonschema` | 未安裝 | M6 agent schema 驗證 |

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

## 下一步（2026-08-26 調整方向：先做 Part VI Admin UI 與稽核系統）

外部阻塞已全部解除：資料齊備、sigma 解出、real split 凍結、E1/E2 bootstrap 核定。

**Batch 6（E1 metrics + dual-lock）刻意暫緩**，改先做兩件事：

### 1. Part VI —— Admin LLM Setup UI（SAI §38、§42、§46、§50、§51、§52）

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
