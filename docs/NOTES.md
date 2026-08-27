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

**已知線索（2026-08-26 全站重掃後更新）**：Notion 全部 10 個研究頁面
**零附件**，全站搜尋 "csv" 僅 5 筆且全為程式碼字串，
因此原始 CSV 確定不在 Notion。三條可能的來源，依可能性排序：

1. **Edge Impulse 專案 `AndyCohan / AndyCohan-project-1`**（Target: Raspberry Pi 4）。
   資料當初上傳至此訓練，Edge Impulse 保留 raw data 且支援匯出。
   設定為 448 training windows、5h 6m 8s、4 類 —— 對應 560 筆的 80% 訓練切分，
   代表**完整 560 筆的 500 點序列曾存在於該專案**。這是目前最可能的來源。
2. 樹莓派本機 `/home/pi/`（採集腳本輸出目錄）。
3. Google Drive「實驗交接」—— 已下載並確認**不含**原始逐 recording CSV。

取得 CSV 時必須**同時取得採集當時的腳本** —— 沒有它就無法解析 Sigma
究竟讀自 0x18 或 0x1E（見 [NOTE-010]），E1-G08 會卡在 UNRESOLVED。
**Edge Impulse 上不會有這個答案**：雲端只存上傳後的數值，不存採集程式。

**採集腳本的線索（2026-08-26）**：Notion 六個技術頁面均非採集腳本
（環境架設、合併後處理、impulse 設定截圖、兩份純 Keras 模型定義、
三份推論程式）。但 `classify4.py` 的 docstring 寫「優化內容：
1. 禁用 sensor_data CSV 保存（節省空間）」，程式中為
`SAVE_RAW_SENSOR_DATA = False  # ← 修改：不保存原始傳感器數據`。
「禁用」與「修改」代表**先前存在一版把該旗標設為 True 的同支程式**，
那一版就是（或極接近）採集腳本。向原作者索取時應直接指名這一版。

此線索同時解釋取樣率差異：偏移測試用單一參數腳本（I²C 讀取少）→ 0.0624 s；
主資料集讀四個 metric（交易多）→ 0.082 s，與 Edge Impulse 的 12.19512 Hz 一致。

樹莓派的 SSH / VNC 帳密依 SRC-SAI §30 屬操作資訊，
**不寫入本 repo、SAI config 或任何 manifest**；需要時另循 environment/secrets 管道。

**驗證**：
```
py -3.10 -m pcmef.cli locks status
```
`real_split_policy` 顯示為 pending，其下游全部顯示 BLOCKED，
與本條目描述的阻塞範圍一致。真實資料到位後，`LegacyCSVAdapter` 直接指向
`data/raw_real/` 即可執行，不需要改碼。

---

## NOTE-028 E1-G08 拆分：可驗證的 Sigma channel/scale 與不可觀測的暫存器位址（AMD-001）

**決策日期**：2026-08-27

**適用範圍**：`pcmef/core/constants.py` 的 provenance facet 詞彙與
`E1_G08_CONTRACT_VERSION`；`pcmef/provenance/sigma.py` 的 `ProvenanceFacet`／
`SigmaResolution`／`resolve_sigma()`；`pcmef/audit/e1_gates.py` 的 `_check_g08()`；
`pcmef/provenance/timing.py` 的 `EvidenceLevel.DATASET_PRIMARY`；
`pcmef/core/amendments.py`；`freeze/amendments/AMD-001.amendment.json`。

**決策**：

1. Sigma provenance 由**單一 RESOLVED 旗標**改為**四個獨立 facet**，
   各自持有 `CONFIRMED / CONFLICT / RECONSTRUCTED / UNKNOWN` 之一：

   | facet | 狀態 | 證據位階 |
   |---|---|---|
   | `channel_semantics` | **CONFIRMED**（非測距值） | rank 1 |
   | `numeric_scale` | **CONFIRMED**（/65536） | rank 1 |
   | `register_address` | **CONFLICT** | rank 5 |
   | `original_acquisition_method` | **UNKNOWN** | rank 2（未取得） |

2. **E1-G08 契約升至 v2**：只要求 `channel_semantics` 與 `numeric_scale`
   為 CONFIRMED。`register_address` 為 CONFLICT/UNKNOWN **不影響判定，
   也不使 dataset 失效**。
3. 只有 `CONFIRMED` 能滿足 gate。`RECONSTRUCTED` 刻意不列入
   （`PROVENANCE_GATE_SATISFYING`）。
4. 新增 `EvidenceLevel.DATASET_PRIMARY`（rank 1），timing 偏差改以它為 baseline。
5. 新增 amendment 凍結機制，本次記錄為 **AMD-001**。

**原因**：

**其一，原契約是範疇錯誤，不是門檻設太嚴。** 取得 560 筆 raw dataset
（rank-1，280,000 列）後才看清楚：`channel_semantics` 與 `numeric_scale`
可由資料驗證，而 exact historical register address **原理上無法**由匯出的
浮點數反推 —— 你看不到當初讀了哪個 I²C 位址。把三者綁成同一個旗標，
等於讓兩個可驗證的事實被一個不可觀測的事實永久扣住。

**其二，原本的「刪到剩一個」推論不成立。** v1 的論證是：
「0x1E 是 final range → sigma 欄不是 range → 0x1E 排除 → 候選只剩 0x18」。
三個問題：

- 排除「sigma 欄等於測距值」證成的是 **channel semantics**，不是位址身分。
  `rule_out_range_register()` 的 docstring 本來就寫著「無論那個暫存器叫什麼
  位址」—— 它從一開始就是與位址無關的檢定，v1 把它的結論用錯了地方。
- 候選集 `{0x18, 0x1E}` 來自 legacy 腳本出現過的常數，但產生這 560 筆的
  採集腳本不在其中任何一份。刪掉一個得到的是「未知」，不是「必為另一個」。
- 它把最弱的證據升格為結論：0x18 只出現在**不使用 sigma** 的單參數程式
  （rank 6，很可能是沒清乾淨的死常數），卻被判成 RESOLVED。
  這正是 SRC-HANDOFF §8 禁止的「低位階 evidence 覆寫高位階」。

而且 v1 的結論與系統自己的紅線互相矛盾：NOTE-001 的維護邊界早就規定
第四欄**不得**被稱為「真實 VL53L0X internal Sigma」。既然不能這樣宣稱，
把 E1 資格綁在「知道它讀自哪個暫存器」上，一開始就是內部不一致。

**其三，為什麼這不是「看到結果後改判準」。** 凍結時的前提證據已寫進
`AMD-001.amendment.json` 並經實測而非引述：`FW-02 PASS`、
`heldout_access_count = 0`、`e1_outcome.lock` 不存在。
held-out 從未開啟、E1 沒有任何結果 —— 沒有可以被回頭迎合的數字。
這是預註冊修訂。

**其四，timing 的 baseline 錯了。** `timing_provenance.json` 原本完全沒有
收錄 dataset 自帶的 `interval_ms = 82.00001312`（rank-1），
卻以偏移測試副產物 CSV 的 0.0624 s 當 baseline，於是報告把
rank-1 證據寫成「偏離 31%」。三者的正確定位是：

| 值 | 定位 |
|---|---|
| **82.00001312 ms** | **rank-1 dataset provenance**，baseline |
| 0.0624 s | 另一次採集（偏移測試副產物）的實測值，**不描述這 560 筆** |
| 0.02 s | 後期 deployment 迴圈的 sleep 設定值 |

**驗證**：
```
py -3.10 -m pcmef.cli provenance resolve-sigma \
    --paired-source data/raw_real/edge_impulse_export --export-decimals 4
py -3.10 -m pcmef.cli audit e1-gates
py -3.10 -m pytest tests/provenance tests/audit tests/unit/test_amendments.py -q
```
結果：四個 facet 如上表；`E1-G08 PASS  channel=CONFIRMED scale=/65536.0
register=CONFLICT [v2/AMD-001]`；`canonical/valid/e1_eligible` 維持 560/560/560。

**其五（v2 補強），證據來源與推導方式必須分開表示。** 初版把
`numeric_scale` 記成 `evidence_rank=1`，讀起來像「rank-1 dataset 直接寫了
/65536」—— 但 dataset **沒有任何欄位**寫著這個數字，它是對 280,000 列
做量化殘差分析推得的。三個欄位因此拆開，各自回答不同問題：

| 欄位 | 回答 | numeric_scale 的值 |
|---|---|---|
| `source_evidence_rank` | 證據出自哪裡 | **1**（raw dataset） |
| `derivation` | 怎麼從那份證據得到結論 | **quantization_residual_test** |
| `conclusion_status` | 結論的強度 | **CONFIRMED** |

`channel_semantics` 同理為 `range_correlation_exclusion_test`；
取得採集程式碼後的 `register_address` 會是
`source_evidence_rank=2` + `derivation=source_code_citation`。
舊格式（只有 `status` / `evidence_rank`）會被 G08 判 FAIL 並要求重產。

**其六（v2 補強），formal run 必須能證明自己依哪一版判準通過。**
amendment 進了版控，不代表某次 run 知道自己用了哪一版。因此
`AMENDMENT_PROVENANCE_KEYS`（`amendment_id` / `amendment_payload_hash` /
`g08_contract_version`）同時成為：

- `sigma_resolution.json` 的 `protocol_amendment` 區塊（G08 會驗雜湊是否與
  `freeze/amendments/` 的實際記錄相符，不符即 FAIL）
- `e1_scientific_rule.lock` 與 `formal_config.lock` 的必要 key
  —— 沒帶追溯就凍不了

`amendment_provenance()` 的雜湊一律**當下重算**而非從常數複製：
記錄被改過時它會拋錯，而不是安靜寫出一個過期雜湊。

**維護邊界**：
- 不得因 `register_address` 為 CONFLICT/UNKNOWN 而判定 dataset 無效，
  或把 `e1_eligible_recordings` 歸零。
- 不得把 `source_evidence_rank`、`derivation`、`conclusion_status`
  合併回單一欄位。合併就會再次出現「rank-1 說了算」的誤讀。
- 不得從 `e1_scientific_rule` / `formal_config` 的 required_keys
  移除 amendment 追溯。
- 不得將任一 register 位址宣告為 CONFIRMED，除非取得 rank-2 採集程式碼；
  取得後須依本條與 NOTE-010 複核，並開立新的 amendment。
- 不得把 `RECONSTRUCTED` 加進 `PROVENANCE_GATE_SATISFYING`。
  要放寬必須是另一次明示的 amendment，不能改一個常數就過。
- 不得覆寫已凍結的 amendment；判準改兩次就要有兩份記錄。
- G08 仍必須有能力 FAIL。
  `tests/audit/test_e1_gates.py` 的 `test_g08_fails_when_*` 系列守住這一條 ——
  沒有它們，這次修訂就只是把 gate 變成橡皮圖章。

相關：[NOTE-010]、[NOTE-001]、[NOTE-005]、[NOTE-022]

---

## NOTE-027 瓶壁必須是 roughdielectric，時間窗前緣餘裕以 bin 數表示

**決策日期**：2026-08-27

**適用範圍**：`pcmef/simulation/mitsuba_adapter.py` 的 `bottle_wall` BSDF 與
`_BOTTLE_SURFACE_ALPHA`；`pcmef/simulation/mitransient_adapter.py` 的
`default_binning()`、`_LEADING_MARGIN_BINS` 與 `bounce_budget` 預設值。

**決策**：

1. 瓶壁 BSDF 由 `dielectric` 改為 **`roughdielectric`**（GGX，
   `_BOTTLE_SURFACE_ALPHA = 0.02`）。此值不得為 0。
2. 時間窗前緣餘裕改以 **bin 數**（`_LEADING_MARGIN_BINS = 8`）表示，
   不再用 `shortest * 0.9` 這種相對比例。
3. `bounce_budget` 預設由 **7.0 改為 3.0**。

**原因**：

**其一，delta 光源配 delta BSDF 是採樣不到的路徑。** NOTE-026 把光源改成
共置的 `spot`（delta 光源）之後，瓶壁若是完全光滑的 `dielectric`（delta BSDF），
兩者之間的鏡面路徑在標準 path tracing 中機率為零 —— 這是經典的 SDS 問題。
於是瓶子**結構上完全不回光**，不是回得少。實測證據：

| 量測 | 值 |
|---|---|
| 有能量的像素 | 234 / 4096（**5.7%**），全部在畫面最左與最右兩條窄邊 |
| 中央 16×16（瓶身正中）能量佔比 | **0.02%** |
| 四類 distance | 全部 **261.28 mm**（= 背景板），與瓶內容物無關 |

瓶身對相機張開 `asin(28.5/78.5) = 21.3°`，相機半視角 22.5°，因此只在左右各留
一條 21.3°–22.5° 的窄縫看得到背景板；上下沒有窄縫，因為圓柱高 114 mm
而 50 mm 處的可見半高只有 20.7 mm，垂直方向永遠被瓶身擋住。
唯一回到感測器的能量就是那兩條窄縫後方的背景板 —— 四類同值的原因。

改成 `roughdielectric` 後總能量由 325 / 294 / 283 / 298 變成
**37,248 / 346,095 / 119,154 / 56,049**（Empty / Water / Bubbly / Misty），
distance 恢復隨內容物變化。

粗糙度不是為了讓數字好看而加的：真實 PET／玻璃瓶本來就有微觀表面粗糙度，
理想鏡面才是不真實的假設。**但 0.02 這個值尚未校準**，真值須由 E1
calibration 決定。

**其二，相對比例的前緣餘裕在小場景等於沒有餘裕。** 舊式 `start = shortest * 0.9`
在 shortest = 0.10 m、bin 寬 0.0137 m 時只有 **0.73 個 bin**。瓶子開始回光後
峰值就落在 bin 0，量不到左側半高點，FWHM 恆為 0，`map_sigma_like()` 直接
拒絕輸出 —— 與 NOTE-013 的窗尾截斷是同一種病，只是發生在另一端。

改以 bin 數表示後可解析且與場景尺度無關：由 `bin_width = (end - start)/N`
與 `start = shortest - m*bin_width` 得 **`bin_width = (end - shortest)/(N - m)`**，
非循環。餘裕另有上界 —— `start` 不得 ≤ 0（光還沒離開發射器）——
由同一組式子解得 **`m < shortest*N/end`**。bin 數太少時餘裕被壓縮是幾何事實，
不是可調參數，因此實際採用的 m 一律寫進 manifest 供事後核對。

**其三，`bounce_budget = 7` 是在「瓶子不回光」的舊場景上量的。**
NOTE-013 當時實測末端能量落在 1.675 m，那是 area emitter + 光滑瓶壁的結果。
新場景實測四類能量**全部終止於 OPL ≤ 0.646 m**（= shortest + 2.19×extent），
而舊窗尾 1.8465 m 讓三分之二的解析度落在永遠沒有能量的區間。
取 3.0 使窗尾為 0.8485 m，對實測末端仍有 31% 餘裕。

**驗證**：
```
export PATH="/c/Program Files/LLVM/bin:$PATH"
py -3.10 -m pcmef.cli sim smoke --config configs/simulation/smoke.yaml \
    --out outputs/rough_win_2140
py -3.10 -m pcmef.cli surrogate smoke \
    --simulation-out outputs/rough_win_2140 --out outputs/rough_win_2140_surrogate
py -3.10 -m pytest tests/simulation tests/surrogate -q
```
窗口推導在 32/64/128/256 bins 下 `start_opl` 均為正，首回波分別落在
bin 2.00 / 6.00 / 8.00 / 8.00，窗尾恆為 0.8485 m。

**尚未解決**：distance 現在會隨內容物變化，但仍是雙峰的
**45.35 mm（Empty、Misty）／285.6 mm（Water、Bubbly）**，
而真實資料為 Empty 100.91 / Water 113.87 / Bubbly 105.57 / Misty 79.51 mm ——
四類落在 79–114 mm 的連續區間，對應瓶子遠壁（107 mm）而非前緣（50 mm）。
模擬取的是全域峰值，真實 VL53L0X 取的不是。這一步尚未裁決，
**在此之前 distance 一欄不得用於任何 fidelity 主張**。

**維護邊界**：
- `_BOTTLE_SURFACE_ALPHA` 不得設為 0，會退回 delta BSDF 並復現上述
  「瓶子完全不回光」的狀態，而且**不會有任何錯誤訊息**。
- `_BOTTLE_SURFACE_ALPHA`、`_LEADING_MARGIN_BINS`、`bounce_budget`
  都是未校準的建模常數，與 NOTE-026 的 `_ROOM_LIGHT_RATIO` 等同屬
  `calibration.py` 的 placeholder 防線攔不到的缺口；
  凍結 `initial_simulation.lock` 前必須一併處理。
- 不得為了讓 distance 對上真實均值而調整這三個常數中的任何一個。

相關：[NOTE-013]、[NOTE-026]、[NOTE-005]

---

## NOTE-026 收發同軸的場景幾何，與「參與介質必須真的進積分器」

**決策日期**：2026-08-27

**適用範圍**：`pcmef/simulation/mitsuba_adapter.py` 的 `build_scene_dict()`、
`scene_path_bounds()`、`_medium_dict()`；`pcmef/simulation/mitransient_adapter.py`
的 `build_transient_scene_dict()` 積分器選擇。

**決策**：

1. **光源與相機共置（monostatic）**，型別由 `rectangle` + area emitter 改為
   `spot`（`cutoff_angle` 25°），座標與 sensor 完全相同。
2. **最短光程改為 `2.0 * sensor_to_bottle`**，不再由光源座標另算一條斜邊。
3. **場景含參與介質時，積分器改用 `transient_prbvolpath`**；無介質時維持
   `transient_path`。依場景內容自動選擇，不由呼叫端指定。
4. **另加一盞 `constant` 環境光**（`_ROOM_LIGHT_RATIO = 0.02`），與感測器
   自己的 spot 分開。

**原因**：

**其一，共置是 `optical_path_to_distance = 0.5` 的幾何前提。** SRC-SAI §10
明令不得把這個係數當旋鈕去逼近真實均值。舊場景的光源在
`(0, outer_r*4, camera_z*0.5)` —— 離軸且離相機很遠，光程是一條斜邊加一段回程，
根本不是單程距離的兩倍。在那個幾何下 `0.5` 沒有任何物理依據，
唯一能讓距離對上的方法就是去調係數，而那正是規格禁止的事。
改成共置之後，`0.5` 由幾何成立，係數不再是自由參數。

用 `spot` 而非把 rectangle 搬到相機位置，有兩個理由：rectangle 是實體幾何，
放在相機同一點會與相機視線互相遮擋；而 VL53L0X 的 VCSEL 本來就是帶發散角的
點狀照明，spot 更貼近實物。

**其二，`transient_path` 會靜默忽略 interior medium。** 這是最危險的一種錯：
算得出圖、能量數值也正常，但四類的散射差異完全不存在 —— 實測加介質前後
總能量到小數點都相同。沒有任何例外、任何警告。改用 volumetric 積分器後，
四類總能量由「幾乎相同」變成有實質差異（見驗證）。

依場景內容自動選擇而非由呼叫端指定，是因為「加了介質但忘了換積分器」
不會有任何症狀，而這個系統要比較的正是散射行為。讓兩者無法分離，
就不可能忘記。

**其三，環境光必須與感測器的光分開。** 改成 monostatic 之後場景只剩感測器
自己發光，`ambient_rate` 會恆為 0 —— 而真實 VL53L0X 的 ambient 量的正是
「不是自己打出去的光」。少了它，四特徵中的 ambient 這一欄不具意義，
E1 會在一個恆為零的欄位上比較分佈。

**驗證**：
```
export PATH="/c/Program Files/LLVM/bin:$PATH"     # drjit 執行期需要 LLVM-C.dll
py -3.10 -m pcmef.cli sim smoke --config configs/simulation/smoke.yaml \
    --out outputs/baseline_2116
py -3.10 -m pcmef.cli surrogate smoke \
    --simulation-out outputs/baseline_2116 --out outputs/baseline_2116_surrogate
```
四類總能量 **325.0 / 294.5 / 282.9 / 298.2**（Empty / Water / Bubbly / Misty），
落差約 13%。改動前的 area-emitter + `transient_path` 版本為
3908.9 / 3898.9 / 3879.0 / 3875.9，落差僅 0.8% —— 那 0.8% 是幾何與 seed 造成的，
不是散射。ambient / signal / sigma 三欄在改動後都取得類別差異。

**已知後果，尚未解決 —— distance 一欄同時失去了類別鑑別力。**
四類 distance 全部等於 **261.28 mm**（真實資料為 Empty 100.91 / Water 113.87
/ Bubbly 105.57 / Misty 79.51 mm）。根因已定位，不是係數問題：

| 事實 | 數值 |
|---|---|
| 瓶身對相機張開的半角 | `asin(28.5/78.5)` = **21.3°** |
| 相機半視角（fov 45°） | **22.5°** |
| 有能量的像素 | **234 / 4096（5.7%）**，全部集中在畫面最左與最右兩條窄邊 |
| 中央 16×16（瓶身正中）能量佔比 | **0.02%** |
| 峰值所在 | bin 31，distance 261.13 mm ≈ 背景板（幾何預期 249.5 mm） |

瓶身幾乎填滿整個視野，只在左右各留一條 21.3°–22.5° 的窄縫看得到背景板；
而瓶壁是**完全光滑的 `dielectric`**，在共置照明下鏡面反射一律偏離光源，
除了正射入射那一點之外不回光。於是唯一回到感測器的能量來自那兩條窄縫後方的
背景板，其距離與瓶內裝什麼無關 —— 四類同值。

上下兩條邊沒有能量，因為圓柱高 114 mm，在 50 mm 距離的可見半高只有 20.7 mm，
垂直方向永遠被瓶身擋住。只有左右看得到瓶身輪廓外。

修法屬於場景與 BSDF 的建模決策（候選：`roughdielectric` 表面粗糙度、
背景板配置、感測器 FOV 加權），**尚未裁決**，另立 NOTE 記錄。
在此之前 distance 一欄不得用於任何 fidelity 主張。

**維護邊界**：
- 不得為了讓 distance 對上真實均值而調整 `optical_path_to_distance`
  或搬動背景板（SRC-SAI §10 禁止做法）。本條目的整個重點就是讓那個係數
  由幾何決定而非由擬合決定。
- `_ROOM_LIGHT_RATIO`、`_SIGMA_T_REFERENCE_PER_M`、`_ALBEDO_BY_PRESET`
  全部是**未校準的建模常數**，真值須由 E1 calibration 產出。
  它們不在 `calibration.py` 的 placeholder 清單內，因此 formal 模式的
  自動防線攔不到 —— 這是已知缺口，凍結 `initial_simulation.lock` 前必須處理。
- 積分器不得改回由呼叫端指定。

相關：[NOTE-005]、[NOTE-013]、[NOTE-012]

---

## NOTE-025 Console 只探索不凍結，即時輸出以檔案為單一真相

**決策日期**：2026-08-27

**適用範圍**：`pcmef/console/{runner,results,routes}.py`；
`pcmef/admin/templates/console*.html`；`pcmef/admin/app.py` 的 `_register_console()`。

**決策**：
1. Console 只跑**探索性**指令（`sim_smoke` / `surrogate_smoke` / `audit_gates`），
   `_assert_not_formal()` 硬性拒絕任何含 `formal` 的參數。
2. 子行程的輸出**逐行寫入檔案**，網頁以位元組位移增量讀取，
   再以 Server-Sent Events 推送。
3. 每次執行把**完整的 scenario.yaml 與指令**存進該 run 的目錄。
4. 圖表由伺服器端產生 SVG，不引入繪圖相依。
5. Console 與 LLM Setup 共用同一個 Flask app 與同一套 CSRF/權杖檢查。

**原因**：

**其一，UI 的界線與 Part VI 完全相同。** §208 與 §52 結語規定 formal run
一律無 UI、走 CLI。Console 讓人按一下就跑模擬，但它產生的是探索用產物，
不是 formal identity —— 頁面上也明寫這件事。`_assert_not_formal()` 掃的是
**所有含 formal 的鍵**而不是某個特定參數名，因為繞過的方式通常是換個名字。

**其二，log 落盤而非留在記憶體，是為了三件事同時成立**：重新整理頁面後
還看得到、多個分頁能同時看同一次執行、容器重啟後證據還在。
以位元組位移增量推送而非每秒重傳整份 log：一次高品質模擬的輸出可以到數 MB，
重傳會讓瀏覽器與伺服器一起被自己的輸出拖垮。

**其三，存完整設定而非只存幾個數字。** 日後看到一張 transient 曲線時，
「它是用什麼參數跑出來的」必須能完整回答。存 `scenario.yaml` 全文
（含 placeholder 標記）比存 `{"spp": 16}` 可靠 —— 後者要靠拼湊，
而拼湊會在預設值改動後失真。

**其四，SVG 而非 matplotlib。** FR-020 要的 thesis-ready figure 由 CSV
另行產生；console 的圖是給人看趨勢的。伺服器端 SVG 足夠、不新增相依，
也維持首頁零 script。

**minimal JS 的量化定義**：§42 允許 minimal JavaScript、禁的是 React/Vue
這類大型前端依賴。本系統的具體落點是 —— 首頁 `document.scripts.length === 0`
（進階摺疊用原生 `<details>`），執行頁**恰好一段內嵌腳本、零外部來源**，
內容是 `EventSource` 接收與自動捲動。`test_run_page_uses_exactly_one_inline_script`
把這個定義釘住。

**實作中被測試抓到的兩個 falsy 吸收缺陷**：
- `classes or CLASS_ORDER` 讓「四個核取方塊全部取消」靜默變成「跑全部四類」，
  與使用者按下去的意思正好相反。
- `spp or preset["spp"]` 讓輸入的 `0` 靜默變成 preset 值。

兩者都是 NOTE-005 那條「falsy 被 `or` 悄悄吸收」的同一個病灶，
只是這次出現在 UI 參數而不是 config。已改為顯式區分「沒給」與「給了」。

**視覺缺陷**：`results.py` 的調色盤用 `var(--chart-N)`，但 `admin.css`
從未定義這幾個變數，於是 `stroke` 解析成 `none` —— **曲線畫得出來但完全透明**，
畫面上只看得到圖例，看起來像「沒有資料」。這類缺陷不會有任何測試失敗，
只有實際打開瀏覽器才看得到；已補上四個色階並在 CSS 註明理由。

**驗證**：
```
py -3.10 -m pytest tests/console -v
py -3.10 -m pcmef.cli admin serve            # http://127.0.0.1:8787/console
docker compose up console                    # http://127.0.0.1:8801
```
實機驗收：在瀏覽器按下「開始模擬」，log 逐行出現、結束時徽章由 running
轉 succeeded、重新整理後四條 transient 曲線與能量長條圖正確呈現；
四類能量 3908.9 / 3898.9 / 3879.0 / 3875.9 與 CLI、Docker 三處完全一致。

**維護邊界**：
- 不得在 console 新增任何會寫 lock 或帶 `--formal` 的端點。
- 不得把 log 用 `innerHTML` 附加。伺服器輸出含使用者可控的檔名與錯誤字串，
  必須以 `createTextNode` 附加，否則就是 XSS；
  `test_the_live_log_is_appended_as_text_not_html` 守住這一條。
- Docker 的主機埠預設 **8801** 而非 8787：8787 是 `pcmef admin serve` 的
  預設埠，本機直接跑 CLI 時就會佔住它，接著 `docker compose up` 會撞埠。
  兩者用不同埠才能同時開著互相對照。

相關：[NOTE-005]、[NOTE-012]、[NOTE-019]、[NOTE-022]

---

## NOTE-024 E1 的三層分工：度量、成對重抽、判定各自獨立

**決策日期**：2026-08-27

**適用範圍**：`pcmef/stats/metrics.py`、`pcmef/stats/bootstrap.py`、
`pcmef/experiments/e1.py`、`pcmef/experiments/e1_outcome.py`。

**決策**：E1 拆成三個互不相依的層，且每層都有一條它自己擋得住的事：

| 層 | 擋什麼 |
|---|---|
| `stats.metrics` | 不同物理單位的 raw W1 不得相加；s_f 退化時 BLOCK 而非套預設除數 |
| `stats.bootstrap` | 兩個候選必須共用同一組重抽索引；推論單位是 recording/scenario |
| `experiments.e1` | 沒鎖規則不准碰 held-out；兩候選必須跑在同一組隨機實現上 |
| `experiments.e1_outcome` | 規則只能來自已凍結的 lock；判定不可重來 |

**原因**：

**其一，跨特徵平均的單位問題不是形式主義。** distance 的 W1 單位是 mm、
signal/ambient 是 MCPS、sigma_like 無單位。實測一個對照：
distance 差 5 mm（s_f=10）與 signal 差 0.005 MCPS（s_f=0.010），
raw W1 相差 **1000 倍**，但兩者代表的失真程度其實相同（NW 都是 0.5）。
直接平均 raw W1 會讓 distance 完全主導結果，而 signal 的改善與退步都看不見。
§11 因此明訂跨特徵一律先轉 NW。

**其二，成對重抽不是精緻化，它會改變結論。** 以 60 個 scenario 的合成對照
實測：兩候選共用同一組重抽時，95% CI 寬度比各自獨立重抽窄 **5 倍以上**，
而且**下界的符號不同** —— 配對後下界為正（判定有改善），
獨立後下界跨越 0（判定沒有）。原因是同一個 scenario 在兩個候選下的難度本來
就相關（它們共用 base scenarios 與 seed matrix），各自重抽等於把這份相關性
丟掉，換來的是純粹的抽樣雜訊。這條對照寫成
`test_paired_resampling_is_narrower_than_independent`，因為沒有它，
把實作改成各自獨立重抽不會有任何測試失敗。

**其三，規則必須早於結果，而且只能來自 lock。**
`ScientificRule.from_lock()` 刻意不提供「直接傳三個門檻進來」的建構路徑。
若允許，就等於允許看過 CI 下界之後臨時換一組門檻，而每一組門檻單獨看都合法。
Appendix H2 的「frozen before Held-out access」靠的正是這個結構限制。

**其四，DEGRADED 不是失敗，是必須被凍結的結論。** §12.1 規定 DEGRADED
仍可繼續工程與探索，但下游 manifest 一律帶 `claim_mode="synthetic_testbed"`。
因此 `freeze_outcome()` 對兩種結果一視同仁地寫 lock —— 不凍結就等於這次
評估沒發生過，而下一次評估會以為自己是第一次。

**趨勢一致性的判定方式（實作中被測試修正）**：初版以「相對變化的絕對差
是否 ≤ 0.5」判定幅度，這是**錯的**。真實相對變化本身只有 0.1 量級時，
「合成完全沒有變化」的絕對差只有 0.1，照樣通過 —— 而那正是最該擋下的情況
（方向沒錯只是因為沒有反向）。已改為比值判定：`|syn/real − 1| ≤ tolerance`，
幅度塌到千分之一會得到比值 0.001，必然落在容差外。

**驗證**：
```
py -3.10 -m pytest tests/e1 -v
py -3.10 -m pcmef.cli e1 metrics-evidence     # 產出 E1-G06 的 tests/e1_metrics.xml
py -3.10 -m pcmef.cli audit e1-gates
```
`test_normalization_makes_two_features_comparable` 鎖住第一點；
`test_paired_resampling_is_narrower_than_independent` 鎖住第二點；
`test_rule_comes_only_from_the_lock` 與
`test_outcome_cannot_be_refrozen_with_a_different_verdict` 鎖住第三、四點。

**維護邊界**：
- 本批次完成的是**引擎**，不是可執行的 formal E1。surrogate 的九個校準常數
  仍全部是 placeholder，formal 模式會拒絕載入，因此 E1-G07/G10/G11/G12
  四個 lock 目前仍無法凍結。這不是缺陷，是 M2 校準尚未進行。
- 不得因為結果不理想就重跑 E1 final。held-out 只能開一次
  （`splits.heldout_ids` 記錄取用次數），而 `e1_outcome` 的不可覆寫性
  是第二道保險。
- `tests/e1_metrics.xml` 不進版控。證據要靠「跑一次」而不是「被提交過」；
  JUnit 報告帶機器專屬的時間與路徑，提交它只會讓 diff 充滿雜訊。

相關：[NOTE-015]、[NOTE-022]

---

## NOTE-023 LLM setup 的流程與版型對齊 roothinks LAVA setup

**決策日期**：2026-08-27

**適用範圍**：`pcmef/llm/registry.py` 的 `Lifecycle` 與 `ConnectionRow.bindable`；
`pcmef/admin/services.py` 的 `select_model` / `test_connection` /
`lock_connection` / `unlock_connection`；
`pcmef/admin/templates/llm_setup.html` 的 Connections 與 Service Binding 兩張 card；
`pcmef/cli.py` 的 `llm connection select-model / test / lock` 與 `llm binding lock`。

**決策**：連線的設定進度採 roothinks LAVA setup 的四態
`draft → fetched → connected → locked`，每一步解鎖下一個動作：

| 狀態 | 可做 | 不可做 |
|---|---|---|
| draft | Fetch、選模型 | Test、Connect |
| fetched | Test、換模型、重新 Fetch | Connect |
| connected | Connect、Test、換模型 | — |
| locked | Unlock、Delete | 換 vendor/key/模型、Fetch、Test |

並確立三條由此衍生的規則：

1. **只有 locked 的線路能被綁到 task**（LAVA 的 `僅顯示 Locked`）。
2. **一條 locked 的線路只提供它被檢查過的那一個模型。** 同一把 key 底下其他
   沒測過的模型不出現在綁定選單，也綁不上去。
3. **換模型會退回 fetched。** 先前的 Test 結果對新模型無效。

另外，`llm_task_bindings.is_locked` 是 **draft 層的確認鎖**，
與 formal 的 `llm_runtime.lock` 是兩件事。

**原因**：SRC-SAI §42 只規定了 Add Connection / Connections / Task Bindings /
Formal Snapshot 四張 card 與各自的欄位，**沒有規定操作者要照什麼順序把一條線路
設定好**。roothinks 與 rootmedicals-a 兩套系統在真實使用中收斂到同一個流程，
而使用者是同一個人 —— 讓 PC-MEF 用第三種流程，代價是每次切換系統都要重新學，
而且沒有換到任何研究上的好處。

四態本身也解決一個 §42 沒處理的問題：`llm_models` 可以有幾十個 model profile，
但一條線路實際上只用一個。沒有 `selected_model_profile_id` 與 locked 狀態時，
綁定選單會列出所有「能力恰好符合」的 profile，包含從未被實際呼叫過的那些。
LAVA 的做法是先讓操作者把一條線路收斂成一個確定的 (vendor, key, model) 三元組
並明示確認，再讓它進入可綁定池。這比逐一檢查 profile 更接近實際的心智模型。

**與 roothinks 的三處刻意差異**：

| 項目 | roothinks | PC-MEF | 理由 |
|---|---|---|---|
| Test 的內容 | 送一句話看回不回 OK | 跑 chat + structured_json + vision 三項 probe，但**只以最低要求為門檻** | §44 規定這些能力不可只信 metadata；回一句 OK 證明不了任何一項。門檻另見下方修正。 |
| 憑證遮蔽 | key 的後四碼 | HMAC 指紋 `****abcd` | §46 規定不顯示 key prefix/full value；指紋能達成同樣的辨識用途而不洩漏字元（NOTE-016）。 |
| 前端 | JS 驅動、`fetch()` 逐列更新 | server-rendered 表單，零 script | §42 明訂第一版採 server-rendered HTML + minimal JavaScript。資訊架構與狀態機完全相同，只是每個按鈕是一次 POST + redirect。 |

第三點值得展開：照抄 LAVA 的 JS 會直接違反 §42，而只抄版型不抄流程則失去
這次對齊的意義。取捨的方式是**保留狀態機與資訊架構、改變傳輸方式** ——
操作者看到的按鈕、順序與啟用/停用邏輯完全一致，差別只在每次動作會整頁刷新。
對一個本機、單人、低頻率的設定頁面，這個代價可以接受。

**驗證**：
```
py -3.10 -m pytest tests/llm_admin/test_connection_lifecycle.py -v
py -3.10 -m pytest tests/llm_admin/test_cli_llm_flow.py -v
```
`test_transitions_follow_the_declared_table` 以參數化窮舉九組轉移；
`test_only_locked_lines_are_bindable` 驗證鎖定前後的綁定選單差異；
`test_a_locked_line_offers_exactly_the_model_it_was_checked_with` 鎖住第 2 條規則；
`test_changing_the_model_drops_back_to_fetched` 鎖住第 3 條。

實機驗證（離線 stub、隔離 registry）：locked 那一列的 Fetch/Set/Test 皆為
disabled 且只剩 Unlock/Delete；draft 那一列 Fetch/Set 可用而 Test/Connect
為 disabled；已鎖定的 arbitration_agent 不顯示 dropdown 而顯示
「已鎖定，解鎖後才能更換」；`document.scripts.length === 0`。

**鎖定門檻的修正（2026-08-27 當日）**：初版把「chat + structured_json + vision
三項全過」當成鎖定條件，這是**錯的**，已修正為
`MINIMUM_BINDABLE_CAPABILITIES`（由 `TASK_REGISTRY` 推導，目前是
`{chat, structured_json}`）。

§45 是**逐 role** 的要求表，不是一張全體適用的清單：

| role | 需要 vision？ |
|---|---|
| observation_agent | 是 |
| visual_semantic_agent | 是 |
| physics_agent | **否** |
| arbitration_agent | **否** |

因此一個沒有視覺能力的純文字強模型完全可以服務 physics 與 arbitration。
把 vision 提升成線路層的鎖定門檻，會讓這種模型永遠鎖不起來，
連帶把它能勝任的兩個角色一併排除 —— 而畫面上只會顯示「Test 未通過」，
看不出被排除的其實是兩個它本來做得到的工作。

正確的分層是：**線路層只問「這條線路至少能服務某一個角色嗎」，
逐 role 的能力守門留在 bind 時**（`compatible_models` 與
`registry.set_binding` 已經在做）。vision probe 失敗現在只記錄在
`last_error` 並反映在 `servable_roles`，不阻擋 lock。

只有當一個角色都服務不了（連 chat + structured_json 都不到）才算 Test 失敗。

**維護邊界**：
- 不得允許 `draft` 直接跳到 `locked`。那會讓 Test 變成裝飾品，
  而綁定選單的可信度完全建立在「locked 代表測過」這個假設上。
- 不得把 vision 加回線路層的鎖定門檻，也不得手寫
  `MINIMUM_BINDABLE_CAPABILITIES` 的內容；它必須由 `TASK_REGISTRY` 推導，
  否則改了 §45 的能力矩陣就會留下一個對不上表格的常數。
- `is_locked`（draft 確認鎖）不得被當成 formal identity 使用。
  §52 結語的裁決是 UI 永遠不能成為繞過 freeze 的第二條設定通道；
  draft 層多一個確認鎖不違反它，但如果哪天有程式讀 `is_locked` 來決定
  formal 行為，那就違反了。formal 只能讀 `llm_runtime.lock`。
- 解鎖仍被綁定的線路一律拒絕，否則會留下綁著未鎖線路的 task。

相關：[NOTE-016]、[NOTE-018]、[NOTE-019]、[NOTE-020]

---

## NOTE-022 稽核狀態分四種，「尚未產出」不是失敗

**決策日期**：2026-08-27

**適用範圍**：`pcmef/audit/result.py` 的 `CheckStatus` 與 `AuditReport.exit_code()`；
`pcmef/audit/e1_gates.py`；`pcmef/audit/firewall.py`；
`pcmef/cli.py` 的 `audit e1-gates / heldout-firewall / real-split-policy`。

**決策**：
1. 稽核結果分成 **PASS / FAIL / NOT_PRODUCED / BLOCKED** 四種，
   **NOT_PRODUCED 不計入失敗**。
2. 不帶 `--require` 時只是盤點：只有 FAIL 會讓 exit code 非零。
   帶 `--require` 時，被指名的檢查只要不是 PASS（含 NOT_PRODUCED）就非零。
3. 稽核模組**只讀不寫**被稽核的產物，且不提供任何「修復」路徑。
4. 每個宣告的 gate 都必須有對應的檢查函式；缺一個就在啟動時拋例外。

**原因**：Batch 7 刻意排在 Batch 6 之前 —— 稽核器先於被稽核的產物存在，
就不會在事後被寫成剛好符合已產出的結果。但這個順序有個直接後果：
執行當下必然有一半的證據還不存在（G05 屬 Batch 8，G06/G07/G10/G11/G12 屬 Batch 6）。

若把「還沒產出」報成 FAIL，這個指令從第一天起就會一直是紅的。
一個永遠紅的檢查等於沒有檢查 —— 讀報告的人會學會忽略它，
而真正的 FAIL 出現時也不會有人注意到。因此四種狀態的分野不是文件上的細膩，
是這份報告能不能被當真的前提。

反過來，FAIL 在任何情況下都必須非零：它代表**已經產出**的證據自相矛盾
（例如五層計數不單調、registry 雜湊與 assignments 對不上），
那與「還沒做」完全是兩回事。

第 3 點：稽核器一旦能寫，就會有人在發現不一致時「順手修好」，
而 NOTE-014 已經寫明發現不一致的正確處置是開新 run。能修的稽核器
會變成掩蓋工具，`test_the_audit_never_writes_to_what_it_audits` 把這條釘住。

第 4 點擋的是「有宣告卻沒有檢查」的空殼 gate —— 那比沒有 gate 更糟，
因為它在報告上看起來被稽核過。

**首次執行的發現（2026-08-27）**：稽核器上線第一次跑就抓到一個真實不一致 ——
`provenance/sigma_resolution.json` 顯示 `status=UNRESOLVED`、`register=null`，
但 `configs/base.yaml` 與 STATUS.md 都宣稱 sigma 已解出 `0x18`。

追查後確認**結論是對的，證據是舊的**：NOTE-010 v2 的排除法分析當時以臨時
腳本完成，而產生 E1-G08 證據的 `provenance resolve-sigma` 指令從未實作
那條路徑 —— 它只讀彙總格式的 sigma 值（55,440 筆），既沒有呼叫
`rule_out_range_register()`，也沒有傳 `export_decimals`。

修正是把兩者都接進 CLI（`--paired-source` 與 `--export-decimals`），
重跑後獨立重現了 NOTE-010 v2 記載的數字：280,000 列、`/128` 殘差 0.4992
遠超容差 0.0064、register 解出 `0x18`。E1-G08 因此由 FAIL 轉為 PASS。

**排除法需要逐 recording 的來源**：`sigma × 65536` 是否等於同一筆的 `distance`
這個檢驗，需要**同一列**的兩個 metric 配對。彙總格式已把時間軸摺成窗口統計量，
兩個 metric 的列不再對應同一個時刻，因此只有 Edge Impulse export 做得到。
這也是 `EdgeImpulseAdapter.stacked_values()` 存在的理由。

**驗證**：
```
py -3.10 -m pytest tests/audit -v
py -3.10 -m pcmef.cli audit e1-gates
py -3.10 -m pcmef.cli audit heldout-firewall
py -3.10 -m pcmef.cli audit real-split-policy
```
`tests/audit/test_e1_gates.py` 對每個檢查都有對應的「破壞後應該 FAIL」案例；
一個永遠回 PASS 的稽核器與沒有稽核器等價，因此只測 PASS 路徑不算數。

**維護邊界**：
- 不得把 NOT_PRODUCED 併進 FAIL，也不得反過來讓 FAIL 被當成「還沒做」。
- 不得為了讓某個 gate 變綠而放寬內容檢查。gate 的用途是擋下 E1 開跑，
  放寬它等於取消這道關卡。
- `--require G01:G12` 是 `experiment e1 --formal` 之前的最後一道閘門
  （§32 CLI 契約），它現在會因為六個 NOT_PRODUCED 而擋下 —— **那是正確的**，
  E1 確實還不能跑。

相關：[NOTE-010]、[NOTE-011]、[NOTE-014]

---

## NOTE-021 Agent cache key 刻意與 case 身分和 checkpoint pair 無關

**決策日期**：2026-08-27

**適用範圍**：`pcmef/agents/cache.py` 的 `AgentCacheKey`、`PAIR_SPECIFIC_FIELDS`
與 `assert_pair_independent()`；未來 `experiments/e2_formal.py` 消費 s_A 之處。

**決策**：
1. cache key 只由 §48 的七個要素組成（evidence_hash、representation_mode、
   provider_model_id、provider_revision、prompt_hashes、schema_hash、
   runtime_config_hash），**不含** case id、opaque_case_id 或 training_pair_id。
2. 寫入快取的 payload 遞迴檢查，含 p_T/p_V/r_T/r_V/D/U/Q/g/F/predicted_class/
   training_pair_id 任一者即拒絕。
3. `resolve(key, producer)` 是唯一會呼叫 provider 的路徑，且只在未命中時呼叫。

**原因**：SRC-SAI §48 指出 Formal E2 有多組 frozen checkpoint pair，而 Agent 路徑
只看同一 scenario 的 raw evidence 加 frozen Agent config，不依賴 p_T/p_V 或
training_pair_id。若把 pair 身分放進 key，同一份 evidence 會在每組 pair 各問一次，
s_A 因此變成 pair 的函數 —— 而 PC-MEF 的 F=(1-g)p_rel+g·s_A 之所以能把
G5 相對 G4 的差異歸因到 Agent，前提正是 s_A 在各 pair 之間相同。這不只是省錢，
是可比較性本身。

反向的錯誤同樣嚴重：把 pair 專屬的量（Appendix J2 右欄）寫進共用快取，
第二組 pair 會讀到第一組的數字，而且**不會有任何症狀** ——
數值都在合理範圍、沒有 NaN、沒有缺值。因此以遞迴欄位檢查在寫入當下攔截，
而不是靠日後對帳。

把「查快取」與「有能力發問」綁在同一個方法上，是為了讓 LLM-CACHE-01 與
LLM-RESUME-01 成為結構性保證而非紀律要求：呼叫端拿不到繞過快取的入口。

**驗證**：
```
py -3.10 -m pytest tests/cache/test_agent_cache.py -v
```
涵蓋三組 pair 只 1 次 provider call、七個要素逐一變動皆 miss、
resume 時 producer 被呼叫即失敗、pair 專屬欄位被拒、半套目錄不算命中。

**呼叫次數的正確說法**：一個 scenario 有 **4 個 logical Agent invocations**
（Observation → Physics ∥ Visual-Semantic → Arbitration）。**實際的 HTTP/API
call 次數不等於 4**，三種情況都會偏離：

| 情況 | 實際呼叫數 |
|---|---|
| 首次執行且四步都一次成功 | 4 |
| 遇 timeout 或 schema invalid 而進 frozen retry policy | **> 4** |
| content-addressed cache 命中 | **0**（完全不呼叫 provider） |

因此描述成本或重現性時要說「4 個 logical invocations」，
不要說「4 次 provider call」—— 後者在 retry 與 resume 兩種情況下都是錯的，
而 LLM-RESUME-01 要驗的正是「resume 時新增呼叫數為 0」。

跨 checkpoint pairs 共用的是 Observation / Physics / Visual / Arbitration 的
raw+validated artifacts 與 s_A；`p_T / p_V / r_T / r_V / D / U / Q / g / F`
一律逐 pair 重算（Appendix J2）。

**維護邊界**：`combine_hashes` 的參數順序即 §48 的串接順序，不得重排；
兩個不同的 key 若只是欄位換位置就得到相同雜湊，會造成 cache 誤命中。

相關：[NOTE-004]、[NOTE-020]

---

## NOTE-020 llm snapshot 產生 lock candidate，凍結是另一個動作

**決策日期**：2026-08-27

**適用範圍**：`pcmef/llm/snapshot.py` 全檔；`pcmef/cli.py` 的
`cmd_llm_snapshot()`；`pcmef/admin/services.py` 的 `snapshot_view()`。

**決策**：`pcmef llm snapshot` 解析 draft binding 成 lock candidate、算出
candidate hash、寫出 `outputs/llm/runtime_snapshot_<hash>.json`，並列出所有
尚未滿足的前提。只有加上 `--freeze` 且 blocking 清單為空時，才寫入
`freeze/llm_runtime.lock.json`。仍有 blocking 時 `--freeze` 以 exit code 2 中斷。

**原因**：SRC-SAI 兩處對本指令的描述不一致。§32 的 CLI 草稿寫
`pcmef llm snapshot --out freeze/llm_runtime.lock.json`，看起來是直接產 lock；
§50 Admin API Contract 則明訂這個動作是「resolve draft -> lock candidate；
returns hash；does not auto-start Formal」。採 §50，理由有二。

其一，§52 結語的核心裁決是「UI 永遠不能成為繞過 freeze 的第二條設定通道」。
這條規則的實質內容是「freeze 需要前提齊備」，而不是「freeze 只能從 CLI 按」——
若 CLI 可以在前提未齊時直接產 lock，那條紅線就只是換個地方被跨過去。

其二，實際跑過就會看到前提真的不齊：目前 blocking 清單有十項，其中
`agents.representation_mode` 與 `agents.retry.max_attempts` 是教授未裁決值
（NOTE-005），四份 agent prompt 屬 M6 尚未撰寫。一個會在這種狀態下寫出 lock
的指令，等於把「還沒決定」凍結成「已經決定」。

candidate hash 對未決項以 `{"__required__": key}` 顯式標記入雜湊，而非略過。
這與 `ResolvedConfig.config_hash()` 的處理一致：一份帶缺口的快照，
不應該和補齊之後的快照得到相同雜湊。

**驗證**：
```
py -3.10 -m pytest tests/llm_admin/test_snapshot.py -v
py -3.10 -m pytest tests/llm_admin/test_cli_llm_flow.py -k snapshot -v
```
`test_shipped_config_blocks_the_snapshot` 鎖住「shipped config 仍 formal-blocking」；
`test_snapshot_freeze_exits_two_when_blocked` 鎖住 `--freeze` 的拒絕行為；
`test_freezing_cannot_skip_the_prerequisite_locks` 鎖住 §23 的相依鏈。

**維護邊界**：
- `llm_runtime` 的前置 lock 是 `agent_schema`，其上游一路到 `real_split_policy`。
  因此在 E1 與 M3 完成之前，`llm_runtime.lock` **本來就不可能**被凍結。
  這不是缺陷，是 state machine 的正確結果；Part VI 的完成度不以能否凍結衡量。
- `resolve_formal_agent_binding()` 刻意不接受 registry 參數。Appendix J1 把
  `db.query("SELECT * FROM llm_task_bindings ...")` 明列為 formal 禁止行為，
  而沒有可用的 DB 控制代碼比一句註解可靠。

相關：[NOTE-005]、[NOTE-019]、[NOTE-021]

---

## NOTE-019 Admin 頁面的網路邊界與寫入邊界

**決策日期**：2026-08-27

**適用範圍**：`pcmef/admin/auth.py`、`pcmef/admin/app.py`、
`pcmef/admin/routes_llm.py`、`pcmef/admin/services.py`；
`pcmef/cli.py` 的 `cmd_llm_connection_add()` 與 `cmd_admin_serve()`。

**決策**：
1. 預設 bind `127.0.0.1`。非 loopback 位址必須同時具備 `PCMEF_ADMIN_TOKEN`
   與 `PCMEF_ADMIN_TLS=1`，否則 `assert_network_policy()` 直接拒絕啟動。
2. 無法解析成 IP 的主機名一律視為**非**本機。
3. 所有寫入端點強制 CSRF；帶有效 admin token 標頭的請求例外。
4. UI 與 CLI 共用同一個 `AdminService`，UI 不得自行操作 registry。
5. **CLI 的 `llm connection add` 不接受 API key 明文**，只收 `--secret-ref`。
6. UI 的 Bind 由 `bind_enabled` 旗標控制，預設關閉。

**原因**：這個頁面可以寫入 provider 憑證，因此曝露範圍不是樣式問題。§46 的
Fail Behavior 明訂「不符合即拒絕非-loopback 啟動」，而非警告後照樣啟動。

主機名判定選擇「未知即非本機」而非相反：反過來寫的話，一個打錯的主機名
（例如 `127.0.0.l`，尾字是小寫 L）會被當成本機而繞過整套檢查。

CLI 不收明文 key 是與 UI 刻意不對稱的設計。命令列參數會留在 shell history
與作業系統的 process list，兩者都不是本系統能遮蔽的範圍；而 UI 的密碼欄位
提交後立刻轉成 secret_ref，明文只存在於一次 request 的生命週期內。
需要輸入 key 本身時走 UI，需要無人值守時走 `env:` 參考。

UI 與 CLI 共用服務層而非各寫一份：兩套寫入路徑必然行為分歧，
而分歧的那一套通常是忘了做能力檢查的那一套。

`bind_enabled` 預設關閉且在畫面上顯示原因，而不是直接把按鈕拿掉 ——
§52 步驟 5 要求先讓 CLI 走完全流程再接 UI Bind，把這個狀態顯示出來，
比讓功能靜靜消失更容易在交接時被理解。

**驗證**：
```
py -3.10 -m pytest tests/llm_admin/test_admin_security.py -v
py -3.10 -m pytest tests/llm_admin/test_admin_page.py -v
py -3.10 -m pytest tests/llm_admin/test_admin_services.py -v
```
`test_serve_refuses_non_loopback_without_controls` 鎖住啟動閘門；
`test_csrf_is_required_on_every_write_endpoint` 逐端點驗證；
`test_connection_add_takes_no_plaintext_key_option` 鎖住 CLI 的不對稱設計。

**維護邊界**：session 簽章金鑰每個行程重新產生，不得持久化。代價只是重啟後
需要重新載入頁面取得 CSRF 權杖，而一把寫死或存檔的金鑰是永久性的風險。

相關：[NOTE-016]、[NOTE-020]

---

## NOTE-018 verified 能力由 probe log 推導，且 probe 失敗不牽連 connection

**決策日期**：2026-08-27

**適用範圍**：`pcmef/llm/registry.py` 的 `record_verification()`、
`_derive_verified()` 與 `ConnectionRow.bindable`；`pcmef/llm/capabilities.py`；
`pcmef/llm/verification.py`；`pcmef/admin/services.py` 的 `fetch_models()`。

**決策**：
1. `llm_models.verified_caps_json` 只由 `record_verification()` 依 probe log 推導，
   並與 log 寫入同一個交易；不提供手動設定的路徑。
2. 判定採「每個能力**最近一次** probe 的結果」，不是「曾經成功過」。
3. **單一 capability probe 失敗不改變 connection 狀態。** 連線層級的故障只由
   `fetch_models()` 的 `list_models` 失敗標記為 `error`。
4. task binding 只認 probe-verified 能力，provider metadata 宣稱的
   declared 能力僅作提示。

**原因**：SRC-SAI §44 明訂「能力狀態必須分成 provider-declared 與 probe-verified；
Task Binding 只允許使用 verified capability」。把 verified 做成可手填的欄位，
等於留一條「宣稱驗證過但沒有 log 佐證」的路。

採最近一次而非歷史最佳：模型被下架、方案降級或配額用盡時 probe 會開始失敗，
若沿用歷史成功結果，binding 會繼續指著一個已經不能用的能力，
而問題會在 formal run 中段才爆。

第 3 點是實作過程中由驗收測試逼出來的修正。初版讓任何失敗的 probe 把
connection 標成 `degraded`，於是 LLM-UI-03（structured-json probe FAIL 時
Bind 應被拒）確實被拒了，但**拒絕的理由是連線狀態而不是能力不足**。
理由錯了就是錯的：操作者會照著訊息去檢查憑證與端點，而真正的問題是
這個模型不支援結構化輸出。一個模型缺某項能力，與憑證或端點故障，
是兩個不同的事實，不該共用同一個狀態欄位。

**驗證**：
```
py -3.10 -m pytest tests/llm_admin/test_registry.py -v
py -3.10 -m pytest tests/llm_admin/test_capabilities.py -v
py -3.10 -m pytest tests/llm_admin/test_verification.py -v
```
`test_verified_capabilities_follow_the_latest_probe` 鎖住第 2 點；
`test_a_failed_probe_does_not_condemn_the_whole_connection` 與
`test_fetch_models_marks_the_connection_error_when_the_provider_fails`
成對鎖住第 3 點的兩半。

**維護邊界**：`CapabilityVerifier.verify()` 不得新增「自訂 probe 內容」的參數。
§44 要求 verification payload 最小化且不得使用真實 formal evidence；
沒有這個參數，就沒有人能在趕時間時順手拿一筆 case 來測。

相關：[NOTE-017]、[NOTE-019]

---

## NOTE-017 Provider 錯誤訊息不得夾帶 request URL，且離線 stub 排除於 formal

**決策日期**：2026-08-27

**適用範圍**：`pcmef/agents/provider.py` 全檔，特別是
`HTTPProviderAdapter._request()`、`FORMAL_ELIGIBLE_PROVIDERS` 與
`StubOfflineAdapter`；`pcmef/core/logging_setup.py` 的 `redact()`。

**決策**：
1. 不呼叫 `raise_for_status()`，改為自行檢查狀態碼。
2. **例外訊息一律不含 request URL**，只帶 provider 名稱、HTTP 狀態碼與
   已遮蔽的回應片段；連線層例外只保留例外**類別名稱**。
3. provider 回應片段在組成訊息時就以 `redact()` 清洗，不等 log filter。
4. `StubOfflineAdapter` 顯式登記為 provider，但**不在** `FORMAL_ELIGIBLE_PROVIDERS`。

**原因**：第 1 點承自 [NOTE-007]。第 2 點是它的延伸：NOTE-007 的做法是
「把 key 從 query string 移到 header，所以 URL 進訊息也無妨」，但那讓安全性
依賴於兩處程式**同時**維持正確。只要日後有人為了除錯把 key 加回 query，
訊息就會再度外洩，而那次改動看起來完全無害。訊息裡本來就不需要 URL —— 
provider 名稱加狀態碼足以定位問題 —— 因此直接移除這個依賴。

第 3 點的必要性在於落盤時機：provider 的回應內容由對方決定，可能原樣回吐
我們送出的憑證，而該訊息會被寫進 `llm_verification_logs.error_sanitized`，
屬於 LLM-SEC-01 的掃描範圍。log filter 只在寫 log 時生效，救不了寫進 DB 的那份。

第 4 點讓「離線也能把整條 admin 流程走完」與「假 provider 絕不進 formal」
同時成立。沒有 stub，離線環境無法驗證 admin 流程，測試只能改用 mock 去繞過
真實程式路徑 —— 那會讓被測的東西不是被用的東西。有了 stub 而不設邊界，
則會有一天在 `llm_runtime.lock` 裡看到 `stub_offline`。兩者都不可接受。

**驗證**：
```
py -3.10 -m pytest tests/llm_admin/test_provider_contract.py -v
```
`test_no_module_calls_raise_for_status` 以 AST 掃描全樹（比對呼叫節點而非
字串，否則檔頭寫下這條禁令本身就會觸發它）；
`test_http_error_message_contains_neither_url_nor_key` 與
`test_error_body_excerpt_is_redacted` 分別鎖住第 2、3 點；
`test_stub_provider_is_excluded_from_formal` 鎖住第 4 點。

**維護邊界**：`ProbeResult.artifact_hash()` 刻意排除 `probed_at` 與 `latency_ms`。
同一個結論在不同時間 probe 應得到相同 hash，否則
`capability_probe_artifact_hashes` 每跑一次就變，`llm_runtime.lock` 會被迫
跟著變而失去「identity 沒變」的意義。

相關：[NOTE-007]、[NOTE-018]

---

## NOTE-016 secret 只以參考流動，且缺加密後端時 fail-closed

**決策日期**：2026-08-27

**適用範圍**：`pcmef/secrets/vault.py` 與 `pcmef/secrets/crypto.py` 全檔；
`pcmef/llm/registry.py` 的 `_check_secret_ref()`；`.gitignore` 第 6 行。

**決策**：
1. 系統各層一律只傳遞 `env:<VARNAME>` / `vault:<uuid>` / `session:<token>`
   三種參考；解析出明文只發生在 `SecretVault.resolve()`，且該處同時把值
   註冊進 log 遮蔽清單。
2. 本機加密 vault 使用 `cryptography` 的 Fernet。**缺這個套件時拒絕保存
   持久化 secret**，不退回自製加密。
3. UI 顯示的指紋為 `HMAC-SHA256(vault 專屬 salt, secret)` 前四個 hex，
   不是 key 的任何字元。
4. `rotate()` 保持 vault entry 的 uuid 不變，只換密文並遞增 `secret_version`。
5. `session:` 參考只存在於行程記憶體，且被 registry 與 snapshot 明文拒絕。

**原因**：第 2 點是本則的核心取捨。缺 `cryptography` 時有兩條路：
以標準函式庫自己組一個 HMAC-CTR 加 encrypt-then-MAC 的構造，或直接拒絕。
選後者，因為前者的成本不對稱 —— 自製構造即使當下寫對，也沒有第二個人審查過，
而它保護的是可以直接花錢的 API 憑證；而拒絕的代價只是一行
`pip install`，或改用 `env:` 讓 key 留在環境變數裡完全不落盤。
「無 master key 時禁止 save persistent secret，可允許 ephemeral session verify」
本來就是 §46 寫明的行為，把「無加密後端」歸入同一條路徑是自然的延伸。

第 3 點：§46 只要求「不顯示 key prefix/full value」，因此常見的「顯示後四碼」
在字面上並不違規。仍然不採用，是因為指紋的用途只是「分辨裝的是哪一把」，
而雜湊完全能做到，多洩漏四個字元換不到任何東西。salt 是必要的 ——
沒有 salt 的四字元雜湊會變成離線驗證預言機：任何人拿一把候選 key
算一次雜湊就能確認是否命中。

第 4 點對應 §46 的 Secret rotation 規則：只換 credential 而
provider/model/base_url/runtime identity 不變時，不必 invalidate scientific lock。
而 lock 內存的正是這個 uuid —— 換掉它會讓一次單純的換 key 看起來像
binding identity 變更，逼出一次不必要的重新 freeze。

**驗證**：
```
py -3.10 -m pytest tests/llm_admin/test_secret_vault.py -v
py -3.10 -m pytest tests/secret/test_global_secret_scan.py -v
```
`test_fingerprint_has_the_masked_shape_and_leaks_no_key_material` 斷言指紋
不是 key 的任何子字串；`test_llm_sec_01_no_artifact_contains_the_secret`
在跑完整條 admin 流程後，逐位元組掃描每一個落盤檔案的任意 8 字元片段。

**維護邊界**：`.gitignore` 的 vault 排除規則必須寫成 `/secrets/` 而非
`secrets/`。後者會連 `pcmef/secrets/`（本抽象層的原始碼）一起吞掉，
而那份程式碼必須進版控 —— 這個坑在 2026-08-27 已經踩過一次。

相關：[NOTE-007]、[NOTE-017]、[NOTE-019]

---

## NOTE-015 兩組 bootstrap 參數的教授核定

**決策日期**：2026-08-26

**適用範圍**：`configs/base.yaml` 的 `e1.scientific_rule.bootstrap_*`
（凍結於 `e1_scientific_rule.lock`）與 `statistics.bootstrap_*`
（凍結於 `statistics_config.lock`）；所有讀取這兩組值計算信賴區間的模組。

**決策**：以下四項由教授於 2026-08-26 核定。

| 參數 | 值 | 凍結位置 |
|---|---:|---|
| `e1.scientific_rule.bootstrap_replicates` | 10000 | `e1_scientific_rule.lock` |
| `e1.scientific_rule.bootstrap_seed` | 20260826 | `e1_scientific_rule.lock` |
| `statistics.bootstrap_replicates` | 10000 | `statistics_config.lock` |
| `statistics.bootstrap_seed` | 20260827 | `statistics_config.lock` |

核定時 held-out access_count 為 0，且尚無任何 E1 結果產出。

**原因**：E1 的 PASS 規則是 `macro_mean_delta_ci_lower_bound_gt: 0.0`，
亦即 PASS/FAIL **直接由 bootstrap 信賴區間的下界決定**。seed 換一個，
下界就會小幅移動；若由實作端選 seed，等於可以在看到結果後試多個 seed
挑一個剛好越過門檻的，而這在論文上完全看不出來——每個 seed 單獨看都合法。
因此這不是可由實作端便宜行事的技術細節，而是必須先於結果、由教授核定的
預註冊項目。E2 的多 condition 比較同理。

B = 10000 而非慣用的 1000：PASS 規則讀的是 2.5% 分位的尾端，B=1000 時該
尾端自身的蒙地卡羅誤差不可忽略，結果落在門檻附近時 PASS/FAIL 可能在兩次
執行間翻轉。168 筆 held-out 跑 10000 次僅需數秒，計算量不構成限制。
兩組取同一個 B，也避免日後被質疑「為何此處 10000 彼處 1000」。

兩組 seed 刻意不同（20260826 / 20260827）：兩者凍結於不同 lock、不同時間點，
分開後任一組需重跑或發現缺陷時不會與另一組糾纏。此項影響很小，
但既然沒有成本就取較乾淨的作法。

**驗證**：`py -3.10 -m pcmef.cli config check` 的待核定項由 14 降為 10；
四個值皆帶 `decided_by: advisor` 與 `decided_on: 2026-08-26`。

**維護邊界**：
- **這四個值在對應 lock 寫入後即不得修改。** 若日後認為 B 應更大，
  必須開新 run 重跑，不得就地改值後宣稱是同一次分析。
- 「換個 seed 看看」在任何情況下都不是除錯手段。若懷疑結果不穩，
  正確作法是加大 B 並在新 run 中重跑，而非比較不同 seed 的結果。
- `statistics` 這組在 `e2.final_n_per_class` 決定之前核定是正確的，
  不是搶跑：B 是蒙地卡羅重抽次數，與樣本數無關，凍結愈早，事後
  可調整的空間愈小。`final_n_per_class` 依 SRC-SAI FR-023 只能由
  e2_pilot 依預註冊 sizing rule 決定，與本則無關。

相關：[NOTE-005]、[NOTE-014]

---

## NOTE-014 real split policy 的教授裁決與 group_rule 判定

**決策日期**：2026-08-26

**適用範圍**：`pcmef/core/splits.py`；`configs/base.yaml` 的 `real_split_policy`；
`freeze/real_split_policy.lock.json`（E1-G09）；`data/splits/split_registry.json`（E1-G02）。

**決策**：以下三項由教授於 2026-08-26 核定。

| 項目 | 值 |
|---|---|
| allocation | calibration **70%** / heldout_real **30%** |
| minimum_per_class | **100** e1-eligible recordings |
| seed | **20260826** |
| 任一 class < 100 | **BLOCK** |
| lock 後 | **禁止 redraw** |
| group 規則 | 若有 session/time grouping 則 group-disjoint 優先，數量取最接近 70/30 的 feasible split |

**group_rule 的實際判定：`seeded_stratified_recording`。**
這不是預設值而是查證後的結論 —— Edge Impulse 匯出中**不存在採集 session metadata**：

- `protected.iat` 全部落在 **46 秒**內（2026-03-15 12:21:19–12:22:05），
  而採集 560 筆 × 41 s 需要 **6h22m**，故該欄為 ingestion 時戳而非採集時間。
- payload 僅有 `device_type / interval_ms / sensors / values`，無任何 session 欄位。

依 SRC-SAI §7.9 的 `group_rule`（session/time group if available; otherwise
seeded stratified recording），退回 seeded stratified。此證據字串寫入
policy 與 lock，且 `RealSplitPolicy` 拒絕在無證據時使用該退路。

**執行結果**（560 筆全數 e1-eligible）：

```
class          eligible  calibration  heldout_real
Bubbly              140           98            42
Empty               140           98            42
Misty               140           98            42
Water-filled        140           98            42
TOTAL               560          392           168
id collisions: 0    heldout access count: 0
```

與裁決預期（98 + 42、392/168）完全一致。

**原因**：這三個值先前是 `!required`，任何程式讀到即中斷（NOTE-005）。
教授裁決後才填入，並附上預期數量供交叉檢核 —— 若實作算出的數量與預期不符，
代表分配邏輯或資料有問題，而不是「反正接近就好」。

**驗證**：
```
py -3.10 -m pcmef.cli split plan-real --source data/raw_real/edge_impulse_export
py -3.10 -m pcmef.cli split freeze-real --source data/raw_real/edge_impulse_export
py -3.10 -m pytest tests/unit/test_splits.py -v
```
已實測：改 seed 後重凍被拒（`locks are immutable`）。

**維護邊界**：
- heldout 只能經 `heldout_ids("e1_final_evaluation")` 取用，其他用途一律拒絕
  並保持 access_count 為 0。calibration / tuning / sanity check 都不算例外 ——
  取用一次就消耗掉這個一次性評估。
- 每類使用獨立 rng（seed 混入 class 名稱與筆數），因此補了某一類的資料
  不會意外重抽其他類。共用單一 rng 會讓「只補一類」變成全體重抽。
- session-group 路徑刻意**未實作**並在被指定時拋例外：等真的有 metadata
  再實作，程式路徑才測得起來。
- `split_registry.json` **進版控**、`freeze/*.lock.json` **不進**（`.gitignore`
  第 17 行，lock 屬於個別 run）。因此 registry 也帶出三組 set hash，與 lock
  內同名欄位由同一個 `_set_hash` 產生。少了這三欄，被版控的 registry 就是
  一份無法對照的孤兒檔 —— 有人改了 `assignments` 不會有任何東西攔。
  已實測三組雜湊與既有 lock 完全相符（`f76776e4` / `4bda77f6` / `8421a54e`）。
- 完整重現這個切分需要 `data/raw_real/`（亦不進版控，560 個 JSON）。
  repo 單獨保有的是 registry 的 assignments 與雜湊，足以**稽核**切分，
  但不足以**重算**。這是刻意取捨，不是遺漏。

相關：[NOTE-005]、[NOTE-011]

---

## NOTE-013 transient 時間窗必須涵蓋完整回波，並在產出當下偵測截斷

**決策日期**：2026-08-26

**適用範圍**：`pcmef/simulation/mitsuba_adapter.py` 的 `scene_path_bounds()`；
`pcmef/simulation/mitransient_adapter.py` 的 `default_binning()` 與截斷偵測。

**決策**：
1. transient 時間窗由 `scene_path_bounds()` 依**實際場景座標**推導
   （光源→瓶面→相機的直達路徑，加上背景板與相機的距離作為每次反射的增量上界），
   再乘 `bounce_budget`（預設 7）。
2. 算完 transient 後立即偵測截斷：峰值落在最後兩個 bin 內，
   或最末 bin 仍保有峰值 50% 以上能量，即**拋例外中斷**。

**原因**：Batch 4 初版的窗以「相機到瓶身來回加瓶徑三倍」估算，得到窗尾 0.442 m。
實測掃描（Empty 場景、0–3 m、10 mm bin）顯示：

| 量 | 實測 OPL |
|---|---|
| 首次抵達 | 0.185 m |
| **峰值** | **0.465 m** |
| 99% 能量 | 1.475 m |
| 末端 | 1.675 m |

**窗在峰值抵達前就關了。** 波形因此單調上升到邊界，找不到右側半高點，
FWHM 恆為 0；而 surrogate 的 Sigma 映射以 FWHM 為基底，整條路不可用。

更值得記的是**這個缺陷沒有在 Batch 4 被發現**。當時的診斷指標
`nonzero_bin_ratio = 0.719` 看起來完全正常 —— 因為截斷後的波形確實有 72%
的 bin 帶能量。真正的徵兆是「峰值貼在窗邊」，而初版沒有量這件事。
缺陷直到 Batch 5 試圖消費該輸出、Sigma 模組拒絕 FWHM=0 時才浮現。

修正後總能量由 712 上升到 3909（約 5.5 倍），確認先前截掉了大部分回波。

**驗證**：
```
py -3.10 -m pcmef.cli sim smoke
py -3.10 -m pytest tests/simulation/test_simulation.py -k "energy or truncat" -v
```
manifest 中每個 scenario 的 `peak_bin` 與 `edge_fraction` 即截斷診斷；
峰值應遠離窗邊，`edge_fraction` 應遠小於 0.5。

**維護邊界**：本場景的光源與相機**非共置**，與真實 ToF 感測器（收發同軸）不同。
因此 `optical_path_to_distance = 0.5`（單程 = 光程長的一半）這個假設
在本場景並不成立，實測 Distance 約 230–257 mm 而非真實的 100–114 mm。
這不是缺陷而是**尚未校準**：該係數屬 E1 calibration 的產物，目前標記為 placeholder。
若未來要讓 surrogate 更貼近真實幾何，正確做法是把光源移到相機位置
（monostatic），而不是調整這個係數去湊距離 —— 後者是 SRC-SAI §10 明列的禁止做法。

相關：[NOTE-012]

---

## NOTE-012 載入 mitsuba 後以 os._exit() 結束行程，繞過 drjit 的 teardown 崩潰

**決策日期**：2026-08-26

**適用範圍**：`pcmef/cli.py` 的 `exit_process()`；所有會載入 mitsuba 的 CLI 子指令
（目前為 `sim smoke`，未來的 scenario generator 與 E1 pipeline 同樣適用）。

**決策**：`sim smoke` 的實際算圖在**子行程**執行，父行程完全不載入 mitsuba。
父行程以 `simulation_smoke_manifest.json` 的內容判定成敗，
但**仍檢查並完整回報子行程的 raw exit code** —— 子行程非零而 manifest 全成功時，
明確標示為已知的 DLL-detach 崩潰並指向本條目。
「以 manifest 判定」與「隱瞞 exit code」是兩回事，後者不可接受。

**原因**：drjit 1.3.1 + mitsuba 3.8.0 在 Windows 上，連續算多個場景後
行程結束時必定崩潰。實測特徵：

- 單一場景算圖後正常結束（exit 0）
- 連續算 4 個 scenario 後必定崩潰，錯誤碼在 `0xC0000005`（ACCESS_VIOLATION）
  與 `0xC0000409`（STACK_BUFFER_OVERRUN）之間變動
- 崩潰發生在**所有工作完成之後**：manifest 顯示 4 ok / 0 failed、
  16 個 artifact 全部落盤、transient 全為有限值、時間軸正確

排除過的做法（依嘗試順序）：

1. `drjit.sync_thread()` + `flush_malloc_cache()` + `flush_kernel_cache()`
   + `gc.collect()` 的顯式釋放 —— **無效**
2. `os._exit(code)` —— **無效**。這一步同時推翻了「崩潰在 Python 直譯器
   teardown」的初步判斷：`os._exit()` 已經跳過 atexit 與直譯器關閉，
   仍然崩潰，代表問題在更下層的 **DLL_PROCESS_DETACH**，
   也就是 Windows 卸載原生 DLL 時執行的 detach handler。
3. `kernel32.TerminateProcess(GetCurrentProcess(), code)` —— **不可靠**。
   單獨測試時有效，但在 CLI 中無效。原因是它是**非同步**的：
   實測 `TerminateProcess` 呼叫後會返回，後續的 `time.sleep(5)` 甚至能完整跑完。
   它只是送出終止請求，Python 仍繼續執行到模組結尾並進入正常關閉，
   於是又撞上同一個崩潰。這種「有時有效」的修法比沒修更危險。
4. **子行程隔離** —— **採用**。父行程不載入 mitsuba，因此本身能乾淨結束；
   崩潰被關在子行程內，且父行程仍看得到它的 exit code。

這不是可以忽略的雜訊。SRC-SAI FR-019 要求所有 formal run 可無 UI 透過 CLI 執行，
而 CI 與批次腳本一律以 exit code 判定成敗；一個「工作全部成功但回報失敗」的
指令會讓整批 simulation 被誤判為需要重跑，或更糟 —— 讓真正的失敗被當成
「又是那個已知的假警報」而被忽略。

子行程隔離同時服務 SRC-SAI NFR-07（400 scenario first-pass 可分批/平行）：
未來要平行跑多場景時，本來就需要行程層級的隔離，
因此這個決策不是為了繞過缺陷而付出的技術債，而是原本就該有的架構。

**驗證**：
```
py -3.10 -m pcmef.cli sim smoke --config configs/simulation/smoke.yaml
echo $LASTEXITCODE   # 必須為 0
py -3.10 -m pytest tests/simulation/test_controller.py -k exit -v
```

**維護邊界**：這是對上游缺陷的因應，不是好的通則。
未來 drjit 修正此問題後應移除本函式並改回 `SystemExit`；
移除前必須先確認連續多場景算圖能乾淨結束。
**不得**把 `os._exit()` 擴大套用到未載入 mitsuba 的路徑 ——
那會在無關的指令上靜默吞掉 atexit 清理與未排清的日誌。

相關：[NOTE-011]

---

## NOTE-011 原始 ToF 500 點序列的遺失與復原

**決策日期**：2026-08-26（同日先判定遺失、後判定復原）

**適用範圍**：M0 資料可用性判定；E1 的 Primary/Secondary 指標定義；
`adapters/legacy_csv.py`、`adapters/legacy_kg.py`、`adapters/edge_impulse.py`
三個 reader 的適用範圍。

> **狀態：已復原。** 取得 Edge Impulse 完整 dataset export 後，
> **560 筆 500×4 原始序列全數可用**，`CanonicalCase` 的 (500,4) 契約
> 由真實資料滿足，E1 **可照原規格進行，不需降級為窗口統計量**。
> 下方原始判定的第 1、2 點因此撤銷；第 3 點仍成立且更為必要。

**決策**：
1. ~~承認真實 ToF 的四特徵 500 點原始序列不存在，M0 只能以滑動窗口 Mean/Std
   作為 real 側證據~~ —— **已撤銷**，見「復原紀錄」。
2. ~~E1 若要進行，synthetic 側必須套用相同滑動窗口處理，並改寫
   `e1_scientific_rule.lock` 的措辭~~ —— **已撤銷**，不再需要此妥協。
3. 取樣間隔一律由該筆 recording 推導，**三個文件值皆不得作為預設**。此點成立。

---

### 復原紀錄

來源：`andycohen-project-1-export 2.zip`（Edge Impulse 官方 dataset export，
由擁有者自行下載）。逐檔驗證 560 個 JSON，**0 筆結構異常**：

| 核對項 | 實測 |
|---|---|
| 樣本總數 | 560（training 448 / testing 112） |
| 每類 | Empty / Water-filled / Bubbly / Misty 各 140（112 + 28） |
| measurement 編號 | 每類 1–140 **完整無缺號** |
| 每筆形狀 | 560/560 皆為 **500 × 4** |
| 欄序 | 560/560 皆為 Distance → Ambient → Signal → Sigma |
| interval_ms | 560/560 皆為 **82.00001312**（單一值） |
| 500 × 82.00001312 ms | **41.0000 s** |
| 448 × 41 s | 18,368 s = **5h 6m 8s**（與 Edge Impulse 畫面一秒不差） |

**與獨立來源交叉驗證**：與先前自 Google Drive 取得的 Kaleidagraph 窗口統計量
比對 16 個 class×feature 組合，**全部吻合到小數第四位**（相對差 0.00%）。
兩個來源彼此獨立，因此可互為佐證。四類距離平均
（Empty 100.91 / Water 113.87 / Bubbly 105.57 / Misty 79.51 mm）
亦與 SRC-PLAN §2.1 的錨點相符。

**邊界**：這是 Edge Impulse ingest 後再匯出的 JSON，**不是**硬碟上
byte-for-byte 的原始 CSV。數值序列、label、train/test 切分、interval
與 measurement 編號都保存了，但匯出時四捨五入到**四位小數**——
這一點在 Sigma divisor 的量化檢定中直接影響可判定範圍（見 [NOTE-010]）。
reader 因此仍逐筆驗證形狀與欄序，不因為「官方匯出」就跳過。

**原因**：2026-08-26 取得 Drive「實驗交接」資料夾（擁有者 m90099457，
共享給本人）後逐檔盤點，結果如下。

`KG_all_kaleidagraph.7z`（1.2 MB，46 個 CSV）：
- `KG_all/` 內有 32 個檔，命名為 `KG_<condition>_<Metric>_<Mean|Std>.csv`
  （4 類 × 4 metric × 2 統計量）。每檔 shape 皆為 **(99, 140)** ——
  140 欄是 `No.1`…`No.140`，即每類 140 筆 recording；99 列是滑動窗口。
  由 500 點推得窗口數 99，對應 window=10 / step=5（(500−10)/5+1=99）；
  確切參數需回讀 SRC-NOTION「合併csv、資料後處理」的
  `calculate_sliding_window_stats()` 確認。
- **這些檔案沒有時間欄**，因此本身不帶 measurement-time provenance。
- 僅有 3 個檔案是真正的原始序列：`偏移量測試/{left,normal,right}/`
  下的 `lowest_confidence_window_1.csv`，shape (500,3)，
  欄位 `Sample_Index / Distance_mm / Timestamp` —— **只有 Distance 一個 metric**，
  且來自推論腳本的 `save_lowest_confidence_data()` 副產物，不是資料集本體。

`image_classify.7z`（81.5 MB）：**Vision 側完整**。
`converted_keras/dataset/` 下 4 類各 300 張、共 1200 張 .jpg，
資料夾名已是 canonical 標籤；另含 `keras_model.h5`、`best_model.pth`、`labels.txt`。
與 SRC-PLAN §2.1「4 classes × 300 images = 1,200」完全吻合。

**因此模態間的可用性是不對稱的**：Vision 有完整原始資料，ToF 只有衍生統計量。

**附帶發現：資料集內部存在兩種取樣率（2026-08-26 修正）。**

初次盤點時，三個偏移測試原始序列的實測取樣間隔中位數為 **0.0624 s**
（500 點 = 31.7 秒），當時據此推論「0.082 與 0.02 都不是實測值」。
**該推論不成立**，後續讀取 SRC-NOTION 的 Edge Impulse 設定截圖後修正如下：

Edge Impulse impulse 設定明列 `Frequency (Hz) = 12.19512`、
`Window size = 41,001 ms`。換算：

- 1 / 12.19512 = **0.082 s**，即 SRC-PLAN 記載的 82 ms/sample
- 41,001 ms / 82 ms = **500.01 ≈ 500 點**，一個 window 恰為一筆 500 點 recording

交叉驗算：`Training windows = 448`、`Data in training set = 5h 6m 8s`；
448 × 41 s = 18,368 s = 5h 6m 8s，完全吻合。448 / 560 = 0.8，
對應 Edge Impulse 的 80/20 train/test 切分。

**因此 0.082 是主資料集的正確取樣率，不是未經驗證的文件數字。**
0.0624 屬於偏移測試那次獨立採集，兩者是不同的 acquisition session。

這個修正**強化**而非削弱了 `adapters/legacy_csv.py` 逐 recording 推導間隔的設計：
同一個資料家族內部真的存在不同取樣率，任何全域常數都必然對其中一批是錯的。

**附帶驗證：偏移錨點來源確認。** 三筆原始序列的 Distance 平均值為
normal 98.53 / left 90.91 / right 88.73 mm，與 SRC-PLAN §2.1 記載的
baseline 98.61、±0.1 cm 側 91.05 / 88.76 相差均在 0.15 mm 內。
SRC-PLAN 的 offset anchor 數字來源自此，可追溯。

**驗證**：
```
py -3.10 -c "import pandas as pd; print(pd.read_csv(r'data/raw_real/tof_aggregated/KG_all/KG_nowater_Distance_mm_Mean.csv').shape)"
```
應回傳 `(99, 140)`。Vision 側：`data/raw_real/vision/` 下四類各 300 張。

**維護邊界**：`adapters/legacy_csv.py` 目前只支援
`<condition>/<metric>/*.csv` 的逐 recording 格式，**讀不了**上述 (99,140)
矩陣格式；它的對齊、排除帳與五層計數契約仍然正確，但需要第二個 reader。
在補上之前不得宣稱 M0 可以完成。原始逐 recording CSV 若仍存在，
最可能在樹莓派 `/home/pi/`（見 [NOTE-008]）。

相關：[NOTE-008]、[NOTE-010]

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
`0x1A`/`0x1C` 加 `/128.0`）。

**2026-08-26 修正：此項已由真實資料解出為 `0x18`，且先前的直覺是錯的。**

當時（以及一份外部摘要）的推論是：「四參數腳本宣告 0x1E 而它真的用到 sigma，
所以 0x1E 是高可信候選；0x18 只出現在沒用到 sigma 的單參數腳本，
可能是沒清乾淨的 legacy constant。」**這個推論方向完全相反。**

取得 Edge Impulse 完整匯出（560 筆 × 500 列 = 280,000 列真實觀測）後做排除檢驗：
若 sigma 欄讀自存放測距結果的暫存器，則 `sigma × 65536` 必須等於同一筆的
`distance`（同一個 16-bit 讀值被除了兩次不同的數）。實測：

| 檢驗 | 假設成立應為 | 實測 |
|---|---|---|
| `sigma×65536` 等於 distance 的比例 | 100% | **0.0000%** |
| 相關係數 | +1.0 | **−0.476** |
| 隱含 raw 值域 | 43–118（distance） | **15473–55883** |
| 量級比 | 1.0 | **298.5** |

因此 sigma 欄**不是**從存放測距值的暫存器讀來的。而 `0x1E` 正是 VL53L0X
result 區塊中的 final range（自 `0x14` 起算 offset 10–11），
故 `0x1E` 被排除，候選只剩 `0x18`。

**這反過來說明那幾支推論腳本有 bug**：四參數與兩參數腳本把 final range
當成 sigma 讀，再除以 65536，得到約 0.0015 mm 的假 sigma。
單參數腳本宣告的 `0x18` 才是對的 —— 它只是剛好沒用到那個值。

**這不是「挑一個」而是「刪到剩一個」**：前者需要理由，後者需要反證，
而反證已經有了。實作為 `provenance/sigma.rule_out_range_register()`。

同時 divisor 也由量化步長檢定確定為 `/65536`：`sigma × 128` 的最大殘差
0.4992 遠超匯出四位小數所容許的 0.0064，故 `/128` 被排除；
`/1` 因觀測值非整數被排除。注意 `/65536` 本身**無法**由量化檢定證明
（1/65536 = 1.5e-5 比匯出精度 1e-4 還細，量化痕跡已被 rounding 抹掉），
它是刪除其餘候選後的唯一倖存者。

**保留條件**：採集腳本本身仍未取得。上述為資料反推的結論；
若日後取得採集腳本且與此矛盾，以腳本為準並重新 freeze。
`configs/base.yaml` 的 `acquisition_script_obtained: false` 記錄此狀態。

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
