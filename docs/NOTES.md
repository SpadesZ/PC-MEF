# PC-MEF Research System — 決策記錄（NOTES）

本檔保存無法只從語法還原、且後續維護不得任意改寫的研究完整性與安全決策。
程式中的 `NOTE(NOTE-NNN):` 必須能在此找到同號條目；若行為改變，需同步更新
決策、測試與引用處，禁止留下失效 reference。號碼不重用。

每則條目固定五個欄位：**決策日期／適用範圍／決策／原因／驗證**，
必要時加「維護邊界」。

規格 baseline：
- `SRC-PLAN` = PC-MEF 實驗計畫 v0.9.7S（Evidence-Integrity Hardened Thesis Core）
- `SRC-SAI` = PC-MEF SAI v0.5.0（LLM Setup / Task Binding Integrated）

引用完整性由 `tests/test_repo_integrity.py` 自動稽核。

---

## NOTE-001 四特徵 canonical 順序固定為 Distance / Ambient / Signal / Sigma-like

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/constants.py` 的 `TOF_SCHEMA` 與 `tof_index()`；
所有對 ToF 陣列做 flatten、reshape 或取欄的模組（adapters、surrogate、
models.tof_1dcnn、gate、stats）。

**決策**：`TOF_SCHEMA = ("distance_mm", "ambient_rate_mcps", "signal_rate_mcps",
"sigma_like")`。任何取欄一律經由這個常數或 `tof_index()`，不得依文字敘述順序推測。

**原因**：SRC-SAI §8 的 Compatibility rule 明確指出兩份來源的列舉順序不同 ——
Notion 四參數推論程式的 feature order 是 Distance → Ambient → Signal → Sigma，
但 SRC-PLAN 正文敘述時常寫成 Distance / Signal / Ambient / Sigma。這是
SRC-D04（array channel order 錯位）的直接風險：若某個模組照正文順序 flatten，
signal 與 ambient 兩欄會靜默對調，而且因為兩者都是 rate、數量級接近，
不會觸發任何 range 檢查，只會讓 E1 的 Wasserstein 與 perception 的輸入同時錯掉。

採 Notion 順序而非正文順序，是因為 Notion 順序是實際跑過前研究資料的程式碼順序，
正文順序只是散文敘述；SRC-SAI §8 也裁定「新系統的 canonical order 也採這個順序」。

命名歧異另有一處：SRC-SAI §8 的 `LegacyCSVAdapter.TOF_COLUMNS` 第四欄寫作
`sigma_mm_or_surrogate`，Appendix A1 的 CanonicalCase `tof_schema` 寫作 `sigma_like`。
本系統以 Appendix A1 為 canonical，因為 CanonicalCase 才是跨模組契約；
`sigma_mm_or_surrogate` 僅作為 legacy adapter 的來源欄位別名保留。

**驗證**：
```
py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -k "canonical_order or tof_schema"
```

**維護邊界**：第四欄不得被稱為「真實 VL53L0X internal Sigma」（SRC-SAI §10 禁止做法）。
相關：[NOTE-002]

---

## NOTE-002 顯示順序與 array index 必須是兩個獨立概念

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/constants.py` 的 `TOF_DISPLAY_ORDER`；所有報表與圖表產生模組。

**決策**：`TOF_SCHEMA`（array index 真值）與 `TOF_DISPLAY_ORDER`（報表排版用）
分開宣告。`TOF_DISPLAY_ORDER` 只能存放欄位名稱，不得被任何數值路徑用來取 index。

**原因**：SRC-PLAN Appendix C 的 SRC-D04 audit item 要求
「canonical array order 固定 Distance, Ambient, Signal, Sigma-like；
display order 與 array index 分離」，狀態為 UNIT TEST。若只有單一常數，
任何人為了讓論文表格符合正文敘述順序而改動它，就會同時改掉數值路徑。
分成兩個常數之後，改 display order 在結構上不可能影響數值。

**驗證**：
```
py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -k display_order
```
該測試同時斷言兩者順序確實不同，避免有人「順手對齊」而讓這條防線失效。

相關：[NOTE-001]

---

## NOTE-003 InferencePayload 的 quality_cues 不得包含 perception 模型導出的量

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/inference_payload.py` 的 `OBSERVABLE_QUALITY_CUES`；
`pcmef/core/constants.py` 的 `RELIABILITY_FEATURE_CUES`；所有 agents 模組。

**決策**：`InferencePayload.quality_cues` 的 allowlist 只收「由當前 RGB/ToF evidence
直接可觀測計算」的量。**predictive entropy（H(p_T)、H(p_V)）被排除在 agent payload
之外**，即使它出現在 SRC-SAI §15 的 quality cue 表格中。

**原因**：SRC-SAI §15 的表格描述的是 **Reliability Estimator 的 q_T / q_V 特徵向量**
（numerical path 用來 fit logistic regression 的輸入），不是 Multi-Agent 的
InferencePayload。兩者是不同的東西，只是都叫 "quality cue"。

把 predictive entropy 放進 agent payload 會直接破壞本研究的核心主張：
SRC-PLAN 圖 5 的 core idea 是「Numerical reliability and structured evidence
remain separate until the final case-wise fusion」，而 F=(1-g)p_rel + g·s_A 的
decision-space interpolation 語意，前提就是 s_A 不是 p_rel 的函數。若 agent 看得到
H(p_T)，s_A 就與 p_rel 統計相關，G5 相對 G4 的任何改善都會失去因果解釋，
口試時無法辯護。

另外 SRC-PLAN §2.2.3 對 payload 的措辭是「quality_cues 只能由當前 evidence
可觀測導出」—— 模型輸出的 entropy 不是 evidence 可觀測量，是模型的函數。

**驗證**：
```
py -3.10 -m pytest tests/leakage/test_inference_firewall.py -k "quality_cues or disjoint"
```
`test_observable_cues_and_reliability_features_are_disjoint` 以斷言鎖住兩份
allowlist 的不相交關係，任何人把 entropy 加進 agent 那份都會失敗。

相關：[NOTE-004]

---

## NOTE-004 opaque_case_id 採 run-scoped keyed HMAC，而非全域隨機 UUID

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/opaque_ids.py` 全檔；`experiments.e2_formal` 的 run 初始化。

**決策**：`opaque_case_id = HMAC-SHA256(run_salt, canonical_case_id)[:32]` 之 hex，
`run_salt` 每個 formal run 隨機產生一次，只存在 evaluator-only 的 opaque map 檔中，
不進 manifest、不進 lock 的可讀欄位、不進送給 provider 的任何 payload。

**原因**：SRC-SAI Appendix I1 允許「random UUID 或 keyed opaque mapping」兩種做法。
選 keyed mapping 是因為 FR-018 resume 與 FR-031 LLM resume immutability 要求
「已完成 case resume 時只讀 frozen raw/validated response，不得重新 call provider」。
若 opaque id 每次載入都重新隨機產生，同一個 canonical case 在 resume 後會拿到不同
opaque id，evaluator join 需要額外對照表才能還原，等於多開一條可能錯接的路徑。
run-scoped HMAC 讓同一 run 內 deterministic（resume 安全），跨 run 不同
（避免 opaque id 本身變成跨 run 可累積的半永久語意標籤）。

不用全域固定 salt 的理由：固定 salt 會讓 opaque id 在所有 run 之間一致，
時間一久就會被人（或被 log）當成穩定識別碼引用，實質上退化成語意 ID。

`map_hash()` 刻意不涵蓋 salt：lock 檔是 formal provenance 的一部分，會被檢視
與流通，salt 進了 lock 就等於 opaque id 可被任何拿到 lock 的人逆推。

**驗證**：
```
py -3.10 -m pytest tests/leakage/test_inference_firewall.py -k "opaque or map"
```
涵蓋 run 內 deterministic、跨 run 相異、map_hash 排除 salt、竄改偵測與不可覆寫。

**維護邊界**：opaque id 不參與 agent artifact cache key。SRC-SAI §48 的 cache key 是
content-addressed（evidence_hash + representation_mode + model/prompt/schema/runtime
hash），與 case 身分無關，所以 run-scoped opaque id 不影響 cache 命中率。

相關：[NOTE-003]

---

## NOTE-005 未核定數值以 REQUIRED sentinel 表示，讀取時 fail-fast

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/config.py` 的 `Required` 與 `load_config()`；
`configs/base.yaml` 中所有 `!required` 標記項目。

**決策**：config 中任何尚未由教授核定的 numeric threshold / allocation，一律寫成
`!required` YAML tag（載入後成為 `Required` sentinel 物件）。任何程式路徑讀到
sentinel 就丟 `FormalBlockingError`，且錯誤訊息必須指出 config key 與 SRC 出處。
**實作端不得填入任何預設值，包含「暫時的」預設值。**

**原因**：SRC-PLAN Appendix A 的 Formal-readiness note 與 SRC-SAI Appendix F
都明文寫「未核定的 numeric threshold/allocation 必須保持 formal-blocking，
禁止 Coding Agent 自行補值」。這條規則若只靠註解提醒，實務上一定會在某次
「先跑跑看」時被填上預設值，然後那個預設值會一路存活到 formal run，
最後變成一個沒有人記得出處、卻寫進論文的數字。

用 sentinel + fail-fast 的好處是：缺值不會安靜地變成 0 或 None，
而是在第一次被讀到時就中斷，且中斷點會指出是哪個 key 缺教授裁決。
`Required` 刻意做成物件而非 `None`，因為 `None` 會被 `or`、
`get(key, default)` 與各種 falsy 判斷悄悄吸收掉。

目前處於 formal-blocking 的 17 項數值可執行 `py -3.10 -m pcmef.cli config check`
取得最新清單，包含 real split 的 allocation/minimum_per_class/seed、
gate 的 alpha/beta/gamma、conflict 的 delta、E2 的 final_n_per_class/
severity_allocation、兩組 bootstrap 參數等。

其中 E2 各 condition 的 100 / 25-per-class 在 SRC-PLAN §3.3 明確標示為
**initial coverage target**，final N 必須由 e2_pilot 決定並鎖入
`e2_sample_size.lock`，因此 100 不得被當成 final 值硬寫。

**驗證**：
```
py -3.10 -m pytest tests/unit/test_config_and_locks.py -k "required or formal or bypass"
py -3.10 -m pytest tests/test_cli.py -k shipped_base_config
py -3.10 -m pcmef.cli config check
```
`test_default_argument_cannot_bypass_a_required_value` 鎖住最容易被繞過的路徑；
`test_shipped_base_config_is_still_formal_blocking` 會在有人偷填值時失敗。

---

## NOTE-006 stabilize_prob 是全系統唯一的機率正規化 API，禁止影子複製

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/numeric.py`；未來所有 gate、fusion、agents、
reliability、stats 模組。

**決策**：`pcmef.core.numeric.stabilize_prob(p, eps=EPS_P)` 是唯一簽章。
所有模組一律 `from pcmef.core.numeric import stabilize_prob`，
不得在模組內另外定義同名函式或改寫 eps 預設值。

**原因**：SRC-SAI §16 明文列出 invariant「stabilize_prob API: single signature
stabilize_prob(p, eps=EPS_P) across all modules」，Appendix G1 也註明
"same stabilize_prob signature imported everywhere; no shadow copy"，
並在 Appendix G cross-document audit 中列為 FR-P0-00。

風險具體來說是：JSD 與 entropy 若各自用不同 eps clipping，
D(p,p) < 1e-12 這條 invariant 仍會過，但 D 的上界會偏移，
於是 g = clip(αD + βU + γQ) 的分佈跟著偏移，而 alpha/beta/gamma 是在
validation 上選出來的 —— 等於 gate 被一個沒被記錄的數值差異調參，
且 formal freeze 的 hash 完全看不出來。

**驗證**：
```
py -3.10 -m pytest tests/unit/test_numeric.py -k "exactly_one_definition or epsilon_constants"
```
`test_stabilize_prob_has_exactly_one_definition_in_the_codebase` 掃描 `pcmef/`
全樹，斷言除 `core/numeric.py` 外沒有第二處 `def stabilize_prob`。

---

## NOTE-007 Google adapter 不得使用 raise_for_status，且 API key 走 header 不走 query

**決策日期**：2026-08-25

**適用範圍**：未來的 `pcmef/agents/provider.py` 與所有 provider adapter；
`pcmef/core/logging_setup.py` 的遮蔽樣式。

**決策**：本系統的 Google/Gemini provider adapter (1) 不呼叫
`response.raise_for_status()`，改為自行檢查 status code 後丟自製例外；
(2) API key 放 `x-goog-api-key` request header，不放 URL query string。

**原因**：參考實作 `rootmedicals-a/ebm-rag/lava/adapter/google.py` 同時踩到兩個問題。
`raise_for_status()` 產生的 `HTTPStatusError` 訊息會包含完整 request URL，
而該 adapter 把 key 放在 `?key={api_key}`，所以 key 會被帶進 exception text。
它雖然有 `safe_error()` 做遮蔽，但那條 regex `r"([?&]key=)[^'\"\\s]+"`
在 character class 裡寫的是 `\\s`，在 raw string 中意義是「反斜線或字母 s」，
不是「空白字元」—— 於是 key 只會被遮蔽到第一個出現的字母 `s` 為止，
其餘明文外洩。

這件事在 roothinks 專案已經以 commit `1b3ccb2` 修掉，本系統直接繼承該結論。

對本研究的額外重量：SRC-SAI NFR-08 要求「API key 不寫入 repo/manifest/log」，
LLM-SEC-01 acceptance test 是「manifest / report / DB plaintext 全域掃描無 secret」。
provider 例外訊息會進 `llm_verification_logs.error_sanitized` 與 formal run 的
error artifact，屬於上述掃描範圍，所以這不只是衛生問題，是會讓 LLM-SEC-01 直接
FAIL 的問題。

**驗證**：
```
py -3.10 -m pytest tests/secret/test_log_redaction.py -v
```
`_assert_fully_redacted()` 斷言 secret 的任何連續 8 字元片段都不存在，
因此「只遮到第一個字母 s」這種部分遮蔽會被抓出來，不會被誤判為通過。
provider adapter 實作於 M6 完成後，需在 `tests/secret/` 補一條 fixture test：
以已知 key 觸發 4xx/5xx，斷言例外訊息與 log 皆不含該 key。

---

## NOTE-008 Repo 位置與 real data 現況

**決策日期**：2026-08-25

**適用範圍**：整個 repo 的存放位置；M0 Data Audit 與 E1 的可執行性判定。
本條目無對應程式標記，屬環境現況記錄。

**決策**：系統建於 `C:\Users\Franky Kuo\Desktop\pcmef-research`，
與規格文件所在的 `Desktop\pre碩論\正式可用\正式實驗` 分開存放。
目錄名稱沿用 SRC-SAI §31 目錄樹的 `pcmef-research/`。

M0 標記為 BLOCKED；Batch 1/2 的實作以合成 fixture 完成測試，不等待真實資料。

**原因**：前研究（蔡連興）的原始 ToF CSV 與 RGB 影像**不在本機**。
已掃描 Desktop / Documents / Downloads 全樹，沒有任何符合 ToF recording 特徵的
CSV，`pre碩論` 資料夾內只有文件檔。

影響範圍是硬性的：M0 Data Audit 無法完成，連帶 `real_split_policy.lock`、
E1-G01、E1-G09 全部無法通過，正式 E1 不可能啟動。這不是可以繞過的工程問題 ——
SRC-SAI §7.9 的 audit rule 明文「不得 hard-code usable N=560」，
nominal logical count（4×140=560）只是計算上的數字，
usable N 必須由實際 physical source file 盤點後決定。

**驗證**：
```
py -3.10 -m pcmef.cli locks status
```
`real_split_policy` 顯示為 pending，其下游全部顯示 BLOCKED，
與本條目描述的阻塞範圍一致。真實資料到位後，`LegacyCSVAdapter` 直接指向
`data/raw_real/` 即可執行，不需要改碼。

---

## NOTE-010 四個 metric 分檔存放且靠位置對齊，必須逐 metric 保存來源檔名

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/schema.py` 的 `MetricAlignment` 與
`CanonicalCase._check_metric_alignment()` / `_check_sigma_provenance()`；
`pcmef/core/constants.py` 的 `LEGACY_METRIC_FOLDERS`、`LEGACY_CSV_COLUMN_TITLES`、
`SIGMA_REGISTER_CANDIDATES`；未來的 `adapters/legacy_csv.py`。

**決策**：
1. `source_role=real_anchor` 且帶 ToF 的 case 必須提供 `metric_alignment`，
   逐 metric 記錄原始檔名、列數與來源 SHA-256，且四個 metric 的列數必須一致。
2. real case 的 `provenance` 必須明示 `sigma_status`（RESOLVED / UNRESOLVED）。
   canonical 化階段允許 UNRESOLVED，阻擋點在 E1-G08 而非此處。

**原因**：2026-08-25 直接讀 SRC-NOTION 的原始程式後確認三件事實，
這些在 SRC-PLAN 與 SRC-SAI 中只被概括描述，實際情況更嚴重：

其一，資料組織是 `<condition>/<metric_folder>/*.csv` —— 一筆邏輯 recording 的
四個特徵被拆成四個獨立檔案分開存放。合併程式的對齊邏輯是：

```python
for measurement_idx in range(max_files):
    for metric_folder, column_name in metrics.items():
        if measurement_idx < len(all_metric_files[metric_folder]):
            csv_filename, file_path = all_metric_files[metric_folder][measurement_idx]
```

**純粹依 natural sort 後的位置對齊，沒有任何檔名比對**；而且某個資料夾檔案較少時，
`if measurement_idx < len(...)` 只會讓該 metric 被記為 missing 而不中斷。
只要任一 metric 資料夾少一個檔（或多一個暫存檔），該索引之後的所有 recording
都會靜默錯位 —— distance 來自第 57 次測量，sigma 來自第 58 次。
這種錯位不會產生 NaN、不會超出 range、不會有任何症狀，
只會讓 E1 的四特徵 Wasserstein 與 perception 的輸入同時失真。

這正是 SRC-SAI §7.4 要求 `measurement_alignment.csv` 的原因，但該節只說「另保存
每個 metric 的原 filename」，沒有說明後果；實際看過程式碼才知道失效模式是靜默的。

其二，Sigma register 在三份推論程式中不一致：四參數與兩參數用 `0x1E`，
單一參數用 `0x18`，三者 scaling 皆為 `/65536.0`（Signal/Ambient 則一致為
`0x1A`/`0x1C` 加 `/128.0`）。值得注意的是 `0x18` 只出現在
`FEATURES_PER_SAMPLE = 1`（只用 distance）的腳本裡，也就是該常數所讀到的 sigma
從未進入模型，因此很可能從未被驗證過。但這**不能**作為「0x1E 就是對的」的證據 ——
dataset 是由當時的採集腳本產生的，而那份腳本不在這三份之中。
因此系統只記錄兩個候選值，不預設其一。

其三，`sigma_mm = sigma_raw / 65536.0` 這個 scaling 三份一致，
所以 SRC-D02 的爭議點在 register 而非 divisor。

**驗證**：
```
py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -k "metric_alignment or sigma or notion or legacy_csv or metric_folders"
```
涵蓋：缺 alignment 被拒、metric 不齊被拒、四檔列數不一致被拒、
缺 sigma_status 被拒、UNRESOLVED 仍可 canonical 化、
以及 canonical 順序與原始 flatten 程式碼的逐字比對。

**維護邊界**：四個 metric 列數不一致時一律拒絕，**不得靜默裁切到最短長度**。
SRC-SAI §7.5 允許在明確 audit 後以 `configured crop` 處理，
但那必須是 adapter 層的顯式決策並記錄原長度，不是 schema 層的預設行為。

相關：[NOTE-001]

---

## NOTE-009 Synthetic scenario ID 的 condition token 採完整拼寫

**決策日期**：2026-08-25

**適用範圍**：`pcmef/core/ids.py` 的 `CONDITION_ID_TOKEN` 與
`synthetic_scenario_id()`；未來的 `simulation/scenario_generator.py`。

**決策**：condition token 使用 `clean` / `visiondegraded` / `tofdegraded` /
`conflictstress`，因此 Conflict-Stress 的 scenario ID 形如
`syn_conflictstress_bubbly_0042`。Clean condition 傳入 severity 會被拒絕。

**原因**：SRC-SAI 內部有兩處範例不一致。§34 的 IDs/Naming Convention 表格寫
`syn_conflict_water_mid_0042`（縮寫 `conflict`），Appendix A1 的 CanonicalCase
完整實例寫 `syn_conflictstress_bubbly_0042`（完整拼寫）。

採 A1 的原因有兩個。其一，A1 是一份完整的 case 契約實例，§34 那格是格式示意
（同一格還把序號、severity 位置一起示意），實例的證據力較強。其二，縮寫
`conflict` 會與未來任何以 conflict 開頭的 condition 撞名，而 condition 名稱直接
編進 immutable scenario ID —— 一旦 formal run 開始就不能改，撞名的代價是整批
scenario ID 需要重新產生，連帶 `e2_sample_size.lock` 與 formal IDs 全部失效。

Clean 拒絕 severity 的理由：SRC-SAI §13 的 scenario rule 明訂 Clean 使用
Nominal-A/B/C seed strata 而非 degradation severity，若允許
`syn_clean_empty_mid_0001` 這種 ID 存在，等於在 ID 層面承認「Clean 也有嚴重度」，
與 frozen scenario rule 直接矛盾。

**驗證**：
```
py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -k "id_formats or severity or clean_condition"
```
測試以 §34 與 A1 的範例字串逐字比對，並斷言 Clean + severity 會拋
`InvalidIdentifier`。

---
