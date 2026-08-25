# PC-MEF Research System — 決策記錄（NOTES）

本檔是本系統唯一的決策理由來源。程式碼中任何 `NOTE(NOTE-NNN)` 標記，
在本檔必須有同號的完整條目；只有一行摘要不算數。

規格 baseline：
- `SRC-PLAN` = PC-MEF 實驗計畫 v0.9.7S（Evidence-Integrity Hardened Thesis Core）
- `SRC-SAI` = PC-MEF SAI v0.5.0（LLM Setup / Task Binding Integrated）

規格文件的硬性要求：**未經教授核定的 numeric threshold / allocation 一律保持
formal-blocking，禁止實作端自行補值**（SRC-PLAN Appendix A Formal-readiness note、
SRC-SAI Appendix F）。本檔的 `NOTE-005` 說明系統如何強制執行這條。

---

## NOTE-001 四特徵 canonical 順序固定為 Distance / Ambient / Signal / Sigma-like

**決策**：`TOF_SCHEMA = ("distance_mm", "ambient_rate_mcps", "signal_rate_mcps", "sigma_like")`，
並且任何 array flatten / reshape 都必須引用這個常數，不得依文字敘述順序推測。

**理由**：SRC-SAI §8 的 Compatibility rule 明確指出兩份來源的列舉順序不同 ——
Notion 四參數推論程式的 feature order 是 Distance → Ambient → Signal → Sigma，
但 SRC-PLAN 正文敘述時常寫成 Distance / Signal / Ambient / Sigma。這是
SRC-D04（array channel order 錯位）的直接風險：若某個模組照正文順序 flatten，
signal 與 ambient 兩欄會靜默對調，而且因為兩者都是 rate、數量級接近，
不會觸發任何 range 檢查，只會讓 E1 的 Wasserstein 與 perception 的輸入同時錯掉。

**採 Notion 順序而非正文順序的原因**：Notion 順序是實際跑過前研究資料的程式碼順序，
正文順序只是散文敘述。SRC-SAI §8 也裁定「新系統的 canonical order 也採這個順序」。

**命名歧異**：SRC-SAI §8 的 `LegacyCSVAdapter.TOF_COLUMNS` 第四欄寫作
`sigma_mm_or_surrogate`，Appendix A1 的 CanonicalCase `tof_schema` 寫作 `sigma_like`。
本系統以 Appendix A1 為 canonical（`sigma_like`），因為 CanonicalCase 才是跨模組契約；
`sigma_mm_or_surrogate` 僅作為 legacy adapter 的來源欄位別名保留。
第四欄不得被稱為「真實 VL53L0X internal Sigma」（SRC-SAI §10 禁止做法）。

**如何應用**：`pcmef.core.constants.TOF_SCHEMA` 是唯一來源。任何模組若需要
「顯示順序」與 array index 不同，必須分開兩個變數（SRC-D04 的處理方式：
canonical schema 固定 order，顯示名稱與 array index 分離）。

相關：[NOTE-002]

---

## NOTE-002 顯示順序與 array index 必須是兩個獨立概念

**決策**：`TOF_SCHEMA`（array index 真值）與 `TOF_DISPLAY_ORDER`（報表/圖表用）
分開宣告，且 `TOF_DISPLAY_ORDER` 只能存放欄位名稱，不得被任何數值路徑用來取 index。

**理由**：SRC-PLAN Appendix C 的 SRC-D04 audit item 要求
「canonical array order 固定 Distance, Ambient, Signal, Sigma-like；display order 與 array index 分離」，
狀態為 UNIT TEST。若只有單一常數，任何人為了讓論文表格符合正文敘述順序而改動它，
就會同時改掉數值路徑。分成兩個常數之後，改 display order 不可能影響數值。

**如何應用**：`core/constants.py` 提供 `tof_index(name)` 供數值路徑取 index；
報表層只准用 `TOF_DISPLAY_ORDER` 排版，不准用它 enumerate 出 index。

相關：[NOTE-001]

---

## NOTE-003 InferencePayload 的 quality_cues 不得包含 perception 模型導出的量

**決策**：`InferencePayload.quality_cues` 的 allowlist 只收「由當前 RGB/ToF evidence
直接可觀測計算」的量（例如 ToF sigma 統計、signal rate 統計、temporal variability、
RGB 亮度/模糊/對比）。**predictive entropy（H(p_T)、H(p_V)）被排除在 agent payload 之外**，
即使它出現在 SRC-SAI §15 的 quality cue 表格中。

**理由**：SRC-SAI §15 的表格是在描述 **Reliability Estimator 的 q_T / q_V 特徵向量**
（numerical path 用來 fit logistic regression 的輸入），不是 Multi-Agent 的
InferencePayload。兩者是不同的東西，只是都叫 "quality cue"。

把 predictive entropy 放進 agent payload 會直接破壞本研究的核心主張：
SRC-PLAN 圖 5 的 core idea 是「Numerical reliability and structured evidence
remain separate until the final case-wise fusion」，而 F=(1-g)p_rel + g·s_A 的
decision-space interpolation 語意，前提就是 s_A 不是 p_rel 的函數。若 agent 看得到
H(p_T)，s_A 就與 p_rel 統計相關，G5 相對 G4 的任何改善都會失去因果解釋，
審查時無法辯護。

另外 SRC-PLAN §2.2.3 對 payload 的措辭是「quality_cues 只能由當前 evidence
可觀測導出」——模型輸出的 entropy 不是 evidence 可觀測量，是模型的函數。

**如何應用**：`core/inference_payload.py` 的 `OBSERVABLE_QUALITY_CUES` 與
`core/constants.py` 的 `RELIABILITY_FEATURE_CUES` 是兩份不同的 allowlist，
且前者不得包含後者的 `*_predictive_entropy`。單元測試 `test_inference_firewall.py`
以斷言鎖住這個不相交關係。

相關：[NOTE-004]

---

## NOTE-004 opaque_case_id 採 run-scoped keyed HMAC，而非全域隨機 UUID

**決策**：`opaque_case_id = HMAC-SHA256(run_salt, canonical_case_id)[:16]` 轉 hex，
`run_salt` 每個 formal run 隨機產生一次，只存在 evaluator-only 的 opaque map 檔中，
不進 manifest、不進 lock 的可讀欄位、不進送給 provider 的任何 payload。

**理由**：SRC-SAI Appendix I1 允許「random UUID 或 keyed opaque mapping」兩種做法。
選 keyed mapping 是因為 FR-018 resume 與 FR-031 LLM resume immutability 要求
「已完成 case resume 時只讀 frozen raw/validated response，不得重新 call provider」。
若 opaque id 每次載入都重新隨機產生，同一個 canonical case 在 resume 後會拿到不同
opaque id，evaluator join 需要額外對照表才能還原，等於多開一條可能錯接的路徑。
run-scoped HMAC 讓同一 run 內 deterministic（resume 安全），跨 run 不同
（避免 opaque id 本身變成跨 run 可累積的半永久語意標籤）。

**為何不用全域固定 salt**：固定 salt 會讓 opaque id 在所有 run 之間一致，
時間一久就會被人（或被 log）當成穩定識別碼引用，實質上退化成語意 ID。

**注意**：opaque id 不參與 agent artifact cache key。SRC-SAI §48 的 cache key 是
content-addressed（evidence_hash + representation_mode + model/prompt/schema/runtime hash），
與 case 身分無關，所以 run-scoped opaque id 不影響 cache 命中率。

相關：[NOTE-003]

---

## NOTE-005 未核定數值以 REQUIRED sentinel 表示，讀取時 fail-fast

**決策**：config 中任何尚未由教授核定的 numeric threshold / allocation，一律寫成
`!required` YAML tag（載入後成為 `Required` sentinel 物件）。任何程式路徑讀到
sentinel 就丟 `FormalBlockingError`，且錯誤訊息必須指出 config key 與 SRC 出處。
**實作端不得填入任何預設值**，包含「暫時的」預設值。

**理由**：SRC-PLAN Appendix A 的 Formal-readiness note 與 SRC-SAI Appendix F
都明文寫「未核定的 numeric threshold/allocation 必須保持 formal-blocking，
禁止 Coding Agent 自行補值」。這條規則若只靠註解提醒，實務上一定會在某次
「先跑跑看」時被填上預設值，然後那個預設值會一路存活到 formal run，
最後變成一個沒有人記得出處、卻寫進論文的數字。

用 sentinel + fail-fast 的好處是：缺值不會安靜地變成 0 或 None，
而是在第一次被讀到時就中斷，且中斷點會指出是哪個 key 缺教授裁決。

**目前已知處於 formal-blocking 的數值**（隨核定逐項解除）：
- `real_split_policy.allocation` / `minimum_per_class`（SRC-PLAN §3.1、SRC-SAI §7.9）
- `gate.alpha/beta/gamma` 的最終值（搜尋空間已定義為 simplex grid step 0.1，
  但最終值必須由 validation 選出後 freeze，不得手填）
- `conflict_operational.delta`（SRC-PLAN §3.3、SRC-SAI I4）
- `statistics.bootstrap_B` / `bootstrap_seed`（SRC-SAI §24）
- `e2_sample_size.final_n_per_class` / `severity_allocation`（SRC-PLAN §3.3 FR-023）
- E2 各 condition 的 `initial coverage target`：文件寫 100/25-per-class 為
  **initial target**，final N 必須由 e2_pilot 決定並鎖入 `e2_sample_size.lock`，
  因此 100 不得被當成 final 值硬寫。

**如何應用**：`core/config.py` 的 `Required` sentinel 與 `resolve()`。
`--formal` 模式下另外會做一次全樹掃描，任何殘留 sentinel 直接拒絕啟動，
不等到該值被讀到才失敗（NFR-04 fail-fast）。

---

## NOTE-006 stabilize_prob 是全系統唯一的機率正規化 API，禁止影子複製

**決策**：`pcmef.core.numeric.stabilize_prob(p, eps=EPS_P)` 是唯一簽章。
gate、fusion、agents、metrics 一律 `from pcmef.core.numeric import stabilize_prob`，
不得在模組內另外定義同名函式或改寫 eps 預設值。

**理由**：SRC-SAI §16 明文列出 invariant「stabilize_prob API: single signature
stabilize_prob(p, eps=EPS_P) across all modules」，Appendix G1 也註明
"same stabilize_prob signature imported everywhere; no shadow copy"，
並在 Appendix G cross-document audit 中列為 FR-P0-00。

風險具體來說是：JSD 與 entropy 若各自用不同 eps clipping，
D(p,p) < 1e-12 這條 invariant 仍會過，但 D 的上界會偏移，
於是 g = clip(αD + βU + γQ) 的分佈跟著偏移，而 alpha/beta/gamma 是在
validation 上選出來的 —— 等於 gate 被一個沒被記錄的數值差異調參，
且 formal freeze 的 hash 完全看不出來。

**如何應用**：`tests/unit/test_numeric.py` 掃描 `pcmef/` 全樹，
斷言除 `core/numeric.py` 外沒有第二處 `def stabilize_prob`。

---

## NOTE-007 Google adapter 不得使用 raise_for_status，且 API key 走 header 不走 query

**決策**：本系統的 Google/Gemini provider adapter (1) 不呼叫
`response.raise_for_status()`，改為自行檢查 status code 後丟自製例外；
(2) API key 放 `x-goog-api-key` request header，不放 URL query string。

**理由**：參考實作 `rootmedicals-a/ebm-rag/lava/adapter/google.py` 目前
同時踩到兩個問題。`raise_for_status()` 產生的 `HTTPStatusError` 訊息會包含
完整 request URL，而該 adapter 把 key 放在 `?key={api_key}`，所以 key 會被帶進
exception text。它雖然有 `safe_error()` 做遮蔽，但那條 regex
`r"([?&]key=)[^'\"\\s]+"` 在 character class 裡寫的是 `\\s`，
在 raw string 中意義是「反斜線或字母 s」，不是「空白字元」——
於是 key 只會被遮蔽到第一個出現的字母 `s` 為止，其餘明文外洩。

這件事在 roothinks 專案已經以 commit `1b3ccb2`（「Google adapter 不得用
raise_for_status，會把 API key 帶進錯誤訊息」）修掉，本系統直接繼承該結論，
不重蹈覆轍。

對本研究的額外重量：SRC-SAI NFR-08 要求「API key 不寫入 repo/manifest/log」，
LLM-SEC-01 acceptance test 是「manifest / report / DB plaintext 全域掃描無 secret」。
provider 例外訊息會進 `llm_verification_logs.error_sanitized` 與 formal run 的
error artifact，屬於上述掃描範圍，所以這不只是衛生問題，是會讓 LLM-SEC-01 直接
FAIL 的問題。

**如何應用**：adapter 基底類別提供 `raise_for_provider_error(response)`，
並在 `tests/secret/` 加一條 fixture test：以已知 key 觸發 4xx/5xx，
斷言例外訊息與 log 皆不含該 key 的任何連續 8 字元子字串。

---

## NOTE-008 Repo 位置與 real data 現況

**決策**：系統建於 `C:\Users\Franky Kuo\Desktop\pcmef-research`，
與規格文件所在的 `Desktop\pre碩論\正式可用\正式實驗` 分開存放。
目錄名稱沿用 SRC-SAI §31 目錄樹的 `pcmef-research/`。

**real data 現況（2026-08-25 盤點）**：前研究（蔡連興）的原始 ToF CSV
與 RGB 影像**不在本機**。已掃描 Desktop / Documents / Downloads 全樹，
沒有任何符合 ToF recording 特徵的 CSV，`pre碩論` 資料夾內只有文件檔。

**影響**：M0 Data Audit 無法完成，連帶 `real_split_policy.lock`、E1-G01、
E1-G09 全部無法通過，正式 E1 不可能啟動。這不是可以繞過的工程問題 ——
SRC-SAI §7.9 的 audit rule 明文「不得 hard-code usable N=560」，
nominal logical count（4×140=560）只是計算上的數字，
usable N 必須由實際 physical source file 盤點後決定。

**因應**：Batch 1/2 的 `LegacyCSVAdapter` 與 audit CLI 仍照規格完整實作，
並以合成 fixture 做單元測試（涵蓋 500×4 shape、缺欄、NaN、長度差異、
Sigma scale 未解析等 FAIL 路徑）。等真實資料到位後直接指向
`data/raw_real/` 即可執行，不需要改碼。

---

## NOTE-009 Synthetic scenario ID 的 condition token 採完整拼寫

**決策**：`CONDITION_ID_TOKEN` 使用 `clean` / `visiondegraded` / `tofdegraded` /
`conflictstress`，因此 Conflict-Stress 的 scenario ID 形如
`syn_conflictstress_bubbly_0042`。

**理由**：SRC-SAI 內部有兩處範例不一致。§34 的 IDs/Naming Convention 表格寫
`syn_conflict_water_mid_0042`（用縮寫 `conflict`），Appendix A1 的 CanonicalCase
完整實例寫 `syn_conflictstress_bubbly_0042`（用完整拼寫）。

採 A1 的原因有兩個。其一，A1 是一份完整的 case 契約實例，§34 那格是格式示意
（同一格還把序號、severity 位置一起示意），實例的證據力較強。其二，縮寫
`conflict` 會與未來任何以 conflict 開頭的 condition 撞名，而 condition 名稱直接
編進 immutable scenario ID —— 一旦 formal run 開始就不能改，撞名的代價是整批
scenario ID 需要重新產生，連帶 e2_sample_size.lock 與 formal IDs 全部失效。

**附帶規則**：Clean condition 傳入 severity 會被拒絕。SRC-SAI §13 的 scenario rule
明訂 Clean 使用 Nominal-A/B/C seed strata 而非 degradation severity，
若允許 `syn_clean_empty_mid_0001` 這種 ID 存在，等於在 ID 層面承認
「Clean 也有嚴重度」，與 frozen scenario rule 直接矛盾。

**如何應用**：`pcmef/core/ids.py` 的 `synthetic_scenario_id()`；
`tests/schema/test_canonical_case_and_ids.py` 以 §34 與 A1 的範例字串直接斷言。

---
