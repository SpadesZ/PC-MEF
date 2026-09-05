# SAI v0.6.0 → CURRENT freshness / delta audit

**稽核時間：** 2026-09-06
**稽核對象：** `PC-MEF_SAI_v0.6.0_Extensible-Research-Workbench_Platformization.md`（文件日期 2026-09-01）
**CURRENT HEAD：** `721b91b`（`feat/part6-admin-llm-and-batch7-audit`，即 `origin/HEAD`）
**Final E2 gate：** 1 / 8（本次稽核為唯讀，未變動）

---

## 0. 這份文件要回答什麼

SAI v0.6.0 不是外部草稿，它已經是**被實作進來的規格**：
原始碼中 96 處、40 個檔案直接引用 `SAI v0.6.0 §…` 或其 `ACC-*` 驗收編號
（例：`core/active_lineage.py` 的模組定位寫的是「SAI v0.6.0 §18 fail-closed
與 ACC-FML-02 在程式層的落點」）。

但它的文件日期是 2026-09-01，而 09-02 ~ 09-05 三輪 formal lifecycle 收斂
（NOTE-084~092）之後，有一部分 SAI 描述已經**落後於實作**。

因此本文件的用途只有一個：

> 在平台化重構時，分清楚哪些 SAI 章節可以直接照用，
> 哪些照用會把已經修好的東西改回去。

**判定規則（本輪定案）：**

1. SAI 與 CURRENT Formal Production Path 衝突 → **一律以 CURRENT 為準**。
2. SAI 未涵蓋而 CURRENT 已實作 → SAI 視為**沉默**，不得當成「可以簡化」。
3. SAI 有、CURRENT 尚未實作且不衝突 → **優先重用 SAI 設計**，不另發明。

---

## 1. CURRENT 已超越 SAI —— 照 SAI 實作會造成回歸

這一節是本次稽核最重要的部分。以下每一項，SAI 都**沒有寫錯，只是沒有寫**；
若平台化時「照文件重寫一份」，等於把 09-02~09-05 修掉的洞放回去。

| # | 主題 | SAI v0.6.0 說法 | CURRENT 實作 | 風險 |
|---|---|---|---|---|
| D-01 | Formal one-shot claim | §18 只要求 fail-closed、identity mismatch refuse | `experiments/run_claim.py`：`RESERVED` / `RUNNING` / `COMPLETE` / `INTERRUPTED_RESUMABLE` 四態，claim 以 `O_EXCL` 建立 | SAI 沒有 claim 概念。重寫 Formal 入口時若只做 fail-closed，一次性實驗會失去獨佔保護 |
| D-02 | claim 狀態推進原子性 | 未涵蓋 | 單調遞增 `revision` + `.RUN_CLAIM.json.rev<N+1>` 的 `O_EXCL` 權杖 CAS；刻意不用 lock 檔（孤兒鎖無法釋放） | 任何「讀出來改一改寫回去」的 profile/claim 儲存實作都會重新引入 read-then-write 競態 |
| D-03 | resume | §4.2 `formal_policy.resume` 只有一個欄位 | pre-flight 認得四種 claim 現況；identity 只由 claim layer 驗證，pre-flight 刻意不重複 | 若平台在 pre-flight 再驗一次 identity，兩份實作會漂移，且較寬鬆的那份會先跑 |
| D-04 | lifecycle exception-safety | 未涵蓋 | claim 生命週期移出執行本體，`_execute_formal_e2` 外只有一個 try；另有 `pcmef formal reclaim` | 卡在 `RUNNING` 的 claim 會同時擋掉 fresh 與 resume |
| D-05 | Final E2 前置條件 | §19.2 列 9 項**畫面** pre-flight（Profile Frozen / Scenario identity / …） | `experiments/final_gate.py` 8 項**機器強制**前置條件 | 兩者不是同一組，用途也不同。Final E2 進入條件以 `final_gate` 的 8 項為準，§19.2 只能當顯示層參考 |
| D-06 | Golden Baseline | §6 定義 TGR-01~12 比對項 | canonical baseline 必須綁 lineage lock hash + runtime identity + code revision；手寫 `{"canonical": true}` 不再被接受 | SAI 沒說 baseline 要綁 identity。不綁的 baseline 是宣稱，不是基準 |
| D-07 | formal_config gate | 未涵蓋 | 更名為 `formal_config_matches_current_llm_runtime`，並改為依賴 `llm_runtime` gate（原本會假 PASS） | 舊語意會讓 Status 顯示「已重凍」而其實兩者都沒有 |
| D-08 | 頂層導航 | §20 列約 13 個頂層項目（Dashboard / Profiles / Scenario Library / Sensor Library / Simulation / Datasets / Models / Fusion / LLM Agents / Experiments / Results / Artifacts / System） | `console/navigation.py` 收斂成**五個**，且維護契約明文禁止加第六個 | **本輪最關鍵的 UI delta。** 見 §3 |

---

## 2. SAI 仍然有效，直接重用

以下章節與 CURRENT 無衝突，平台化應直接沿用，不重新發明：

| 章節 | 內容 | 重用方式 |
|---|---|---|
| §0.3 | 名詞定義（Research Profile / Scenario Plugin / Sensor Adapter / Evidence Contract） | 直接沿用術語，不另造新詞 |
| §3.1 | 七條最高架構原則（UI 不自行拼流程、Web 與 CLI 同一 service、Formal 唯一 executor…） | 作為本輪架構驗收條件 |
| §4.1 / §4.2 | Profile 狀態機與 Profile 內容 YAML | 作為 Project schema 骨架（狀態機需與 claim 狀態對映，見 §3） |
| §5 | Thesis Profile Protection Contract | **直接作為 PC-MEF 遷移的不變量清單** |
| §6 | Golden Regression TGR-01~12 | 比對項清單重用；canonical 判定改用 CURRENT 的 identity 綁定 |
| §7 / §8 | Scenario Wizard、Sensor Wizard 七步驟 UX | 本輪不實作，但保留為 Phase 3 之後的 UX 藍本 |
| §9 / §10 | Modality Evidence Contract、Sensor × Scenario 相容矩陣 | 作為 Research Design 的資料模型 |
| §11 | Basic / Advanced Mode 分層 | 直接沿用 |
| §23 / §24 | Immutable/Mutable 欄位政策、Clone workflow | **直接作為 Phase 7 clone 語意**（scientific fields immutable、Clone 才能改研究性設定） |
| §28 | Plugin Governance | 保留 |
| §34 | UAT-01 ~ UAT-06 | **直接作為本輪驗收腳本來源** |
| §38 | Change Budget（能用 adapter / registry / wrapper 解決就不動 stable core） | **本輪最重要的約束之一**，與使用者第十四節指示一致 |
| §39 | 建議目錄樹 `pcmef/platform/…` | 沿用邊界；文件本身已註明「不要求為了符合目錄樹搬動既有 stable module」 |
| §40 | Frontend Design Rules | 直接沿用 |
| Appendix B/C/D | Scenario / Sensor / Run Event 最小 schema | Run Event schema 直接作為 Phase 5 進度事件基礎 |

---

## 3. 需要調和的兩個不一致

### 3.1 術語：Research Profile vs Project

SAI 通篇稱 **Research Profile**；本輪任務書稱 **Project**。
兩者指的是同一層（版本化的研究設定容器 + namespace 邊界）。

**決議：** 對外顯示與新程式碼統一用 **Project**，並在 schema 內保留
`profile_state` 對映欄位，讓 SAI §4.1 狀態機仍可直接引用。
不同時維護兩套詞彙 —— 兩套詞彙必然漂移。

### 3.2 導航：13 個頂層 vs 5 個頂層

SAI §20 的資訊架構是「功能清單」，`console/navigation.py` 的五個入口是
「問題清單」（每一項的 `hint` 是一個問句：我要跑什麼 / 系統怎麼跑 /
跑出了什麼 / 研究做到哪 / provider 設定）。

後者是 09-03 之後刻意收斂的結果，且維護契約寫明：

> 不得為了新功能再加第六個頂層項目。五個入口是刻意的收斂；
> 新功能要歸進其中一個，否則首頁會重新開始堆疊（那正是要修的問題）。

**決議：以 CURRENT 為準，維持五個頂層入口。**

Project **不是第六個 tab**，而是**五個入口之上的一層**。
這同時滿足使用者第十三節的指示（「不要只是多塞一個 tab」）與
`navigation.py` 既有的維護契約 —— 兩者在此收斂到同一個答案。

連帶：`navigation.py:146` 的 `breadcrumb()` 目前把 `PC-MEF` 寫死為根，
必須改成 Workspace → Project → …，否則切換 Project 後麵包屑仍然說 PC-MEF。

---

## 4. 本輪不得因平台化而改變的（SAI §5.2 + 使用者第十五節）

- class order（Empty / Water-filled / Bubbly / Misty）
- ToF 500×4 semantics
- selective escalation / routing
- frozen severity
- Agent prompts / schemas / evidence contract
- 現有 E1/E2 科學參數、decision formula、thresholds、model weights
- Final E2 sample design
- ACTIVE_LINEAGE 解析語意（pointer 是 resolver，不是 identity）
- families 36–43：**不得生成、讀取、預覽、掃描或執行**

---

## 5. 遷移安全基準（Phase 0 產出）

平台化若必須搬動 artifact，搬動前後以此基準逐檔比對：

| 項目 | 值 |
|---|---|
| 涵蓋範圍 | `freeze/` `configs/` `registry/` `regression/` `provenance/` `schemas/` |
| 檔案數 | 95（freeze 71 / configs 17 / registry 1 / regression 1 / provenance 2 / schemas 3） |
| manifest hash | `bc3114cd5848b4ccc978bfa6e5a5ffe09df186392227bd2431ddf3d035048266` |
| ACTIVE_LINEAGE | `freeze/runs/PFC-001`（status ACTIVE，supersedes `freeze`） |

此清單刻意不列舉 families 36–43。其 `dataset_manifest.json` 尚未生成
（`final_gate.final_partition_manifest` 為 blocker），本次稽核亦未讀取該分割。
