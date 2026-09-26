# PC-MEF Research Platform
# System Analysis with AI（SAI）
## Extensible Research Workbench / Platformization Specification
### PC-MEF 可擴充多模態研究工作台—平台化、前端互動、研究設定檔與正式實驗隔離規格

**文件版本：** v0.7.2  
**文件性質：** Platformization / Extensibility / User Experience / Formal-Isolation SAI  
**日期：** 2026-09-25  
**取代：** `PC-MEF_SAI_v0.6.0_Extensible-Research-Workbench_Platformization.md`（2026-09-01）與 `docs/SAI_v0.6.0_TO_CURRENT_DELTA.md`（2026-09-06）。兩者內容已併入本文件，原檔移除。  
**上位相容文件：** `PC-MEF_SAI_v0.5.0_LLM-Setup_Task-Binding-Integrated`（原始碼與 NOTES 中代號 `SRC-SAI`，與本文件**不是同一份**）  
**對應實作：** branch `audit/execution-alias-and-ports-round8-20260925`，round 10 程式碼 `a08fb53`（round 9 程式碼 `5cbc43b`、round 8 程式碼 `eb8ad9c`）  
**研究核心：** 「結合物理校準模擬與大型語言模型輔助多模態融合之管內液態狀態辨識」既有碩士論文實驗核心  
**本版新增責任：** 將既有論文專用流程提升為可擴充的研究平台，同時保證既有碩論正式研究設定零漂移（zero scientific drift）。  

> **章節編號穩定。** 原始碼有 22 處以 `SAI v0.6.0 §N` 形式引用本文件
> （§3.1、§4.1、§6、§18、§19、§20、§23、§24、§38、§39、ACC-FML-02），
> 因此 v0.7.0 **不重新編號任何既有章節**。新增內容一律接在 §49 之後
> 與 Appendix G。引用時可直接寫 `SAI §N`，版本號不影響定位。

> **檔名刻意不帶版本號。** 版本寫在文件內、歷史留在 git。帶版本號的
> 檔名每改一次就多一份檔案，而那正是這份文件要收掉的問題。

---

# 0. 文件控制、來源、名詞與判讀邊界

## 0.1 文件目的

**PC-MEF Research Platform（PC-MEF 研究平台，以下簡稱「PC-MEF 平台」）**是由既有 PC-MEF Research System 延伸而成的多模態感測模擬、辨識、融合、可靠度分析與大型語言模型輔助決策研究工作台。

本文件定義的目標不是重寫既有論文演算法，而是把目前「高度綁定單一碩論實驗」的系統提升為：

1. 可以由前端建立與管理不同研究設定；
2. 可以新增不同情境狀態，例如 Sand / granular material；
3. 可以新增不同感測模態，例如 Infrared（IR）、Thermal、其他 Time-of-Flight（ToF）感測器；
4. 可以調整開發階段參數；
5. 可以預覽模擬、建立資料、掛接模型、啟動實驗與即時監控；
6. 可以把一組開發設定凍結為不可任意修改的正式研究設定；
7. 可以同時保護目前碩士論文的 frozen scientific identity，不因平台化而改變現有 E1 / E2 方法、參數、資料隔離或統計規則。

本文件屬於**工程架構與研究治理規格**，不新增任何新的科學研究主張。新增情境或感測器是否具備科學有效性，必須由對應研究計畫、資料與驗證證據另行建立。

---

## 0.2 本文件使用的來源

| Source ID | 來源 | 本文件使用方式 |
|---|---|---|
| SRC-SAI-050 | `PC-MEF_SAI_v0.5.0_LLM-Setup_Task-Binding-Integrated` | 沿用既有 formal-safe、provenance、Inference Firewall、LLM Registry、Task Binding、freeze、artifact、CLI parity 與 formal isolation 原則 |
| SRC-PLAN | 目前 PC-MEF 實驗計畫與正式 E1/E2 決策 | 定義現有 Thesis Core 的 scientific identity，不由平台化改寫 |
| SRC-WRITE | `研究型實驗計畫書通用寫作規則_v3.2_8Skill_Audited` | 採用 Experimental Unit、Pilot/Formal separation、Code–Plan Alignment、Artifact Passport、Formal-Run Entry Gate、Failure/Falsifier、Traceability |
| SRC-8SKILL | `通用論文八技能整合與路由附錄_V1.0_20260820` | 使用其「工具輸出不得自行升格為研究結論」、「版本／證據／Human review」治理思想 |
| DEC-PLATFORM | 本文件新增的工程設計決策 | Research Profile、Scenario Plugin、Sensor Adapter、Development/Formal Workspace、Web Workbench、Acceptance Matrix；屬平台化決策，不是科學事實 |

---

## 0.3 名詞定義規則

本文件所有縮寫、自創名詞與系統專有詞第一次出現時，先給完整名稱，再定義後續簡稱。

### 0.3.1 Physics-Calibrated Multimodal Evidence Fusion

**Physics-Calibrated Multimodal Evidence Fusion（PC-MEF，物理校準多模態證據融合）**：本專案中對既有多模態辨識與選擇性仲裁研究架構的專案名稱。其正式科學 claim 仍受現有 E1/E2 frozen research artifacts 約束。

### 0.3.2 Research Profile

**Research Profile（研究設定檔）**：本文件定義的版本化研究設定容器。它把情境、感測器、模擬器、資料、模型、融合、壓力條件、統計、LLM、執行環境與 provenance 綁成一個可追蹤的研究單位。後文簡稱 **Profile**。

### 0.3.3 Development Research Profile

**Development Research Profile（開發研究設定檔）**：允許調整與探索的 Profile。後文簡稱 **Development Profile**。

### 0.3.4 Frozen Research Profile

**Frozen Research Profile（凍結研究設定檔）**：已通過必要驗收並建立 immutable identity 的 Profile。後文簡稱 **Frozen Profile**。Frozen Profile 不允許原地修改 scientific fields；任何研究性變更必須 Clone 成新的 Development Profile。

### 0.3.5 Thesis Research Profile

**PC-MEF Thesis 2026 RGB–ToF Four-State Research Profile（2026 PC-MEF 碩論 RGB–ToF 四狀態研究設定檔）**：用來封裝目前碩士論文之既有 E1/E2 scientific identity。後文簡稱 **Thesis Profile**。它是本次平台化最重要的保護對象。

### 0.3.6 Scenario Definition

**Scenario Definition（情境定義）**：描述研究中的狀態／材料／介質／幾何／環境與可觀察 ground-truth semantics 的版本化物件。後文簡稱 **Scenario**。

### 0.3.7 Scenario Plugin

**Scenario Plugin（情境外掛）**：把特定 Scenario 類型轉成模擬器可執行 scene / medium / geometry / boundary / condition 的擴充模組。不是所有新 Scenario 都能完全無程式碼建立；若現有模板無法表示其物理行為，必須新增 Plugin 並通過驗證。

### 0.3.8 Sensor Adapter

**Sensor Adapter（感測器轉接器）**：將某一感測器的模擬、真實量測、觀測資料結構、品質訊號、特徵摘要、視覺化與 perception interface 正規化為 PC-MEF 平台可使用的介面。後文簡稱 **Sensor Adapter**。

### 0.3.9 Modality Evidence Contract

**Modality Evidence Contract（模態證據契約）**：不同感測器輸出至上層模型與融合層時必須滿足的標準證據結構。後文簡稱 **Evidence Contract**。

### 0.3.10 Development Workspace

**Development Workspace（開發工作區）**：允許建立、修改、預覽、模擬、試跑與比較 Development Profile 的前端操作空間。

### 0.3.11 Formal Research Workspace

**Formal Research Workspace（正式研究工作區）**：只能載入 Frozen Profile，進行 pre-flight、啟動、監控、合法 resume / abort、統計與 artifact 檢視的前端空間。不得在此直接改 scientific parameters。

### 0.3.12 Formal Run

**Formal Run（正式執行）**：以 Frozen Profile、固定 code/runtime identity 與預先定義 validity rule 執行的正式研究 run。正式結果是否成功由科學結果決定；只有事前定義的 execution-invalid condition 才可標記為 `RUN_INHIBITED`。

---

## 0.4 本文件與實作不一致時的裁決規則

SAI 不是外部草稿，它是**已經被實作進來的規格**：原始碼多處直接引用
本文件的章節與 `ACC-*` 驗收編號。但文件會落後於實作，因此需要一條
固定的裁決規則，而不是每次現場判斷。

**判定規則（v0.6.0 → v0.7.0 沿用）：**

1. 本文件與 CURRENT Formal Production Path 衝突 → **一律以 CURRENT 為準**。
2. 本文件未涵蓋而 CURRENT 已實作 → 文件視為**沉默**，不得當成「可以簡化」。
3. 本文件有、CURRENT 尚未實作且不衝突 → **優先重用本文件設計**，不另發明。

規則 2 是三條裡最容易被違反的一條。v0.6.0 時期就發生過：文件沒寫
one-shot claim，於是「照文件重寫一份」會把已經修好的獨佔保護拿掉。
**沒寫不等於不需要。**

本版已依此規則把 2026-09-02 ~ 2026-09-25 之間的實作收斂回文件，
逐項對照見 **Appendix G**。

---

# 1. Executive Summary

PC-MEF 平台 v0.6.0 將既有研究系統從「特定四類液態 + RGB/ToF + 固定 E1/E2」提升成「可建立多個研究 Profile 的可擴充研究工作台」。

平台化的核心不是讓使用者直接改既有碩論正式設定，而是建立兩條互相隔離的操作路徑：

> **Development Workspace：可以新增、調參、試驗、Clone、預覽與探索。**

> **Formal Research Workspace：只接受 Frozen Profile，提供啟動、可視化、即時監控、有效性檢查與結果輸出。**

平台未來可加入 Sand、Oil、Powder 等 Scenario，也可加入 Infrared、Thermal、Spectral、不同 Time-of-Flight sensors 等 Sensor Adapter。但新增一個「名稱」不代表科學模型自然成立。前端必須明確區分：

1. **Template-backed configuration**：現有物理／資料模板已能表示，可由使用者低程式碼或無程式碼設定；
2. **Plugin-required extension**：需要新的 simulator mapping、observation schema 或 perception logic，前端引導建立 Plugin requirement，不假裝可以單靠表單產生正確物理模型。

本次平台化最高優先規則：

> **Thesis Profile 的 scientific identity 不得因平台重構而漂移。**

因此所有平台化工作必須通過 Thesis Profile Golden Regression，證明既有正式資料、模型、routing、Agent evidence contract、formal executor 與 statistics identity 不被新 UI / DB / Plugin mechanism 改寫。

---

# 2. Platformization Goals 與 Non-Goals

## 2.1 Goals

### G-01 可擴充研究設定
使用者可建立多個 Development Profile，不再把所有研究設定硬編碼在單一 config / CLI / Python branch。

### G-02 使用者友善新增 Scenario
一般使用者透過 Wizard 建立情境狀態；常見模板不需接觸 YAML、JSON、Python。

### G-03 使用者友善新增 Sensor
前端可建立感測器 instance、調整參數與掛載已存在的 Sensor Adapter；若為全新感測物理，系統明確提示 Plugin Required。

### G-04 參數可視化與分層
Basic Mode 只顯示安全且常用參數；Advanced Mode 才顯示底層 solver / model / runtime settings。

### G-05 可視化模擬預覽
建立或修改 Scenario / Sensor 後，可先跑低成本 Preview，不需直接產正式 dataset。

### G-06 啟動與即時監控
Web 可啟動開發 run / pilot / formal run，顯示進度、當前 scenario、sensor、routing、LLM、provider、tokens、latency、cost、artifact 與 validity state。

### G-07 Formal isolation
Frozen Profile 不能被 UI 直接改動；任何研究性變更必須 Clone。

### G-08 可追溯
任一結果都能回查 Profile version、Plugin version、Scenario version、Sensor version、code revision、model、LLM runtime、seed、artifact、operator、timestamp。

### G-09 CLI / Service / Web parity
Web 不自行重寫 scientific logic；Web、CLI、service API 最終呼叫相同 application service / production executor。

### G-10 未來教授臨時需求可吸收
對「加沙子」、「換 ToF」、「加紅外線」、「改某參數看看」等要求，不需污染 Thesis Profile。

---

## 2.2 Non-Goals

v0.6.0 不要求：

- 自動發明未知感測器的物理模型；
- 自動保證新增 Scenario 的科學有效性；
- 把所有第三方 simulator 立即整合；
- 將 PC-MEF 變成公開 SaaS；
- 為了平台化重新調整現有 E1/E2 科學參數；
- 重新訓練 Thesis Profile checkpoints；
- 重新開啟已封存的 formal families；
- 用新 UI 取代 existing provenance / formal locks。

---

# 3. 高階架構

```text
┌─────────────────────────────────────────────────────┐
│                  PC-MEF Web Workbench               │
│                                                     │
│ Dashboard / Profiles / Scenario / Sensors / Runs   │
│ Simulation / Dataset / Models / LLM / Results      │
└───────────────────────┬─────────────────────────────┘
                        │
                Application Services
                        │
       ┌────────────────┼───────────────────┐
       │                │                   │
 Profile Service   Experiment Service   Monitor Service
       │                │                   │
       └──────────┬─────┴─────────┬─────────┘
                  │               │
          Plugin / Adapter Layer  │
     ┌────────────┼────────────┐   │
     │            │            │   │
 Scenario      Sensor       Provider
 Plugins       Adapters      Adapters
     │            │            │
     └──────┬─────┴────────────┘
            │
      Scientific Core
 Simulation / Perception / Reliability /
 Fusion / Agents / Statistics / Provenance
            │
      Artifact + Registry
```

## 3.1 最高架構原則

1. UI 不直接 import scientific modules 後自行拼流程。
2. Web 與 CLI 必須透過同一 Application Service。
3. Formal Run 必須透過唯一 Formal Production Executor。
4. Plugin 只擴充 capability，不可繞過 Profile / provenance / validity。
5. Profile 是所有執行的設定入口。
6. Frozen Profile 是 immutable scientific identity。
7. Live UI setting 不得穿透既有 Formal Run。

---

# 4. Research Profile Architecture

## 4.1 Profile State Machine

```text
DRAFT
  ↓
CONFIGURED
  ↓
VALIDATED
  ↓
PILOT_READY
  ↓
FREEZE_CANDIDATE
  ↓
FROZEN
  ↓
FORMAL_READY
  ↓
FORMAL_RUNNING
  ↓
FORMAL_COMPLETE
      or
RUN_INHIBITED
```

### DRAFT
可自由編輯，允許缺欄位。

### CONFIGURED
必填設定完整，但尚未驗證 scientific / adapter compatibility。

### VALIDATED
schema、capability、compatibility、basic smoke tests 通過。

### PILOT_READY
可執行開發／pilot。

### FREEZE_CANDIDATE
準備建立 immutable scientific identity。

### FROZEN
禁止原地修改 scientific fields。

### FORMAL_READY
Formal Entry Gate 全部通過。

### FORMAL_RUNNING
正式執行中。

### FORMAL_COMPLETE
Validity PASS，正式輸出完成。

### RUN_INHIBITED
符合事前定義 execution-invalid condition；保留 run lineage。

---

## 4.2 Profile 內容

```yaml
profile_identity:
  profile_id:
  display_name:
  version:
  state:
  parent_profile_id:
  created_at:
  created_by:

research_scope:
  title:
  task_type:
  research_question_refs:
  class_space:
  claim_boundary:

scenarios:
  scenario_definition_ids:

sensors:
  sensor_instance_ids:

simulation:
  engine:
  engine_version:
  scene_defaults:
  sampling:
  seeds:

datasets:
  sources:
  split_policy:
  family_policy:

perception:
  modality_models:
  checkpoints:

reliability:
  estimator:
  parameters:

fusion:
  strategy:
  parameters:

llm:
  roles:
  runtime_snapshot:

stress:
  conditions:
  severity:

statistics:
  primary_endpoint:
  resample_unit:
  bootstrap:
  seed:

formal_policy:
  inclusion:
  exclusion:
  abort:
  resume:
  validity_rule:

provenance:
  code_revision:
  environment:
  artifact_schema:
```

---

# 5. Thesis Profile Protection Contract

## 5.1 Protected Profile

平台初次導入時必須先建立：

**PC-MEF Thesis 2026 RGB–ToF Four-State Research Profile**

並把目前既有正式 research identity 映射成 Profile，而不是重新生成研究設定。

Profile 至少保存：

- 四類：Empty、Water-filled、Bubbly、Misty；
- RGB + ToF；
- 現有 partial / Ambient-calibrated simulator identity；
- effective validation lineage；
- fixed stress severity；
- fixed perception checkpoints；
- fixed reliability / gate；
- fixed Agent prompts / schemas / evidence contract；
- fixed model + revision；
- formal families 36–43 identity；
- family_domain=44；
- formal statistics；
- formal validity policy。

## 5.2 不允許的平台化行為

平台化過程禁止：

- 將 class space 改成 dynamic list 後造成 Thesis Profile order 漂移；
- 把 Sensor Adapter abstraction 引入後改變 ToF 500×4 semantics；
- 為了 generic fusion 修改現有 selective escalation；
- 用新 DB default 覆蓋 frozen config；
- 把 live UI parameters 自動寫回 Frozen Profile；
- 把 Formal executor 改成依畫面當下值執行；
- 為了支援任意 scenario 重新生成 final families；
- 為了方便 UI 而降低 Inference Firewall。

---

# 6. Thesis Profile Golden Regression

> **v0.7.0 更新（原 delta D-06）：** canonical baseline **必須綁定
> lineage lock hash + runtime identity + code revision**。手寫
> `{"canonical": true}` 不再被接受 —— 不綁 identity 的 baseline 是
> 宣稱，不是基準。實作見 `experiments/regression_snapshot.py`；
> 目前 `regression/provisional/thesis_regression_snapshot.json` 的
> `canonical=false`，尚有 4 條 canonical_blocker 未解除。
> 下方 §6.2 的 TGR-01~12 比對項清單本身仍然有效，直接沿用。

## 6.1 定義

**Thesis Profile Golden Regression（碩論設定檔黃金回歸測試）**：平台化前後，以同一組已允許測試資料與 frozen identity 驗證 scientific behavior 是否一致。

## 6.2 必測項

| ID | 比對項 | PASS 條件 |
|---|---|---|
| TGR-01 | class order | byte/semantic identical |
| TGR-02 | scenario identity | identical |
| TGR-03 | sensor observation schema | identical |
| TGR-04 | ToF channel semantics | identical |
| TGR-05 | perception prediction | deterministic tolerance identical |
| TGR-06 | reliability values | identical |
| TGR-07 | route decision | identical |
| TGR-08 | LLM role evidence projection | same frozen evidence contract |
| TGR-09 | selective escalation result | identical for cached deterministic test |
| TGR-10 | statistics resample unit/seed | identical |
| TGR-11 | frozen formal config hash semantics | no silent fallback |
| TGR-12 | truth firewall | no added inference-visible truth field |

任一 MUST 項 FAIL：

> `PLATFORMIZATION_NO_GO`

直到 root cause 被修復。

---

# 7. Scenario Library 與使用者友善新增情境

## 7.1 UX 目標

教授或研究生不應需要知道 Python class 名稱或 YAML schema 才能新增情境。

新增 Scenario 的第一層 UI 使用「研究語言」，不是「程式語言」。

首頁操作：

```text
Scenarios
  ├─ Empty
  ├─ Water-filled
  ├─ Bubbly
  ├─ Misty
  └─ + Add Scenario
```

按 `+ Add Scenario` 後進入 Scenario Wizard。

---

## 7.2 Scenario Wizard

### Step 1 — 你要新增什麼？

大按鈕模板：

- Empty / Gas
- Liquid
- Liquid with Bubbles
- Aerosol / Mist
- Granular Material
- Solid Object
- Custom

例如教授說「加沙子」：

```text
Granular Material
→ Sand
```

系統不要求先輸入 plugin ID。

### Step 2 — 基本資料

- Display name：Sand
- Canonical name：Granular Sand-Filled State
- Short description
- Category：Granular Material
- 是否為現有研究類別 extension？
- 是否新增為 classification label？
- 是否只是 environment / nuisance state？

### Step 3 — 物理模板

依 category 顯示不同表單。

Granular Material 可呈現：

- fill level
- particle size range
- packing density
- optical scattering preset
- reflectance preset
- moisture state
- container interaction

Basic Mode：
只顯示 5–8 個有研究意義的欄位。

Advanced Mode：
才展開 Mitsuba material、phase function、solver-specific fields。

### Step 4 — Sensor Compatibility

畫面：

| Sensor | Status | 動作 |
|---|---|---|
| RGB | Supported | Configure |
| ToF | Needs Validation | Configure |
| IR | Not Installed | Add Sensor |
| Thermal | Unsupported | Plugin Required |

### Step 5 — Preview

低成本預覽：

- RGB preview；
- geometry preview；
- sensor FOV；
- sample observation；
- warning；
- estimated runtime。

### Step 6 — Validation

自動檢查：

- required parameters；
- physical range；
- unsupported combination；
- missing sensor adapter；
- label collision；
- scene identity collision；
- unit mismatch。

### Step 7 — Save

按鈕：

- `Save Draft`
- `Save & Run Preview`
- `Save as New Version`

禁止把新 Scenario 直接加進 Frozen Thesis Profile。

---

## 7.3 Template-backed 與 Plugin-required

前端必須清楚顯示：

### Template-backed
現有 simulator / material / observation pipeline 可以表示。

可由 UI 完成。

### Plugin-required
缺少必要 physics / mapping / summary / visualizer。

系統顯示：

> 此情境可建立定義，但目前尚無完整模擬能力。建立 Scenario Plugin 後才能進入 simulation / formal research。

不得假裝任何新材料只要填幾個值就科學成立。

---

# 8. Sensor Library 與使用者友善新增感測器

## 8.1 Sensor Library

畫面：

```text
Sensors

[ RGB Camera ]
Status: Ready

[ Time-of-Flight / VL53L0X-like ]
Status: Ready

[ + Add Sensor ]
```

---

## 8.2 Add Sensor Wizard

### Step 1 — 選擇感測類型

- RGB Camera
- Time-of-Flight
- Infrared Camera
- Thermal Camera
- Spectral Sensor
- Ultrasonic
- Import Existing Sensor Plugin
- Custom Sensor

### Step 2 — 選擇建立方式

#### A. Existing Adapter
例如已有 Generic Infrared Adapter。

只需建立 Sensor Instance。

#### B. Compatible Sensor Variant
例如另一顆 ToF。

Clone 現有 ToF Adapter configuration，再填：

- device/model；
- FOV；
- range；
- resolution；
- channels；
- acquisition interval；
- calibration；
- noise model。

#### C. New Sensor Family
系統顯示：

> 需要新的 Sensor Adapter implementation。

Wizard 仍可協助建立：

- identity；
- observation schema；
- parameter schema；
- required capability checklist；
- plugin skeleton metadata；

但不自動宣稱 simulator 已正確。

---

## 8.3 Sensor Adapter Contract

```text
SensorAdapter
│
├─ identity()
├─ capabilities()
├─ parameter_schema()
├─ validate_config()
│
├─ simulate(scene, config)         [optional]
├─ ingest_measurement(source)      [optional]
│
├─ observation_schema()
├─ summarize_evidence()
├─ quality_signals()
├─ visualize()
│
└─ perception_interface()
```

要求：

- 至少 `simulate` 或 `ingest_measurement` 其中之一成立；
- observation schema 必須版本化；
- units 必須顯式；
- quality signal 必須與 predictive probability 分開；
- summary 不得混合不同物理 channel 而失去 semantic meaning；
- Adapter 版本進 provenance。

---

# 9. Modality Evidence Contract

不同 sensor 不應要求 PC-MEF core 到處寫：

```text
if sensor == RGB
if sensor == ToF
if sensor == IR
```

上層統一取得：

```yaml
ModalityEvidence:
  modality_id:
  sensor_instance_id:
  observation_ref:
  observation_schema_version:
  prediction_distribution:
  quality_signals:
  reliability_evidence:
  summary:
  missingness:
  provenance:
```

這是平台層 contract。

但 Thesis Profile 的現有 RGB / ToF evidence contract 仍保留 frozen semantics。

---

# 10. Sensor × Scenario Compatibility Matrix

平台新增中央 compatibility registry。

例如：

| Scenario | RGB | ToF | IR | Thermal |
|---|---|---|---|---|
| Empty | Ready | Ready | Pending | Pending |
| Water-filled | Ready | Ready | Pending | Pending |
| Bubbly | Ready | Ready | Pending | Pending |
| Misty | Ready | Ready | Pending | Pending |
| Sand | Template | Needs Validation | Plugin Required | Plugin Required |

Status enum：

- READY
- TEMPLATE_SUPPORTED
- NEEDS_VALIDATION
- PLUGIN_REQUIRED
- UNSUPPORTED
- BLOCKED

使用者選到不相容組合時，不到執行才報錯，而是在 Profile Builder 立即顯示。

---

# 11. Profile Builder 前端設計

## 11.1 目標

建立研究設定時採 Wizard + Summary，不要求使用者依序跑 CLI。

流程：

```text
Create Profile
  ↓
Research Scope
  ↓
Scenarios
  ↓
Sensors
  ↓
Simulation
  ↓
Models
  ↓
Decision / Fusion
  ↓
Stress / Evaluation
  ↓
Statistics
  ↓
Review
  ↓
Save Development Profile
```

---

## 11.2 Basic / Advanced Mode

### Basic Mode
教授 / 一般研究生優先。

例如 ToF：

- FOV
- noise level
- sample count
- range
- sensor model

### Advanced Mode
再顯示：

- transient bins
- render spp
- integrator
- low-level mapping
- runtime/cache
- internal hashes

避免首頁塞滿工程欄位。

---

## 11.3 Smart Defaults

可用 preset：

- Current Thesis Default
- Fast Preview
- High Quality Simulation
- Sensor Validation
- Custom

但 Smart Default 必須顯示來源：

```text
Preset: Current Thesis Default
Source: Thesis Profile vX
Status: Protected reference
```

不能 silently 套用。

---

## 11.4 Unsaved / Invalid / Scientific-risk 提示

前端至少區分：

- `Unsaved`
- `Schema Invalid`
- `Capability Missing`
- `Scientific Validation Required`
- `Ready for Preview`
- `Ready for Pilot`
- `Frozen`

不要只顯示綠勾／紅叉而無原因。

---

# 12. Simulation Workspace

## 12.1 功能

- 選 Profile；
- 選 Scenario；
- 選 Sensors；
- 調 development parameters；
- Fast Preview；
- Full Simulation；
- paired multi-sensor generation；
- artifact viewer；
- run history。

## 12.2 Preview 與 Formal 分離

**Preview Simulation** 可降低：
- resolution；
- spp；
- realization count。

但畫面要明確：

> PREVIEW — NOT FORMAL EVIDENCE

Formal dataset generation 必須使用 Frozen Profile 的 exact settings。

---

# 13. Dataset Workspace

功能：

- dataset inventory；
- class / family / realization counts；
- split view；
- leakage / collision checker；
- scenario family tree；
- generated / read / sealed status；
- provenance；
- export manifest。

新增 Scenario 後，要能看：

```text
Sand
  families: 0
  samples: 0
  split: not configured
  status: development only
```

不能自動把它塞進既有 Thesis Profile split。

---

# 14. Model & Perception Workspace

功能：

- modality model registry；
- checkpoint identity；
- training dataset；
- compatible observation schema；
- training history；
- evaluation；
- attach existing model；
- disable model；
- clone config。

Sensor Adapter 新增後，若沒有 compatible model：

> `PERCEPTION_MODEL_REQUIRED`

Profile 不得假裝可直接進 multimodal inference。

---

# 15. Fusion / PC-MEF Workspace

Development Mode 可建立：

- single-modality；
- fixed fusion；
- reliability routing；
- selective escalation；
- experimental strategy。

但 Thesis Profile 對應的 gate / thresholds / selective escalation contract 顯示：

```text
FROZEN BY THESIS PROFILE
```

若按 Edit：

> Clone to Development Profile

---

# 16. LLM Agents Workspace

沿用既有 LLM Registry / Task Binding 設計，新增 Profile-aware view。

顯示：

- provider；
- connection；
- model；
- revision；
- capability；
- role binding；
- prompt version；
- schema；
- evidence contract；
- cache；
- timeout。

Development Profile 可改 binding。

Frozen Profile 僅顯示 snapshot。

---

# 17. Experiment Workspace

分為三類：

### 17.1 Preview Run
快速檢查。

### 17.2 Pilot / Development Run
允許探索；任何改動留 iteration log。

### 17.3 Formal Run
只接受 Frozen Profile。

---

# 18. Formal Run 唯一 production path

> **v0.7.0 更新（原 delta D-01 ~ D-04）：** 本節原本只要求 fail-closed
> 與 identity mismatch refuse。CURRENT 在其上實作了 **one-shot claim
> 狀態機**，這一層文件原本是沉默的 —— 而沉默被當成「可以不做」，
> 會直接讓一次性實驗失去獨佔保護。

**Formal one-shot claim（`pcmef/experiments/run_claim.py`）**

| 狀態 | 意義 |
|---|---|
| `RESERVED` | 已取得名額，尚未開始執行 |
| `RUNNING` | 執行中 |
| `COMPLETE` | 已完成，該次一次性實驗用掉了 |
| `INTERRUPTED_RESUMABLE` | 中斷但可續跑 |

- claim 以 `O_EXCL` 建立；狀態推進採單調遞增 `revision` 加上
  `.RUN_CLAIM.json.rev<N+1>` 的 `O_EXCL` 權杖 CAS。
- **刻意不用 lock 檔**：孤兒鎖沒有人能釋放。
- 任何「讀出來改一改寫回去」的 claim / profile 儲存實作都會重新引入
  read-then-write 競態，**不得如此實作**。
- identity **只由 claim layer 驗證**；pre-flight 刻意不重複驗一次。
  兩份實作必然漂移，而較寬鬆的那一份會先跑。
- claim 生命週期在 `_execute_formal_e2` 之外，該層只有一個 try；
  卡住的 claim 由 `pcmef formal reclaim` 標回可續跑 —— 卡在 `RUNNING`
  的 claim 會**同時**擋掉 fresh restart 與 resume。

```text
Web UI ─────┐
            │
CLI ────────┼──> FormalExperimentService
            │          ↓
Service API ┘    Formal Production Executor
                       ↓
                  Validity + Artifacts
```

Web 不得另外實作一份 Formal loop。

Formal production entry 必須：

- explicit active lineage；
- fail-closed；
- 不允許 fallback legacy deterministic path；
- 真正呼叫 Full PC-MEF production executor；
- identity mismatch 立即 refuse；
- output immutable run record。

---

# 19. Formal Research Workspace：即時執行畫面

## 19.1 頁首 Identity Bar

顯示：

- Profile
- Profile version
- state
- run_id
- code revision
- runtime identity
- formal config identity
- active lineage
- provider/model revision
- Validity Gate

---

## 19.2 Pre-flight Panel

```text
[PASS] Profile Frozen
[PASS] Scenario identity
[PASS] Sensor adapters
[PASS] Dataset seal
[PASS] Model/checkpoints
[PASS] LLM runtime
[PASS] Formal executor
[PASS] Statistics config
[PASS] Output location
```

任何 MUST FAIL：

`START FORMAL RUN` disabled。

---

> **v0.7.0 更新（原 delta D-05）：** 上方列的 pre-flight 項目是
> **畫面顯示層**。Final E2 的實際進入條件是
> `pcmef/experiments/final_gate.py` 的 **8 項機器強制前置條件**，
> 兩者不是同一組，用途也不同。**判定一律以 final_gate 為準**，
> 本節只作顯示參考。8 項現況見 §51.1。

---

## 19.3 Progress Panel

例如 Thesis Profile：

```text
Formal E2

Rows completed: 137 / 384
Base scenarios: 42 / 96
Families completed: 13 / 32
Elapsed: ...
Estimated remaining: ...
```

---

## 19.4 Current Case

顯示 evaluator-side metadata：

- class / family / realization / condition；
- sensors；
- status；
- method currently running。

注意：

> evaluator UI 可以顯示 ground truth，但 inference payload 仍由 Firewall 保證不可見。前端顯示與 provider input 必須是不同資料流。

---

## 19.5 Multi-Method Status

```text
Vision-only              PASS
ToF-only                 PASS
Fixed Fusion             PASS
Deterministic Routing    PASS
Full PC-MEF              RUNNING
```

---

## 19.6 Routing / Agent Status

```text
Route counts
trust_vision: ...
trust_tof: ...
fusion: ...
escalated: ...
```

Escalated case：

```text
Observation       PASS       11.2 s
Physics           PASS       10.8 s
Visual-Semantic   RUNNING
Arbitration       WAITING
```

---

## 19.7 Provider Monitor

- requests；
- retries；
- 429；
- timeout；
- latency；
- prompt tokens；
- output tokens；
- thinking tokens；
- estimated cost；
- connection usage。

---

## 19.8 Validity Monitor

顯示：

- dropped cases；
- duplicate IDs；
- model identity mismatch；
- schema failure；
- deterministic substitute；
- provider exhaustion；
- artifact write failure；
- resume identity mismatch；
- evidence contract violation。

---

## 19.9 Run Controls

Formal Running 時只允許：

- View
- Expand details
- Export log
- Abort Run

禁止：

- 改 severity；
- 改 model；
- 改 threshold；
- skip case；
- rerun selected failure；
- swap sensor；
- edit Profile。

Abort 必須留下 immutable run record。

---

# 20. 前端導航資訊架構

> **v0.7.0 更新（原 delta D-08，本版最關鍵的 UI 變更）：** v0.6.0 列的
> 13 個頂層項目是「功能清單」；CURRENT 收斂成 **五個入口**，每一項的
> hint 是一個**問句**。判準因此改變：一個功能該放哪裡，看它回答的是
> 哪一個問題，而不是它在哪個模組裡實作。
>
> `pcmef/console/navigation.py` 的維護契約明文寫著：
> **「不得為了新功能再加第六個頂層項目。」**
> 五個入口是刻意的收斂；新功能要歸進其中一個，否則首頁會重新開始
> 堆疊 —— 那正是要修的問題。

**第一層：五個主入口**（`NAV_ITEMS`，順序即操作順序）

| key | 標籤 | 回答的問題 |
|---|---|---|
| `run` | 實驗 Run | 我要跑什麼？ |
| `pipeline` | 流程 Pipeline | 系統怎麼跑？ |
| `results` | 結果 Results | 跑出了什麼？ |
| `status` | 研究狀態 Status | 研究目前做到哪裡？ |
| `llm` | LLM 設定 | provider / model / binding |

**Project 不是第六個 tab**，而是五個入口**之上**的一層
（Workspace → Project → 五入口）。麵包屑根節點為 Workspace，
**不得寫死成 `PC-MEF`** —— 寫死的話，切換 Project 之後麵包屑仍然
說 PC-MEF。

**第二層：單次 run 的七個分頁**（`RUN_SECTIONS`，順序照資料流）

| key | 標籤 | 回答的問題 |
|---|---|---|
| `overview` | 總覽 Overview | 這次執行整體發生了什麼？ |
| `trace` | 流程追蹤 Trace | 每一筆是怎麼被判斷的？ |
| `inputs` | 輸入 Inputs | 餵進去的是什麼？ |
| `intermediate` | 中間結果 Intermediate | 中途產生了什麼？ |
| `outputs` | 輸出 Outputs | 得到什麼結論？ |
| `cost` | 用量與成本 Cost | 這次花了多少？ |
| `artifacts` | Artifacts | 檔案落在哪裡？ |

Cost 排在 Outputs 之後、Artifacts 之前：它是那份輸出的代價，屬於結果
的一部分，不是檔案清單。Artifacts 放最後，它回答的是「檔案在哪」而
不是「發生了什麼」。

沒有內容的分頁**仍然顯示**，只標成 disabled。藏起來會讓人以為系統
沒有這個能力，而事實是「這種 run 不產生那一層」—— 差別在畫面上必須
看得出來。

**術語裁決：** v0.6.0 通篇稱 **Research Profile**，平台化任務書稱
**Project**，兩者指同一層（版本化的研究設定容器 + namespace 邊界）。
對外顯示與新程式碼一律用 **Project**，schema 內保留 `profile_state`
對映欄位，讓 §4.1 狀態機仍可直接引用。
**不同時維護兩套詞彙** —— 兩套詞彙必然漂移。

---

# 21. Dashboard

Dashboard 只回答：

1. 現在有哪些 Profile？
2. 哪些正在跑？
3. Thesis Profile 是否安全？
4. 有沒有 blocker？
5. 最近產生哪些 artifacts？

卡片：

```text
Thesis Profile
FROZEN
Scientific drift: NONE
Formal readiness: PENDING / READY
```

```text
Sand Extension
DEVELOPMENT
Scenario validation: 4/6
Sensor compatibility: 1 missing
```

---

# 22. Database / Registry Concept

SQLite 可作 metadata/index；immutable scientific artifacts 留 filesystem/content-addressed store。

建議 table：

```text
research_profiles
profile_versions
scenario_definitions
scenario_versions
scenario_plugins

sensor_adapters
sensor_instances
sensor_versions

compatibility_matrix

model_registry
model_bindings

experiment_runs
run_events
run_progress

artifact_registry
profile_artifact_links

formal_snapshots
formal_validity_reports

audit_log
```

---

# 23. Immutable / Mutable Field Policy

## 23.1 Development Profile

Mutable。

但每次 Save 建 profile revision。

## 23.2 Frozen Profile

Scientific fields immutable。

可變的 operational metadata：

- display note；
- connection credential rotation；
- UI labels；

也必須有 audit。

會改 output semantics 的項目：
不得直接改。

---

# 24. Clone Workflow

教授說：

> 「ToF severity 改 0.1 看看。」

若目前開的是 Frozen Thesis Profile：

```text
Edit
  ↓
This profile is frozen.
  ↓
Clone as Development Profile?
  ↓
PC-MEF-Thesis-Variant-ToF-0.1
```

原 profile 0 drift。

---

# 25. 新增 Sand 的完整使用者閉環

```text
1. Profiles
2. Clone Thesis Profile
3. Rename: Sand Extension
4. Scenarios → Add Scenario
5. Granular Material → Sand
6. 設定 basic physical parameters
7. Compatibility Check
8. RGB: supported
9. ToF: needs validation
10. Preview
11. Save
12. Dataset generation
13. Attach / train models
14. Pilot
15. Results
16. 若要正式研究 → 新的 Research Plan / Freeze
```

PASS 定義：

> 一般使用者不用修改 Thesis Profile、不用直接修改 Python core、不用手動改 SQLite/YAML，即可完成 template-supported Sand development workflow。

---

# 26. 新增 Infrared Sensor 的完整使用者閉環

```text
1. Sensor Library
2. Add Sensor
3. Infrared
4. Existing Adapter?
   YES → Configure
   NO  → Plugin Required workflow
5. Observation schema
6. Units
7. Quality signals
8. Simulation / ingest source
9. Preview sample
10. Register sensor
11. Add to Development Profile
12. Compatibility validation
13. Attach IR perception model
14. Pilot multimodal run
15. Result visualization
```

PASS 定義：

> 加 IR 不需要修改 RGB / ToF core implementation；上層由 Evidence Contract 接收新 modality。

---

# 27. 新增另一顆 ToF 的完整使用者閉環

若 sensor family 相容：

```text
Clone ToF Sensor Adapter config
→ device/model
→ FOV/range/resolution
→ channel mapping
→ calibration / timing
→ quality signals
→ validation
→ new Sensor Instance
```

若 channels / physics / timing semantics 不相容：

> 建立新的 Sensor Adapter version。

不能只因名字同為 ToF 就假定可直接沿用 VL53L0X-like mapping。

---

# 28. Plugin Governance

新增 Plugin 必須有：

- plugin_id；
- version；
- author；
- capability；
- parameter schema；
- output schema；
- unit definitions；
- dependency；
- compatibility；
- tests；
- provenance；
- known limitations。

Plugin 進 Formal Profile 前必須：

1. schema validation；
2. deterministic/smoke tests；
3. measurement validity review；
4. compatibility review；
5. artifact generation test；
6. version freeze。

---

# 29. Error Handling

前端錯誤禁止只顯示：

```text
500 Internal Server Error
```

必須映射：

| Code | UI Message |
|---|---|
| PROFILE_FROZEN | 此設定檔已凍結，請 Clone 後修改 |
| SENSOR_PLUGIN_REQUIRED | 此感測器尚無可執行 Adapter |
| SCENARIO_PLUGIN_REQUIRED | 此情境超出現有模板能力 |
| SENSOR_SCENARIO_INCOMPATIBLE | 此感測器與情境尚未通過相容性驗證 |
| MODEL_REQUIRED | 此模態尚未掛載相容模型 |
| FORMAL_IDENTITY_MISMATCH | 正式執行身分與 Frozen Profile 不一致 |
| PROVIDER_QUOTA | 外部供應商配額不足 |
| RUN_INHIBITED | 本次執行因有效性條件失敗而中止，紀錄已保留 |

---

# 30. Security / Local-first

沿用既有：

- localhost-first；
- API keys 不進 repo；
- secret_ref；
- audit log；
- Formal runner 不讀 live binding；
- remote exposure 才啟用 authenticated admin / CSRF / TLS policy。

---

# 31. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-P01 | 建立/Clone/Profile version | MUST |
| FR-P02 | Frozen Profile immutable | MUST |
| FR-P03 | Thesis Profile golden regression | MUST |
| FR-P04 | Scenario Library | MUST |
| FR-P05 | Scenario Wizard | MUST |
| FR-P06 | Scenario template/plugin distinction | MUST |
| FR-P07 | Sensor Library | MUST |
| FR-P08 | Sensor Wizard | MUST |
| FR-P09 | Sensor Adapter contract | MUST |
| FR-P10 | Sensor×Scenario compatibility | MUST |
| FR-P11 | Profile Builder | MUST |
| FR-P12 | Basic/Advanced UI | SHOULD |
| FR-P13 | Simulation Preview | MUST |
| FR-P14 | Dataset inventory/split viewer | MUST |
| FR-P15 | Model registry/binding | MUST |
| FR-P16 | Fusion strategy registry | MUST |
| FR-P17 | LLM profile-aware binding | MUST |
| FR-P18 | Preview/Pilot/Formal separation | MUST |
| FR-P19 | Formal unique executor | MUST |
| FR-P20 | Web live formal monitoring | MUST |
| FR-P21 | Run event stream | MUST |
| FR-P22 | Run validity monitor | MUST |
| FR-P23 | Provider token/cost monitor | SHOULD |
| FR-P24 | Artifact/provenance browser | MUST |
| FR-P25 | CLI/Web/Service parity | MUST |
| FR-P26 | Formal abort record immutable | MUST |
| FR-P27 | Clone frozen before edit | MUST |
| FR-P28 | Plugin registry/version | MUST |
| FR-P29 | Compatibility validation | MUST |
| FR-P30 | No direct DB/YAML requirement for normal workflow | SHOULD |

---

# 32. Non-Functional Requirements

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-P01 | Scientific zero drift | Thesis Golden Regression PASS |
| NFR-P02 | Traceability | result → run → profile → plugin/config/code |
| NFR-P03 | Fail closed formal | missing identity refuses start |
| NFR-P04 | User friendliness | core UAT 不需手改 code/config DB |
| NFR-P05 | Extensibility | 新 sensor adapter 不改 unrelated sensor core |
| NFR-P06 | Accessibility | form labels、keyboard、clear state |
| NFR-P07 | Responsiveness | laptop / common desktop usable |
| NFR-P08 | Recoverability | development runs can resume safely |
| NFR-P09 | Formal immutability | frozen run cannot inherit live UI changes |
| NFR-P10 | Auditability | every edit/freeze/run logged |

---

# 33. Acceptance Matrix：功能閉環

## 33.1 Profile

| ID | 操作 | Expected | FAIL |
|---|---|---|---|
| ACC-PROF-01 | Web 建 Profile | versioned profile created | 要手改 YAML |
| ACC-PROF-02 | Clone Frozen | 新 Development Profile | 原 Frozen 被修改 |
| ACC-PROF-03 | Edit Frozen | refuse + Clone CTA | 直接可儲存 |
| ACC-PROF-04 | Freeze | immutable identity | freeze 後仍可改 scientific field |

## 33.2 Scenario

| ID | 操作 | Expected | FAIL |
|---|---|---|---|
| ACC-SCN-01 | 新增 Sand | Wizard 完成 definition | 手改 core enum |
| ACC-SCN-02 | template-supported preview | 可產 preview | 需改 Python |
| ACC-SCN-03 | unsupported physics | Plugin Required | 假裝成功 |
| ACC-SCN-04 | 加 Sand 至 Thesis Frozen | refuse | 原 profile 被擴成五類 |

## 33.3 Sensor

| ID | 操作 | Expected | FAIL |
|---|---|---|---|
| ACC-SEN-01 | 建另一顆 ToF instance | config + validation | 改 RGB core |
| ACC-SEN-02 | 加 IR with existing adapter | profile 可選 | 修改 fusion business logic |
| ACC-SEN-03 | 加未知 sensor | Plugin Required | generic fake output |
| ACC-SEN-04 | unit/schema mismatch | validation fail | silent continue |

## 33.4 Simulation

| ID | 操作 | Expected | FAIL |
|---|---|---|---|
| ACC-SIM-01 | Preview | visual output + provenance | 無 run record |
| ACC-SIM-02 | paired sensor preview | same scenario identity | 各 sensor 用不同 scene |
| ACC-SIM-03 | Formal generation | exact frozen params | UI live params 穿透 |

## 33.5 Formal

| ID | 操作 | Expected | FAIL |
|---|---|---|---|
| ACC-FML-01 | Start without frozen | refuse | 可跑 |
| ACC-FML-02 | wrong lineage | refuse | silent legacy fallback |
| ACC-FML-03 | start formal | unique production executor | Web 另跑一套 loop |
| ACC-FML-04 | running | realtime progress | 黑箱 |
| ACC-FML-05 | API failure | retry / inhibit per policy | drop case |
| ACC-FML-06 | abort | immutable aborted run | run 消失 |
| ACC-FML-07 | live edit during run | no effect/refuse | output identity 變化 |

---

# 34. User Acceptance Test（UAT）

## UAT-01 教授要求加入沙子

**User statement：**
「這個系統能不能再辨識管內沙子？」

PASS：

1. Clone current profile；
2. Add Scenario；
3. 選 Granular Material / Sand；
4. 填 basic parameters；
5. system 顯示 Sensor Compatibility；
6. preview；
7. save development profile；
8. 原 Thesis Profile hash / content 不變。

---

## UAT-02 教授要求加入紅外線

**User statement：**
「RGB、ToF 之外能不能再加紅外線？」

PASS：

1. Sensor Library → Add Infrared；
2. 若 adapter 已存在，表單配置；
3. 若不存在，明確 Plugin Required；
4. 新 sensor 只能加入 Development Profile；
5. existing RGB/ToF pipeline regression PASS。

---

## UAT-03 教授要求更換 ToF 感測器

PASS：

- 支援相容 sensor variant cloning；
- 顯示 channels / units / timing / calibration 差異；
- 不把「同為 ToF」當作自動等價；
- Thesis Profile 不受影響。

---

## UAT-04 教授要求臨時調參

**User statement：**
「把這個參數改大一點看看。」

Development Profile：

> 允許。

Frozen Profile：

> refuse → Clone。

---

## UAT-05 教授問正式實驗跑到哪

PASS：

網頁可直接看到：

- overall progress；
- current scenario；
- method；
- route；
- agents；
- request/token/cost；
- validity；
- artifacts。

---

## UAT-06 新增情境但沒有 perception model

PASS：

UI 明確顯示：

> Scenario ready / Sensor ready / Perception model missing

不能讓 workflow 靜默進入 formal。

---

# 35. Formal Functional Closure Definition

整個平台只有同時滿足下列才可宣稱：

`PLATFORM_FUNCTIONAL_CLOSURE = PASS`

1. Profile create / clone / freeze；
2. Scenario create / preview；
3. Sensor create / validate；
4. compatibility；
5. dataset；
6. perception binding；
7. pilot；
8. formal freeze；
9. formal start；
10. realtime monitor；
11. error / abort / inhibit；
12. statistics；
13. artifact/provenance；
14. thesis golden regression。

任何一步需要研究者秘密手改 Python / DB / YAML 才能完成「正常使用情境」：

> closure 不完整。

例外：

> 新物理模型的 Sensor/Scenario Plugin 開發本身屬軟體擴充工作，可以需要 code；但 Plugin 完成後，日常研究配置不得再依賴手改 core。

---

# 36. Code–Plan Alignment Gate

每次 platform release 必須回答：

1. UI 宣稱可做的功能是否真的進 production service？
2. service 是否真的呼叫 scientific core？
3. Formal UI 是否真的呼叫唯一 Formal executor？
4. Profile 中顯示的值是否與 runtime 讀取值一致？
5. Frozen Profile 是否真正 fail-closed？
6. Plugin registry 是否與實際 loaded code 一致？
7. artifact 是否能回到 Profile/config/code？
8. Web monitor 的數字是否由 run event / artifact 取得，而非 UI 自己估？

---

# 37. Implementation Phases

## Phase 0 — Containment / No-Drift

先做：

- Thesis Profile identity inventory；
- Golden Regression；
- formal production entry path；
- active lineage explicit；
- existing formal core 不變。

Exit：

`THESIS_ZERO_DRIFT_BASELINE = PASS`

---

## Phase 1 — Profile Layer

- Profile DB/schema；
- version；
- clone；
- freeze；
- current thesis mapping；
- UI profile list。

Exit：

Development / Frozen separation PASS。

---

## Phase 2 — Web Workbench UX

- Dashboard；
- Profile Builder；
- Basic / Advanced；
- Scenario Library；
- Sensor Library；
- Preview。

Exit：

UAT-01 / UAT-04 可走完。

---

## Phase 3 — Extensibility

- Scenario Plugin registry；
- Sensor Adapter registry；
- compatibility matrix；
- IR / alternate-ToF demo adapter；
- plugin validation。

Exit：

UAT-02 / UAT-03 PASS。

---

## Phase 4 — Experiment Orchestration

- preview/pilot/formal；
- unified ExperimentService；
- progress events；
- run registry；
- realtime monitor。

Exit：

formal UI 使用唯一 executor。

---

## Phase 5 — Provenance / Results / Closure

- artifact browser；
- statistics；
- validity；
- exports；
- acceptance suite；
- final golden regression。

Exit：

`PLATFORM_FUNCTIONAL_CLOSURE = PASS`

---

# 38. Change Budget / 不要過度重構

## 38.1 優先重用

重用：

- existing simulation modules；
- current ToF surrogate；
- current perception；
- reliability/gate；
- agents；
- LLM Registry；
- formal locks；
- statistics；
- artifact paths。

## 38.2 新增包裝層

新增：

- profile service；
- plugin registry；
- compatibility service；
- experiment service；
- web workbench；
- event/progress store。

## 38.3 禁止為 generic 而 generic

不要求：

- 把所有 scientific class 都改成抽象 factory；
- 重寫 stable modules；
- 引入大型 microservice；
- 引入複雜 Kubernetes；
- 換資料庫只為未來想像需求。

原則：

> 能以 adapter / registry / profile wrapper 解決，就不動 stable scientific core。

---

# 39. 建議目錄樹

> **v0.7.0 更新：** 標 **[已建]** 的是 CURRENT 實際存在的落點，
> 標 **[規劃]** 的尚未實作。v0.6.0 原樹把 `scenarios/`、`sensors/`、
> `compatibility/`、`artifacts/` 列為平行子套件，那部分仍是 Phase 3
> 之後的目標；而 `projects/`、`runs/`、`pipeline/`、`lifecycle/` 是
> 平台化過程中長出來、原樹沒有預期的。

```text
pcmef/
├─ core/                        [已建] active_lineage 等 stable core
│
├─ platform/
│  ├─ capabilities.py           [已建] 能力模型（§49.2）
│  ├─ executors.py              [已建] template → executor 登記處（§49.3）
│  ├─ projects/                 [已建] Project 抽象、clone、namespace
│  ├─ profiles/                 [已建] §4.1 狀態機、freeze、validation
│  ├─ pipeline/                 [已建] 流程定義（非七個硬寫節點）
│  ├─ runs/                     [已建] 歸屬、邊界、stage events（§49.1/§49.4）
│  ├─ lifecycle/                [已建] Phase 4 的 PC-MEF adapter
│  │
│  ├─ scenarios/                [規劃] registry / contracts / plugins
│  ├─ sensors/                  [規劃] registry / contracts / adapters
│  ├─ compatibility/            [規劃] Sensor × Scenario 矩陣
│  └─ artifacts/                [規劃] artifact service
│
├─ console/                     [已建] 五入口、run 分頁、SSE
├─ admin/                       [已建] Flask app 與 templates
├─ experiments/                 [已建] run_claim / final_gate / formal_service
├─ agents/ perception/ stats/   [已建] stable scientific core
└─ ...
```

此為建議邊界，**不要求為了符合目錄樹搬動既有 stable module**
（見 §38 Change Budget）。

---

# 40. Frontend Design Rules

1. 一頁一個主要任務。
2. 預設 Basic Mode。
3. Advanced settings 收合。
4. 使用 human-readable names。
5. 技術 ID 放 Details。
6. Frozen / Development 色彩與 icon 必須明顯不同。
7. 所有 destructive action 二次確認。
8. Formal Start 要 review summary。
9. 表單即時 validation。
10. 錯誤必須給修復方向。
11. 長時間任務可離開頁面後重新進來看。
12. 不以 hover 作唯一資訊入口。
13. 重要 parameter 顯示 unit。
14. 使用 preset 時顯示來源。
15. 不允許「Save」同時暗中 Freeze。

---

# 41. Freeze Review Screen

使用者按 Freeze 前顯示：

```text
Research Profile Freeze Review

Research scope
Scenarios
Sensors
Simulation
Models
Fusion
LLM
Stress
Statistics
Formal policy
Code/runtime
```

底部：

```text
I understand this creates an immutable research identity.
[ Freeze Profile ]
```

Freeze 後：

`Edit` 變成 `Clone & Edit`。

---

# 42. Artifact Passport

每個 run 主要 artifact：

| Field | Required |
|---|---|
| Artifact ID | YES |
| Profile ID/version/hash | YES |
| Run ID | YES |
| Scenario/Sensor IDs | YES |
| Input scope | YES |
| Code/version | YES |
| Plugin versions | YES |
| Config | YES |
| Output locator | YES |
| Integrity status | YES |
| Known limitation | YES |

---

# 43. Result Workspace

提供：

- Summary；
- per-condition；
- per-class；
- confusion matrix；
- routing；
- reliability；
- LLM escalation；
- cost；
- validity；
- negative cases；
- export。

對 `s_A` / PC-MEF final score：

> 不標示為 calibrated posterior probability。

---

# 44. Research Boundary

平台能支援新的研究設定，不代表：

- 新 sensor 已校準；
- 新 Scenario 已具真實性；
- 新 modality 一定改善；
- plugin output 就是 scientific evidence；
- development result 可以直接寫成 formal conclusion。

每個新 Profile 若要成為新論文 evidence，仍需自己的：

> Research Question → Claim → Experiment → Evidence → Formal Gate。

---

# 45. Release Acceptance Gates

## Gate A — No Drift
Thesis Profile Golden Regression PASS。

## Gate B — Profile
Create / Clone / Freeze / immutable PASS。

## Gate C — Scenario
Sand UAT PASS。

## Gate D — Sensor
alternate-ToF + IR extension workflow PASS。

## Gate E — Experiment
Preview/Pilot/Formal separation PASS。

## Gate F — Formal
single production executor + fail-closed + monitor PASS。

## Gate G — Provenance
result → profile → code/plugin/config trace PASS。

全部通過：

```text
PCMEF_PLATFORM_V060_ACCEPTANCE = PASS
PLATFORM_FUNCTIONAL_CLOSURE = PASS
```

---

# 46. Recommended First Implementation Slice

為降低對目前 Thesis Final E2 的干擾，第一個實作 slice 只做：

1. 建立 Profile schema；
2. 將現有 Thesis config 映射成 Protected Thesis Profile；
3. 建立 Golden Regression；
4. 統一 Formal production entry；
5. Web 顯示 Profile / Formal identity / live monitor；
6. Frozen Profile refuse edit；
7. Clone to Development；
8. Scenario/Sensor Library 先做 registry + UI skeleton；
9. 不立即重寫 scientific core。

完成後再做 Sand / IR demo。

---

# 47. Professor Demo Script

教授查看系統時建議 3 分鐘：

### Demo 1 — 目前碩論
開 Thesis Profile：

> 四類、RGB/ToF、Frozen、formal readiness、執行進度。

### Demo 2 — 未來擴充
Clone：

> `Sand Extension`

Add Scenario：

> Granular Material → Sand。

### Demo 3 — 新感測器
Sensor Library：

> Add Infrared。

系統顯示：

> Adapter available / Plugin Required / Compatibility。

### Demo 4 — 科學完整性
回 Thesis Profile：

> hash / configuration unchanged。

這能清楚展示：

> 系統可以擴充，但不會為了展示彈性污染目前論文。

---

# 48. Definition of Done

PC-MEF 平台 v0.6.0 只有在以下均成立才算完成：

- 使用者可從 Web 建 Profile；
- 可 Clone Frozen Profile；
- 可透過 Wizard 新增 template-supported Scenario；
- 可透過 Wizard 建立/掛載 supported Sensor；
- unsupported physics 清楚轉 Plugin Required；
- 可 Preview；
- 可建立 dataset；
- 可掛 perception；
- 可跑 Pilot；
- 可 Freeze；
- 可啟動 Formal；
- Formal 使用唯一 production executor；
- Web 可視化 + 啟動 + 即時監控；
- Formal live parameters 無法穿透 frozen identity；
- abort / provider failure 有完整 lineage；
- statistics/artifact 可回查；
- Thesis Profile Golden Regression PASS；
- 加 Sand / IR 不改變 Thesis Profile。

---

# Appendix A — Thesis Profile Platformization Firewall

```text
PLATFORMIZATION RULES

1. Existing Thesis scientific locks are source of truth.
2. Profile mapping must preserve, not reinterpret, frozen values.
3. New Scenario/Sensor defaults never enter Thesis Profile automatically.
4. Dynamic class lists cannot reorder Thesis CLASS_ORDER.
5. Live registry cannot override frozen LLM runtime.
6. Web UI cannot be a second formal executor.
7. Plugin version changes invalidate only profiles that reference them.
8. Unused plugins/sensors/scenarios do not alter frozen profile hashes.
9. Any change affecting Thesis outputs requires Golden Regression.
10. Final sealed data remain sealed until existing Formal Entry Gate passes.
```

---

# Appendix B — Minimal Scenario Definition Schema

```yaml
scenario_id:
version:
display_name:
canonical_name:
category:
description:

label_role:
  classification_label:
  environment_state:

physics:
  template_id:
  parameters:
  units:

supported_sensor_types:

visualization:
  preview_capability:

plugin:
  plugin_id:
  version:

validation:
  schema_status:
  physics_status:
  compatibility_status:

provenance:
```

---

# Appendix C — Minimal Sensor Definition Schema

```yaml
sensor_instance_id:
adapter_id:
adapter_version:

display_name:
sensor_family:
device_model:

parameters:
units:

capabilities:
  simulate:
  ingest:
  visualize:
  quality_signals:
  perception:

observation_schema:
summary_schema:
quality_schema:

compatible_scenarios:

validation:
provenance:
```

---

# Appendix D — Run Event Schema

```json
{
  "run_id": "...",
  "timestamp": "...",
  "profile_id": "...",
  "profile_version": "...",
  "phase": "FORMAL_RUNNING",
  "case_id": "...",
  "method": "FULL_PCMEF",
  "event_type": "AGENT_COMPLETED",
  "role": "physics",
  "status": "PASS",
  "latency_ms": 12031,
  "request_count": 1,
  "token_usage": {},
  "artifact_id": "...",
  "validity_flags": []
}
```

Web monitor 只讀 run event / registry，不自行推測 formal state。

---

# Appendix E — 核心驗收清單

```text
[ ] Thesis Profile mapped from current frozen sources
[ ] Golden Regression baseline stored
[ ] Development/Frozen state separation
[ ] Clone workflow
[ ] Scenario Wizard
[ ] Sensor Wizard
[ ] Compatibility Matrix
[ ] Preview Simulation
[ ] Dataset viewer
[ ] Model binding
[ ] Fusion/LLM profile-aware config
[ ] Unified Experiment Service
[ ] Unique Formal Production Executor
[ ] Realtime Formal Monitor
[ ] Provider/token/cost monitor
[ ] Validity monitor
[ ] Abort/Inhibit lineage
[ ] Artifact Passport
[ ] Sand UAT
[ ] Infrared UAT
[ ] Alternate-ToF UAT
[ ] Final Golden Regression
```

---

# Appendix F — 決策摘要

**DEC-P01：** PC-MEF 由 thesis-specific system 升級為 profile-driven research platform。  
**DEC-P02：** 現有 Thesis Profile 為 protected immutable scientific identity。  
**DEC-P03：** 開發彈性透過 Clone + Development Profile 提供，不解鎖原 Frozen Profile。  
**DEC-P04：** Scenario 採 template + plugin 雙層設計。  
**DEC-P05：** Sensor 採 Adapter contract，不假裝任意 sensor 都能 no-code 科學建模。  
**DEC-P06：** Web 提供配置、可視化、啟動、監控，但 Formal execution 只有一條 production path。  
**DEC-P07：** 平台化完成的必要條件包含完整功能閉環與 Thesis Golden Regression。  
**DEC-P08：** 新增 Sand、Infrared、alternate-ToF 都必須在新的 Development Profile 中進行。  
**DEC-P09：** 平台能力與科學有效性分離；能執行不等於能形成正式 research claim。  
**DEC-P10：** 優先包裝既有 scientific core，不做與 Thesis E2 無關的大型重寫。

---

# 49. Execution / Action Layer

v0.6.0 定義了「Formal 只有一條 production path」（§18），但沒有定義
**一次執行在平台上是什麼**。2026-09-06 ~ 09-25 之間的七輪收斂
（commit `16d6e51`..`eb8ad9c`）補上了這一層。本節是那七輪的規格化結果。

貫穿本節的一條原則：

> **畫面、事件檔與 run.json 是三份會同時存在於磁碟上的紀錄。
> 它們不一致時，不一致本身就是缺陷 —— 即使每一份單獨看都合理。**

## 49.1 Run Attribution（一次執行屬於誰）

每一次 run 在建立時寫入 `run_identity.json`，記錄它屬於哪一個
Project 與哪一個 Profile。

- **write-once**：以 `O_EXCL` 建立，不得覆寫。歸屬可以缺，不可以改。
- **fail-closed**：讀不到歸屬的 run 不得回退成「屬於目前選取的 Project」。
  回退等於「刪掉歸屬檔就變成碩論的」，那是最不該放寬的時候放寬。

### Attribution Boundary

歸屬機制不是一開始就有的，因此需要一條分界線：

- `.attribution_boundary.json` 在 run root 上**安裝一次，之後只讀**。
- 邊界**之前**建立的 run 屬於 legacy layout，照常顯示。
- 邊界**之後**建立的 run 必須自帶 `run_identity.json`；缺了就是
  **orphaned，不是 legacy**。
- 讀不到或寫不進去時一律 `AttributionBoundaryError`，
  **不得改用「現在時間」補一個**。以當下補出來的邊界會把所有既有 run
  一次推到邊界之前，於是那條繞道又打開了。
- 時間戳**完整精度不截到秒**：截掉小數就是把邊界往前挪最多一秒，
  而那一秒內建立的 run 會被誤判成孤兒。

> **撰寫測試時注意：** 邊界是在測試執行當下安裝的。把日期寫死成
> 某個「未來」的字面值，過了那天就會變成過去，測試會從那天起每天
> 失敗，而失敗原因與它要守的規則無關。日期一律由邊界推算。

## 49.2 能力模型（Capability Model）

`platform/capabilities.py` 回答「這個 Project **可不可以**啟動某件事」：

| 能力 | 意義 |
|---|---|
| `RUN_SIMULATION` | 可跑模擬 |
| `LLM_SNAPSHOT_READ` | 可讀 LLM snapshot |
| `LLM_RUNTIME_FREEZE` | 可凍結 LLM runtime |
| `FORMAL_E2` | 可執行 Formal E2 |
| `SCIENTIFIC_STATE_READ` | 可讀科學狀態 |

另有 `MINIMUM_STATE`：依 profile 狀態再閘一層。能力由 template 宣告，
**不得以 project id 判斷** —— 用 id 判斷與用名字判斷只差一層。

## 49.3 Executor Registry（這個 Project 有什麼可以啟動）

能力回答「可不可以」，registry 回答「有什麼」。兩個問題都要有答案，
缺一個就會出現「空專案按下開始，跑出一份標著它自己名字、內容卻是
碩論場景的結果」。

`platform/executors.py` 現況（template `pcmef-thesis`）：

| executor | scope | 事件歸戶 | 涵蓋節點 |
|---|---|---|---|
| `sim_smoke` | stage | `simulation` | simulation |
| `surrogate_smoke` | stage | `perception` | perception |
| `formal_e2` | **pipeline** | `formal_e2` | perception, reliability, routing, arbitration, decision |
| `llm_snapshot` | action | `llm_snapshot` | （不是流程的一步） |
| `audit_gates` | action | `audit` | （不是流程的一步） |

三條約束：

1. **不得把 `scope` 與 `stage_ids` 收回成單一 `stage_id`。**
   Formal E2 涵蓋整條推論鏈；記成一個節點，紀錄就會宣稱它只做了最後一步。
2. **涵蓋多步的執行用自己的名字歸戶事件**（`formal_e2`），
   不掛在任何一個研究節點下。掛在 `decision` 上的話，畫面會說
   「決策這一步完成了」，而實際發生的是整條鏈跑完、其餘六個節點
   看起來沒動過。
3. **查詢前必須 `_ensure_registered()`。** 登記是「匯入即註冊」，
   少了它，在還沒有人匯入 provider 的行程裡這張表是空的 —— 而空表的
   意思是「沒有任何 executor」，於是碩論自己被拒絕執行。
   授權結果取決於匯入順序，是這一層最難重現的一種錯。

流程本身是**定義**而非七個硬寫節點；`pcmef-thesis` 的七節點為
`simulation → paired → perception → reliability → routing →
arbitration → decision`（`arbitration` 為 optional）。

## 49.4 Stage Events 與終局唯一

`platform/runs/events.py`：append-only JSONL。

- **fail-soft writer**：寫事件失敗不得拖垮執行本體，但必須留下
  `dropped` 與 `last_error` —— 靜默地少掉幾筆事件，比沒有事件更糟。
- **終局唯一（terminal-once）**：第一個終局事件勝出，之後的被丟棄。
  這條規則本身正確，但它會與下一節的問題交互作用。

### 成功事件的位置

終局唯一代表：**成功事件一旦發出，後面再發失敗也沒用**。因此

> `stage_completed()` 與真正的 `return 0` 之間**不得有任何語句**。

中間夾一行、那一行炸掉，事件檔會留下 `completed`（後到的 `failed`
被丟），而 CLI 以非零結束、`run.json` 說 `failed` —— 兩份紀錄各說
各話，而它們都在磁碟上。此規則由結構性測試強制
（walk AST，`stage_completed` 之後必須緊接 `ast.Return`）。

### 但：已完成的科學結果不得被呈現層改判

`run_formal_e2_full()` 回來的那一刻，report 已寫在磁碟上、一次性
claim 已標成 `COMPLETE`。之後的摘要排版**純粹是講給人看**。

- 排版失敗（缺鍵、格式化錯誤、`| head` 關掉管線造成的
  `BrokenPipeError`）**一律 fail-soft**：在 stderr 警告，
  科學結論、claim 狀態、終局事件與 exit code 全部維持成功。
- 終局事件取值一律 `.get()`，讓它與 `return` 之間連取值都不會炸。

**兩條規則不衝突**：成功事件仍然緊貼 return，只是它前面那一段
不再有機會拋出例外。把「排版壞掉」記成「實驗失敗」的代價是
——claim 已經 `COMPLETE`，重跑會被擋住。

### 科學完成點之後，連中斷都不改判（round 9）

上一條的 fail-soft 只接 `Exception`。`KeyboardInterrupt` 與
`SystemExit` 不是 `Exception`：摘要印到一半按下 Ctrl-C，會穿到入口的
catch-all，寫下 `stage_failed`、以非零結束，console 依 exit code 把
`run.json` 記成 `failed` —— 而 report 與 claim 都已經完成。

`cli._ScienceIsComplete` 標記**科學完成點**（`run_formal_e2_full()`
成功回來的那一刻）：

| 時點 | 例外 / 中斷 | 結果 |
|---|---|---|
| 越界**之前**（科學還在跑） | 任何例外，含 KeyboardInterrupt / SystemExit | **照常往外拋**、`stage_failed`；executor 把 claim 標成可 resume |
| 越界**之後** | 任何例外，含 KeyboardInterrupt / SystemExit | stderr 警告；終局事件 `stage_completed`（終局唯一，至多一筆）；exit 0；`run.json` = `succeeded` |
| 越界之後的 SIGINT（主執行緒） | 改成**只記錄、不拋出** | 同上，並註記「越界後收到中斷」 |

- 入口 catch-all 以 `boundary.crossed` 分流：越界之後一律交給
  `_settle_after_the_boundary()` 補齊終局事件並回 0，**不得 raise**。
- 由命令列啟動（`main()` 從 `sys.argv` 取參數）時，SIGINT 的延後
  維持到行程結束；被當成函式呼叫（測試）時離開入口就還原。
- 端到端驗證：真的子行程、真的 SIGINT、console 依 exit code 寫下的
  `run.json`（`test_the_persisted_run_record_follows_the_scientific_boundary`）。

**已知剩餘窗口（不修）：**
1. executor 回來到 `cross()` 立旗之間的直譯器檢查點（兩個）。要關掉
   它只能在越界之前就遮蔽 SIGINT，那會改變科學執行中的中斷語意。
2. executor **內部**在 `mark_complete` 之後還會呼叫一次 progress
   callback（`claim ... COMPLETE`）。依定義它在「回來之前」，因此那
   一行若拋出，照常記成失敗 —— 而 claim 已是 `COMPLETE`。要收掉它得
   改 `e2_formal.py` 或讓越界前的 progress 輸出 fail-soft，兩者都改變
   越界之前的語意，本輪不做。
3. `SIGTERM` / `TerminateProcess` / `CTRL_BREAK` 直接終止行程，Python
   接不到；越界之後被這樣殺掉，`run.json` 依 exit code 仍是 `failed`。

## 49.5 UNREAPED：收不掉的子行程

執行結束時若無法確認子行程已終止：

- run 狀態為 `UNREAPED`，並在同目錄寫下 `unreaped_process.json`
  （pid / command / host / started_at）。
- 這**不是一般的失敗**。一般失敗代表「跑完了，結果不好」；
  `UNREAPED` 代表「有一個行程還在，而沒有人在讀它」。
- `run.json` 與標記檔由不同路徑寫出（前者 `Path.write_text`，
  後者 `os.open`），**任一邊都可能單獨成功**。因此當紀錄仍寫著
  `running` 而標記檔已經在了，**以標記檔為準**：
  `get()` 與 `list_runs()` 在**讀取時**投影成 `UNREAPED`。
- **只投影不回寫。** `run.json` 寫不成功正是走到這裡的原因之一，
  再寫一次只會再失敗，而且會蓋掉「當時到底發生什麼」。

## 49.6 Run record 的持久化（round 10）

`run.json` 是 Run、Results 與 Formal workspace 三個全域頁面共同的來源。
round 9 實測到它的寫法本身就是缺陷：`Path.write_text()` **先截斷再寫**。
同時讀取會讀到半份（tmp 實測 3000 次讀取有 1487 次 `JSONDecodeError`），
截斷之後寫入失敗（磁碟滿）會留下 0 byte 的紀錄，而 `list_runs()` 沒有
逐筆容錯 —— 三個全域頁面從此**永久 500**。

**不變量：**

1. **成功的狀態轉換以原子替換落盤**（`runner.write_atomically`）：同目錄
   以 `O_EXCL` 建暫存檔 → 寫完、`fsync` → `os.replace()`（POSIX rename /
   Windows MoveFileEx，同 volume 內原子）→ POSIX 上再 `fsync` 目錄。
   讀者只會看到**上一份完整紀錄或新的完整紀錄**。
2. **替換之前任何一步失敗，上一份完整紀錄一個 byte 都不動**，暫存檔刪除，
   例外往外拋；背景的收尾路徑（`_save_or_leave_a_trace`）改寫
   `run_crash.txt`，記下**想寫成什麼**、`record_write_error` 與
   `run_json=previous complete run.json left untouched`。
3. **初始紀錄寫不進去**：不啟動任何行程、不留半份 `run.json`，留下
   `run_crash.txt`（`run_json=no run.json was written`），並以
   `RunLaunchError(process_reaped=True)` 拒絕。經由 `console.launch`
   啟動時，round 1 的「沒起跑的 run 什麼都不留」照舊成立，目錄連同
   `run_crash.txt` 一起收回 —— 證據是 409 拒絕訊息本身（磁碟滿時連
   `run_crash.txt` 都未必寫得進去，只有回應一定送得出去）。
4. **Windows 的讀寫互斥是等待，不是損壞**：讀者開著舊檔時（Python 開檔
   不帶 `FILE_SHARE_DELETE`）替換會被拒絕，寫入端有上限地重試（40 次、
   總計約 3.7 秒，逾時照樣拋）；讀取端遇到替換中的短暫 sharing violation
   也有上限地重試，不把它報成損壞。

**已經壞掉的歷史紀錄只影響它自己：**

| 位置 | 行為 |
|---|---|
| `get()` | 丟 `RunRecordDamaged`（`RunnerError` 子類別）：說出哪一筆、壞在哪裡、大小 |
| `list_runs()` | 跳過它，**不補出一筆看起來正常的 RunRecord** |
| `damaged_runs()` | 另外回報：編號、原因、大小、有沒有 `run_crash.txt`、`formal_output.json` 說的模式 |
| `/console`、`/results`、`/formal` | 200；另列「讀不出來的執行紀錄」，不給任何動作 |
| Run 頁、case 頁、status、stream、figures、delete | 擁有者 409 附診斷；非擁有者 404（與正常紀錄同一條隔離規則） |
| 刪除 | **一律拒絕**，不論 `formal_output.json` 說它是正式、預演或不知道 |

- **「讀不出來」的判準只看結構，不補值**（`parse_record`）：0 byte、
  非 UTF-8、JSON 斷掉、不是物件、缺 `run_id` / `kind`、`run_id` 與目錄
  不符、欄位型別不對（`command` 是字串、`params` 是清單、`exit_code` 是
  bool…）。寬鬆的 `from_json()` 會把字串拆成字元、把成對清單當成 dict
  —— 在壞紀錄上那就是替它編內容，而那份內容會決定它算不算正式執行。
- **正式與否無法確定時當成「可能是正式執行」**：`formal_output.json`
  只有 formal_e2 會寫、與 `run.json` 分開寫，是唯一可能還活著的旁證；
  它不在不代表是預演。Formal workspace 因此列出所有「無法證明不是正式
  執行」的壞紀錄 —— 正式執行紀錄那張表只列得出讀得到的，一筆壞掉的
  正式紀錄若只是從表上消失，這一頁就少說一次「確實跑過」。
- **沒有歸屬檔的壞紀錄誰都不擁有**（P1-4），但也不得從畫面上消失：
  只列編號與原因，不給連結、不給動作；打開它一律 404。
- 更新失敗而保留下來的上一份紀錄可能還寫著「執行中」：Run 頁在同目錄
  有 `run_crash.txt` 時顯示「這筆紀錄有一次更新沒有寫成功」，並附上它的
  內容。**不回寫、不投影成別的狀態**。

---

# 50. 本機執行環境與測試隔離

本節的每一條都是被真的咬過之後才寫下來的。

## 50.1 測試不得污染真實科研樹

三層互補，失敗模式各不相同，**三層都要**：

| 層 | 回答的問題 | 抓得到什麼 |
|---|---|---|
| 指紋比對（session 前後） | 跑完之後有沒有變？ | 經由 C 擴充等稽核事件涵蓋不到的路徑寫入 |
| `WriteTripwire`（稽核事件） | 過程中有沒有碰過？ | **寫了又刪**的那一種（指紋前後相同） |
| `ListenerTripwire`（見 §50.3） | 有沒有佔住 port？ | 真實 TCP 綁定 |

`PROTECTED_PATHS` = `outputs/perception/e2_final`、`freeze/`、`projects/`。

**WriteTripwire 的四道關卡**（`tests/conftest.py`）：

1. **名字粗篩**：路徑字面含受保護根目錄名稱 → 完整 `resolve()` 判定。
   比對的是**最後一段名字**而非絕對路徑前綴 —— 用前綴的話，相對路徑
   （最常見的寫法）一個字都對不上，在 `resolve()` 之前就被放行。
2. **所在目錄**：解析父目錄（有快取）再判一次。
   快取的**鍵必須先 `abspath()`** —— `"."` 在 `chdir()` 之後指的是
   別的地方，沿用舊答案就是一條走得通的繞道。
3. **最後一段是別名**：父層乾淨不代表落點乾淨。
   `safe/alias.json -> freeze/.../lock.json` 名字上看不出任何東西，
   父層 `safe/` 也乾乾淨淨，而 `open()` 會沿著連結寫進去。
   判定用 **reparse point**，**不得只用 `os.path.islink()`**
   —— 它對 Windows junction 回傳 `False`，而 `resolve()` 照樣跟著走。

4. **最後一段是另一個名字的同一個檔案（hardlink）**（round 9）：
   hardlink 沒有 target、沒有 reparse point，路徑解析從哪一邊出發都
   只回到它自己。判準改成身分 `(st_dev, st_ino)`，比對受保護檔案的
   身分索引。`os.link` 在 session 內本來就會被擋；這一關補的是
   **session 開始前就已存在**的 hardlink（實測：Windows 與 Linux 上
   `open(alias, "w")` 都直接改掉受保護內容）。

代價控制：第 3、4 關共用一次**不跟著連結走**的 `lstat()`；絕大多數寫入
的目標還不存在，當場 ENOENT 返回，代價就停在那裡。已存在而
`st_nlink == 1` 的檔案不可能是別人的別名，不查索引；身分索引第一次
真的需要時才建（受保護樹目前 1613 檔），多數 session 從頭到尾不會建。

**寫入落點相對於目錄描述子（openat / `dir_fd`，round 9）：**

POSIX 上 `os.open(name, flags, dir_fd=fd)` 的 `name` 相對於 fd 指向的
目錄，不是 cwd。CPython 的 `open` 稽核事件只有 `(path, mode, flags)`，
**不帶 dir_fd**；其餘事件（`os.remove` / `os.rmdir` / `os.mkdir` /
`os.rename` / `os.link` / `os.symlink`）帶著 dir_fd，但守衛先前忽略它。
Linux 實測，round 8 的守衛下九種 dir_fd 相關寫法有八種直接改到受保護
內容，第九種（`shutil.rmtree`）回報「擋下」時底下的檔案已經刪光。

| 路徑 | 處置 |
|---|---|
| 帶 dir_fd 的稽核事件 | 以 `/proc/self/fd/N`（Linux）或 `F_GETPATH`（macOS）取得描述子位置，接上相對名字後再走四道關卡 |
| `os.open(..., dir_fd=)` | `open` 事件看不到 dir_fd，改由 `os.open` 的窄包裝在**有 dir_fd 且為寫入**時判定；其餘寫入仍由稽核事件負責 |
| `shutil.rmtree` | 在 `shutil.rmtree` 事件上、**刪第一個檔案之前**判定；刪受保護根目錄的祖先也算 |
| `O_RDONLY \| O_TRUNC` | 算寫入（Linux 上它會把檔案截空） |
| 看不出描述子指向哪裡 | **fail closed**，不猜 |

**平台涵蓋：**

| | Windows | Linux | macOS / 其他 POSIX |
|---|---|---|---|
| dir_fd 家族 | 不可達（`os.supports_dir_fd` 為空；目錄也開不成 fd） | 實測涵蓋 | `F_GETPATH` 有實作、**未實測**；兩者皆無的平台 fail closed |
| hardlink | 實測涵蓋 | 實測涵蓋 | 同 Linux 路徑，未實測 |
| `rmtree` | 原本就逐一以完整路徑刪（第一個檔案即被擋）；現在更早 | fd-based，現在於事件上擋下 | 同 Linux |

**仍然看不到的（由指紋比對兜底）：** 在 `_guard_dir_fd_opens` 裝上之前
就把 `os.open` 存成別名的程式碼；`os.mkfifo` / `os.mknod`（沒有稽核
事件）；經由 C 擴充直接寫檔。`chmod` / `utime` 屬於 metadata，依
「內容相同而 mtime 變了不算污染」的既有判準，不當成寫入。

## 50.2 守衛自己也會失效（本平台最重要的一課）

把一個函式拆成入口與本體之後，**三條檢查原始碼的守衛開始看著一個
只剩 try/except 的殼**。其中 presence 檢查會當場失敗（還好），
**absence 檢查則變成永遠通過 —— 它不報錯，只是不再守任何東西。**

因此：

- 任何取 `cmd_*` 原始碼的守衛，都必須把**拆出去的本體一起取**。
- 這條規則本身要有一條測試去掃描強制，否則下一次拆函式會再發生一次。
- 更一般地：**「通過但什麼都沒守」是這一層的主要失敗模式**，
  比「失敗」難發現得多。

## 50.3 Port 衛生

> **`http://localhost:8790` 是唯一的正式 localhost UI port。**

- 主機端只綁 loopback（`127.0.0.1:8790 -> 8787/tcp`），容器內側固定
  8787。內側用哪個 port 對操作者不可見。
- **不得靠換 port（8791、8792…）規避衝突。** 衝突要解決，不是繞過。
- 測試**一律走 Flask `test_client`**：它直接講 WSGI，一個 socket
  都不碰。

**執行期守衛（`ListenerTripwire`）**，掛在 `socket.bind` 稽核事件：

- TCP 綁定預設一律拒絕；真的需要時以
  `LISTENERS.allowing(port)` **明確宣告**，離開時若沒收掉當場失敗。
- `allowing(8790)` 在**宣告當下**就拒絕。

這一條先前是用正規式**掃描原始碼**守的，而掃描原始碼守不住任何東西：
`getattr(sock, "bind")(...)`、包一層 helper、從 library 裡繞出去 ——
三種寫法都掃不到，三種都會真的佔住一個 port。

**判準是擁有權，不是 port 號碼（round 9）。** 先前離開 `allowing()`
時拿 port 號碼去對全機 listening ports：`allowing(0)` 因此驗不到
（OS 挑的號碼要綁完才知道），而 session 層的全機比對會把任何無關程式
在這段期間開的 port 算到測試頭上。現在：

| 層 | 看什麼 | 需要 |
|---|---|---|
| `allowing()` 離開時 | 範圍內綁定的**每一個 socket 物件**是否已 close（port 0 同樣適用）；沒收的由守衛代為 close 後失敗 | 無 |
| `allowing()` 離開時 | pytest **行程樹**在範圍內新開、仍在 LISTEN 的（子行程開的 server、dup 出來的 fd） | psutil |
| session 結束 | pytest 行程樹自己的 listener 前後比對 | psutil |
| 每個測試結束 | `ChildProcessLedger`：這個測試 spawn 過（稽核事件記一筆）才列行程樹；仍活著的子行程歸到**那一個測試**的 teardown 失敗 | psutil |

- 子行程只回報、**不代為終止**，也不需要管理員權限：列的是自己的子孫。
- 本機實測（Windows、無管理員）：`psutil` 與 `netstat -ano` 列出的
  LISTEN port 完全一致（44 / 44），8790 由 `com.docker.backend` 持有；
  先前「psutil 少於 netstat」的觀察本輪未能重現。判準已不依賴全機清單。

> **已知涵蓋範圍：** 子行程在測試結束前就把孫行程分離出去、自己先結束
> 的話，孫行程不再掛在 pytest 底下，行程樹看不到它。沒有 psutil 的
> 環境（例如目前的 `pcmef-research:local` 映像）只剩 socket 物件那一層。

---

# 51. 目前實作狀態與已知缺口

本節是 v0.7.2 的**現況快照**，會隨實作前進而過期；
§0.4 的裁決規則在它過期之後仍然適用。

## 51.1 Final E2 Gate：1 / 8

`pcmef formal preflight --mode formal`（唯讀，exit code 2）：
解析 `freeze/ACTIVE_LINEAGE.json` → `freeze/runs/PFC-001`（10 locks），
判定由 `pcmef/experiments/final_gate.py` 執行。

| # | 項目 | 狀態 |
|---|---|---|
| 1 | `amd007_real_provider_validation` | **PASS**（`READY_FOR_FINAL_E2=YES`, real_calls=8） |
| 2 | `amd007_frozen` | FAIL — `AMD-007.amendment.json` 不存在 |
| 3 | `amd008_frozen` | FAIL — 同上 |
| 4 | `amd009_frozen` | FAIL — 同上 |
| 5 | `llm_runtime_refrozen` | FAIL — `real_agent_validation.json` 未記錄 `runtime_config_hash` |
| 6 | `formal_config_matches_current_llm_runtime` | FAIL — 交叉引用一致，但引用的 runtime 尚未通過驗證 |
| 7 | `canonical_golden_baseline` | FAIL — `canonical=false`，4 條 blocker 未解除 |
| 8 | `final_partition_manifest` | FAIL — `dataset_manifest.json` 未生成 |

> 第 6 項原名會在第 5 項未過時**假 PASS**，導致 Status 顯示「已重凍」
> 而其實兩者都沒有。現已改為依賴第 5 項（原 delta D-07）。

## 51.2 遷移安全基準

| 項目 | 值 |
|---|---|
| 涵蓋範圍 | `freeze/` `configs/` `registry/` `regression/` `provenance/` `schemas/` |
| 磁碟檔案數 | **95**（freeze 71 / configs 17 / schemas 3 / provenance 2 / registry 1 / regression 1） |
| **進版控** | **35** |
| 未進版控 | 60（freeze 57 / provenance 2 / registry 1） |
| tracked-only hash | `0e8fb28e3cbf5f15746db837cb5bc1923f91f1e97ae65ce3832fa23db2d10956` |
| ACTIVE_LINEAGE | `freeze/runs/PFC-001`（status ACTIVE，supersedes `freeze`） |

> **重要且容易誤解：** `freeze/*.lock.json` 共 **45 個 lock 不進版控**，
> 這是 `.gitignore` 的**明文設計決定**：「lock 記錄這次 run 用了什麼
> 參數，可以重產；權威一律是 `freeze/` 下的檔案本身，版控留的是產生
> 它的程式與決策記錄。」進版控的是 amendments / errata /
> preregistrations / ACTIVE_LINEAGE / corrective_lineage。
>
> 連帶結論：**涵蓋全部 95 檔的 manifest hash 不是穩定不變量**，
> 它會隨本機活動變動，也無法從一份乾淨的 clone 重現。要當作比對基準
> 請用上表的 tracked-only hash，或在同一台機器上前後比對。

此清單刻意不列舉 families 36–43。其 `dataset_manifest.json` 尚未生成，
本版亦未讀取該分割。

## 51.3 測試現況

| 範圍 | 結果（round 10，程式碼 `a08fb53`，Windows、py 3.10.11） |
|---|---|
| `tests/`（全量） | 3898 collected — 3847 passed、**13 failed**、38 skipped；13 項與下列歷史清單逐項相同，0 新增 |
| `tests/console/` + `tests/platform/` + `tests/e2/` | 1100 collected；在上面那一輪全量中 **0 failed** |
| `test_run_record_persistence.py`（round 10 新增） | 47 passed |
| Linux 容器（`pcmef-research:local`，repo 唯讀掛載） | 12 個 execution-layer 模組 336 passed、7 skipped、0 failed |

3898 = 3846（round 9）+ 47（`test_run_record_persistence.py`）+ 5
（`test_repo_integrity.py` 對每一個原始碼檔各跑 5 項檔頭檢查，新檔案因此多 5 項）。
round 9 記錄的兩次 `run.json` 撕裂讀取（`JSONDecodeError`）是 §49.6 修正前
的偶發失敗；本輪兩次全量都沒有出現。

- 13 個 failure 是**歷史既有**、與平台化無關的科學程式碼項目：
  `test_paired` ×2、`test_calibration_objective` ×5、
  `test_calibration_plan` ×1、`test_calibration_prereg` ×4、
  `test_repo_integrity` ×1。**不得為了讓它們變綠而改科學程式碼。**
- 38 個 skip：13 個是 POSIX 專屬的 dir_fd 測試（Windows 沒有 dir_fd，
  另有一條測試斷言這個前提）；6 個是 symlink 建立權限（Windows 未開
  Developer Mode，§50.1 第 3 關因此在本機以 junction 代跑，並於 Linux
  容器以真 file symlink 端到端驗過）；19 個是模擬堆疊（mitsuba/drjit 的
  LLVM backend 不在這台機器上）。
- Linux 容器沒有 psutil，行程擁有權那 5 條測試在那裡 skip；該層只在
  Windows 實測。

## 51.4 已知缺口（依優先序）

1. **Final E2 尚有 7 項 blocker**（§51.1），其中 AMD-007/008/009 尚未
   凍結、Golden Baseline 尚未 canonical。
2. **Run record 持久化的剩餘邊界**（§49.6；round 9 發現的非原子寫入
   已於 round 10 修正）：
   - 收尾更新寫不進去時，保留下來的上一份紀錄可能還寫著「執行中」；
     Run 頁會說出來，但 SSE 不會因此收線（不把 `run_crash.txt` 投影成
     終局狀態）。
   - `run_crash.txt` 與 `unreaped_process.json` 本身仍是 `O_TRUNC`
     寫入：它們是其他寫入已經失敗時的最後線索，刻意走最少步驟的路徑。
   - `os.replace()` 的原子性以同一個 volume 為前提；run 目錄被放在
     不支援原子 rename 的網路檔案系統上時不保證。
   - Execution / Action Layer 是否可視為 hard-lock，**交由獨立審查判定**；
     本文件只記錄證據。
3. **守衛設計上看不到、由指紋比對兜底的**（§50.1、§50.3）：在
   `_guard_dir_fd_opens` 之前就存成別名的 `os.open`；`os.mkfifo` /
   `os.mknod`；C 擴充直接寫檔；已分離且父行程先結束的孫行程；
   沒有 psutil 的環境只剩 socket 物件那一層。
4. **科學完成點的剩餘窗口**（§49.4）：executor 回來到立旗之間的
   直譯器檢查點；executor 內部 `mark_complete` 之後的 progress
   callback；無法攔截的行程終止（SIGTERM / TerminateProcess）。
5. **Phase 3 尚未開始**：`scenarios/`、`sensors/`、`compatibility/`、
   `artifacts/` 四個子套件尚未實作（§39），
   因此 §7 / §8 的 Wizard、§10 的相容矩陣、UAT-01~03 都還不能跑。

---

# Appendix G — v0.6.0 → v0.7.0 變更對照

本附錄併入 `docs/SAI_v0.6.0_TO_CURRENT_DELTA.md`（2026-09-06）的全部裁決，
並補上該稽核之後的 round 2~8（`32cb9cb`..`eb8ad9c`）。

## G.1 原 delta 稽核的八項（CURRENT 已超越 SAI）

| # | 主題 | v0.7.0 處置 |
|---|---|---|
| D-01 | Formal one-shot claim | 併入 **§18** |
| D-02 | claim 狀態推進原子性 | 併入 **§18** |
| D-03 | resume 與 identity 驗證歸屬 | 併入 **§18** |
| D-04 | lifecycle exception-safety | 併入 **§18** |
| D-05 | Final E2 前置條件（8 項機器強制） | **§19.2** 標為顯示層；判定見 **§51.1** |
| D-06 | Golden Baseline 必須綁 identity | 併入 **§6** |
| D-07 | `formal_config` gate 假 PASS | 併入 **§51.1** 附註 |
| D-08 | 頂層導航 13 → 5 | **§20** 整節改寫 |

術語（Research Profile vs Project）與導航（13 vs 5）兩項不一致，
裁決均為**以 CURRENT 為準**，已寫入 §20。

## G.2 round 2~10 新增（原 delta 稽核未涵蓋）

| 輪次 | 主題 | 落點 |
|---|---|---|
| — | Run attribution + boundary | §49.1 |
| — | 能力模型 | §49.2 |
| — | Executor registry / scope | §49.3 |
| — | Stage events / 終局唯一 | §49.4 |
| r5 | 源碼守衛「通過但什麼都沒守」 | §50.2 |
| r6 | UNREAPED 與標記檔 | §49.5 |
| r6 | Port 衛生（8790 唯一） | §50.3 |
| r7 | tripwire chdir 繞道 | §50.1 第 2 關 |
| r7 | marker-only unreaped 投影 | §49.5 |
| r7 | Formal aggregate 事件歸戶 | §49.3 約束 2 |
| r8 | tripwire 末段別名繞道 | §50.1 第 3 關 |
| r8 | 科學 COMPLETE 不得被呈現層改判 | §49.4 |
| r8 | 執行期 listener 守衛 | §50.3 |
| r9 | dir_fd / openat 落點、`rmtree` 事前判定、`O_TRUNC` | §50.1 |
| r9 | hardlink 以 inode 身分判定 | §50.1 第 4 關 |
| r9 | listener 以擁有權驗收尾（含 port 0）、子行程帳本 | §50.3 |
| r9 | 科學完成點之後連 KeyboardInterrupt / SystemExit 都不改判 | §49.4 |
| r9 | `run.json` 非原子寫入（發現、未修） | §51.4 第 2 項 |
| r10 | run.json 原子替換、失敗保留上一份、壞紀錄隔離與刪除拒絕 | §49.6 |

## G.3 v0.6.0 原樣沿用、未改動的章節

§0.3 名詞定義、§3.1 七條架構原則、§4.1/§4.2 Profile 狀態機與內容、
§5 Thesis Profile Protection Contract、§6.2 TGR-01~12 比對項、
§7/§8 Wizard UX、§9/§10 Evidence Contract 與相容矩陣、
§11 Basic/Advanced Mode、§23/§24 Immutable 欄位政策與 Clone workflow、
§28 Plugin Governance、§34 UAT-01~06、§38 Change Budget、
§40 Frontend Design Rules、Appendix A~F。

其中 **§5 直接作為遷移不變量清單**、**§38 為本階段最重要的約束之一**
（能用 adapter / registry / wrapper 解決就不動 stable core）。

## G.4 不得因平台化而改變的（§5.2 的執行面重述）

- class order（Empty / Water-filled / Bubbly / Misty）
- ToF 500×4 semantics
- selective escalation / routing
- frozen severity
- Agent prompts / schemas / evidence contract
- 現有 E1/E2 科學參數、decision formula、thresholds、model weights
- Final E2 sample design
- `ACTIVE_LINEAGE` 解析語意（pointer 是 resolver，不是 identity）
- **families 36–43：不得生成、讀取、預覽、掃描或執行**

---

# 結語

PC-MEF v0.6.0 的平台化目標可以濃縮為：

> **一套可由研究者透過友善前端建立情境、加入感測器、調整開發參數、預覽模擬、管理資料與模型、啟動並監控實驗的多模態研究工作台；同時用 Research Profile、Freeze、Plugin/Adapter contract、Formal Production Executor 與 Golden Regression 將「未來可擴充」與「目前論文不可污染」嚴格分開。**

最重要的系統判準不是畫面功能很多，而是教授提出新的 Scenario、Sensor 或參數要求時，研究者能以新的 Development Profile 吸收需求；而已凍結的 Thesis Profile 仍保持完全相同的 scientific identity、資料隔離與正式執行規則。
