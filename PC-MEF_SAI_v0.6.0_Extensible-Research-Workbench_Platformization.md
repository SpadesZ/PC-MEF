# PC-MEF Research Platform
# System Analysis with AI（SAI）
## Extensible Research Workbench / Platformization Specification
### PC-MEF 可擴充多模態研究工作台—平台化、前端互動、研究設定檔與正式實驗隔離規格

**文件版本：** v0.6.0  
**文件性質：** Platformization / Extensibility / User Experience / Formal-Isolation SAI  
**日期：** 2026-09-01  
**上位相容文件：** `PC-MEF_SAI_v0.5.0_LLM-Setup_Task-Binding-Integrated`  
**研究核心：** 「結合物理校準模擬與大型語言模型輔助多模態融合之管內液態狀態辨識」既有碩士論文實驗核心  
**本版新增責任：** 將既有論文專用流程提升為可擴充的研究平台，同時保證既有碩論正式研究設定零漂移（zero scientific drift）。

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

```text
PC-MEF Research Platform

Dashboard
Profiles
  ├─ Development
  ├─ Frozen
  └─ Thesis Profile

Scenario Library
Sensor Library

Simulation
Datasets
Models
Fusion / PC-MEF
LLM Agents

Experiments
  ├─ Preview
  ├─ Pilot
  └─ Formal Runs

Results
Artifacts & Provenance
System / Admin
```

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

```text
pcmef/
├─ core/
│
├─ platform/
│  ├─ profiles/
│  │  ├─ models.py
│  │  ├─ service.py
│  │  ├─ freeze.py
│  │  └─ validation.py
│  │
│  ├─ scenarios/
│  │  ├─ registry.py
│  │  ├─ contracts.py
│  │  └─ plugins/
│  │
│  ├─ sensors/
│  │  ├─ registry.py
│  │  ├─ contracts.py
│  │  └─ adapters/
│  │     ├─ rgb.py
│  │     └─ tof.py
│  │
│  ├─ compatibility/
│  │  └─ service.py
│  │
│  ├─ experiments/
│  │  ├─ service.py
│  │  ├─ formal_service.py
│  │  └─ events.py
│  │
│  └─ artifacts/
│     └─ service.py
│
├─ experiments/
├─ agents/
├─ perception/
├─ stats/
└─ ...

web/
├─ routes/
├─ templates/
│  ├─ dashboard/
│  ├─ profiles/
│  ├─ scenarios/
│  ├─ sensors/
│  ├─ simulation/
│  ├─ experiments/
│  └─ results/
└─ static/
```

此為建議邊界，不要求為了符合目錄樹搬動既有 stable module。

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

# 結語

PC-MEF v0.6.0 的平台化目標可以濃縮為：

> **一套可由研究者透過友善前端建立情境、加入感測器、調整開發參數、預覽模擬、管理資料與模型、啟動並監控實驗的多模態研究工作台；同時用 Research Profile、Freeze、Plugin/Adapter contract、Formal Production Executor 與 Golden Regression 將「未來可擴充」與「目前論文不可污染」嚴格分開。**

最重要的系統判準不是畫面功能很多，而是教授提出新的 Scenario、Sensor 或參數要求時，研究者能以新的 Development Profile 吸收需求；而已凍結的 Thesis Profile 仍保持完全相同的 scientific identity、資料隔離與正式執行規則。
