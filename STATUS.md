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
py -3.10 -m pcmef.cli config check                   # 待教授裁決的 19 項
py -3.10 -m pcmef.cli locks status                   # 22 個 formal lock 的狀態
py -3.10 -m pcmef.cli sim smoke                      # M1 模擬 smoke
py -3.10 -m pcmef.cli provenance resolve-sigma       # SRC-D01/D02 證據
py -3.10 -m pcmef.cli audit real-data --source data/raw_real/... --out data/inventory
```

### 目前卡在哪

| 阻塞 | 影響 | 解法 |
|---|---|---|
| 真實 ToF 只剩窗口 Mean/Std | E1 的 (500,4) 契約 | 去樹莓派 `/home/pi/` 撈原始 CSV，或教授裁決改走彙總路線 |
| Sigma register 未定（0x18 vs 0x1E） | E1-G08、四特徵 primary | 需**採集當時的腳本**，不是已知那三份推論腳本 |
| 19 項數值未核定 | 全部 formal run | `config check` 有完整清單與出處 |

### 動手前必讀

- `docs/NOTES.md` — 12 則決策記錄。**改動前先查有沒有對應 NOTE**，
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
| M0 Data Audit | **部分可行** | Vision 資料完整；ToF 只剩衍生統計量（見 NOTE-011） |
| M1 Simulation | **smoke 通過** | mitsuba 3.8.0 / drjit 1.3.1 / mitransient 1.3.0 已安裝 |
| M2 Surrogate + E1 | 未開始 | 依賴 M0、M1 |
| M3 Post-E1 Split | 未開始 | 依賴 E1 outcome |
| M4 Perception | 未開始 | 需先安裝 tensorflow / scikit-learn |
| M5 Reliability/Gate | 未開始 | 依賴 M4 |
| M6 Multi-Agent | 未開始 | 依賴 M3 |
| M7 Pilot/Freeze | 未開始 | 依賴 M5、M6 |
| M8 Formal E2 | 未開始 | 依賴全部 |

Batch 進度依 SRC-SAI Appendix D「Recommended First Sprint」：

| Batch | 內容 | 狀態 |
|---|---|---|
| Batch 1 | core schema + SplitRole + InferencePayload + hashing/logging | **完成** |
| Batch 2 | LegacyCSVAdapter + nominal/usable count ledger + alignment | **完成（等真實資料）** |
| Batch 3 | Sigma/timing provenance resolver | **完成** |
| Batch 4 | Mitsuba/mitransient optical transient smoke adapter | **完成** |
| Batch 5 | single-acquisition surrogate + 500-point temporal model | 未開始 |
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

**尚未實作**：`core/splits.py`（SplitRegistry）。它在 §31 目錄樹中，
但 `real_split_policy` 的 creation_phase 是 `AFTER_M0_BEFORE_ANY_CALIBRATION`，
必須先有真實 inventory 才能建立，因此排在真實資料到位之後。

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

### SRC-D03（取樣間隔）→ 三來源偏差已量化

| 來源 | 值 | 對實測偏差 |
|---|---|---|
| SRC-PLAN 文件記載 | 0.082 s | **+31.4%** |
| SRC-NOTION 腳本設定 | 0.020 s | **−68.0%** |
| **實測（偏移測試 recording）** | **0.0624 s** | — |

只有 `measured` 等級可用於任何 recording 的契約；文件值與腳本值都只作記錄。

---

## Batch 4 已完成內容（2026-08-26）

測試：**472 passed**。M1 Simulation smoke 通過，E1-G03 的 artifact 已產出。

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

## 待教授裁決事項（formal-blocking，共 19 項）

執行 `py -3.10 -m pcmef.cli config check` 可隨時取得最新清單。
這些數值依 SRC-PLAN Appendix A 與 SRC-SAI Appendix F **禁止實作端自行補值**，
目前全部以 `!required` 標記，任何程式讀到即中斷。

| 分類 | 待裁決項目 |
|---|---|
| Real split | `allocation`、`minimum_per_class`、`seed` |
| E1 | `bootstrap_replicates`、`bootstrap_seed` |
| Perception | `training_seed_pairs` |
| Reliability | `crossfit_folds` |
| Gate | `alpha`、`beta`、`gamma`（須由 validation 搜尋選出後 freeze） |
| Agents | `representation_mode`、`retry.max_attempts` |
| E2 | `final_n_per_class`、`severity_allocation` |
| Conflict | `delta` |
| Statistics | `bootstrap_replicates`、`bootstrap_seed` |
| Sigma provenance | `resolved_register`、`status`（非教授裁決，由 M0 audit 產出，但同樣 formal-blocking） |

對應 SRC-PLAN Appendix A 的六個教授討論題目，其中第 1、5 題直接決定上表的
Real split 與 E2 兩組數值。

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

## 待教授裁決：E1 是否改走「彙總統計量 + 相同滑動窗口」

這是目前唯一影響 **claim 強度**的待決事項，與上表 19 項數值性質不同。

**背景**：真實 ToF 只剩窗口 Mean/Std，原始 500 點序列不存在（NOTE-011）。

**提案做法**：synthetic 算出 500 點後套用**完全相同的**滑動窗口
（window=10 / step=5 → 99 窗口）取 Mean/Std，再與真實側比 Wasserstein。
兩側處理一致，因此比較本身合法。

**三個代價**：

1. `e1_scientific_rule.lock` 的措辭必須在 Held-out 開啟**前**改寫 ——
   不再是「500 點行為的分佈」而是「窗口統計量的分佈」。
2. Secondary 的 temporal variability 降為 window-level。
3. **平均使分佈變窄**：10 點平均把 SD 壓約 √10，兩側分佈都變窄 →
   Wasserstein 距離變小 → **fidelity 測試比原設計容易通過**。
   這不是造假，但若不明寫，等於在較寬鬆的尺規上宣稱校準成功。

**替代路徑**：取得樹莓派上的原始逐 recording CSV，照原規格執行。

實作端不會自行選邊：這改變 E1 claim 的範圍與強度，屬研究主張層級決定。

---

## 下一步

1. **取得前研究原始資料** —— 這是目前唯一的關鍵路徑。
   Batch 2 的程式已完成，資料一到就能直接跑出真實的五層計數與排除帳。
   SRC-NOTION 掃描後有兩條線索：
   - 樹莓派本機（`/home/pi/`，採集腳本輸出目錄），
     連線資訊在 Notion「研究交接」首頁 —— 依 SRC-SAI §30，
     該帳密屬操作資訊，不寫入本 repo 或任何 config。
   - Notion 首頁的 Google Drive 連結（Kaleidagraph Plot、影像辨識程式）。
   取得後同時需要**採集當時的腳本**，才能解析 Sigma register（見 NOTE-010）。
2. 向教授確認上表數值中至少 Real split 與 E2 兩組。
3. **Batch 3**（Sigma/timing provenance resolver）可在資料到位後立即接續；
   若要在等待期間繼續推進，Batch 4（Mitsuba/mitransient smoke adapter）
   不依賴真實資料，但需先安裝 `mitsuba` / `drjit`。
