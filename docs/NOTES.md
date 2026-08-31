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

## NOTE-047 Provider execution path：影像必須是 bytes，schema 必須真的送出

**決策日期**：2026-08-31

**適用範圍**：`pcmef/agents/provider.py` 的 `_payload_to_parts`、
`_payload_to_openai_content`、`GoogleAdapter.invoke`、
`OpenAICompatibleAdapter.invoke`、`StubOfflineAdapter.invoke`；
`pcmef/agents/pcmef_agents.py` 的四個 agent。

**決策**：

1. payload 以保留鍵 `evidence_images` 攜帶影像，值為 base64 bytes，
   **不得是檔案路徑**。Google 走 `inlineData`（camelCase，與已驗證的
   `verify_vision` probe 同形），OpenAI 相容走 `image_url` data URI。
2. 需要 structured output 的角色一律送出 JSON schema
   （Google `responseSchema` + `responseMimeType`；OpenAI
   `response_format.json_schema`），回應必須 `json.loads` 後再經
   `jsonschema.validate`。
3. `needs_image` 的角色收不到影像時**直接拒絕執行**，不得退回純文字請求。
4. `StubOfflineAdapter` 記錄每次 invoke 實際收到的影像數、影像位元組數與
   是否帶 schema，並回傳一個 schema-valid 的合成實例。

**原因**：

修改前 `_payload_to_parts()` 是 `[{"text": json.dumps(payload)}]`，
`OpenAICompatibleAdapter.invoke()` 是 `self._chat(..., _payload_to_text(payload))`。
兩條路徑都把整個 payload 序列化成文字，於是：

* **vision agent 從來沒有看過圖。** 它收到的是一段描述影像的 JSON 文字。
* **structured agent 從來沒有要求過 schema。** 回應碰巧是 JSON 就過，
  不是 JSON 也沒有人檢查。

這種失敗**不會拋任何例外**，指標照樣算得出來 —— E2 會產生一組看起來
完整的數字，而「多模態」那一半其實不存在。`verify_vision` probe 是綠的
也救不了：probe 走 `_generate()` 手工組的 parts，`invoke()` 走另一條路，
兩者之間沒有任何共用程式碼。

第 3 點（收不到圖就拒絕）是關鍵：若容許退回純文字，上述失敗會在真實
provider 上以「安靜降級」的形式重演，而且比原本更難發現，因為那時
`evidence_images` 這個鍵已經存在了，只是碰巧是空的。

禁止傳路徑另有 NOTE-003 / NOTE-004 的理由：路徑字串含 class 與
condition，送進 provider 等於把 ground truth 一起送出去。

第 4 點的用意是讓離線測試**證明得了**上述每一條，而不是只證明
「呼叫沒有拋例外」—— 送出純文字也不會拋例外，那正是這個缺陷原本
躲過所有測試的原因。stub 依 `STUB_ROLE_HINTS` 遵守角色契約
（`visual_semantic_agent` 不得自稱 `physics`），因為它要模擬的是一個
**守約**的模型，不是一個剛好通過 schema 的模型。

**驗證**：

```
py -3.10 -m pytest tests/agents -v
```

`test_google_puts_a_real_png_on_the_wire` 與
`test_openai_compatible_puts_a_data_uri_on_the_wire` 攔截 `httpx.request`，
從實際送出的 request body 取出影像並解碼，斷言前 8 個位元組是 PNG magic。
`test_vision_agent_refuses_to_run_without_an_image` 斷言缺圖時拒絕執行。

---

## NOTE-046 Agent 的 representation mode 與 bounded retry 次數核定

**決策日期**：2026-08-31

**適用範圍**：`configs/base.yaml` 的 `agents.representation_mode` 與
`agents.retry.max_attempts`；`pcmef/agents/pcmef_agents.py` 的 prompt 組裝與
retry 迴圈；後續任何呼叫四個 agent 的 formal run。

**決策**：

1. **`agents.representation_mode = FIXED_SUMMARY`。**
   ToF 的 500x4 逐點序列不進 prompt，改以固定欄位的統計摘要表示。
2. **`agents.retry.max_attempts = 2`**（第一次 + 一次 retry）。
   耗盡即 **ABORT FORMAL RUN**，**不得 drop case**。

**原因**：

FULL_500x4 的 token 成本隨 recording 線性成長，而 500 個點絕大多數是
同分佈的重複取樣 —— 付出的 token 買不到對應的資訊量。更關鍵的是
**長度一致性**：不同長度的 prompt 會讓 case 之間的條件不同，那是一個
在比較兩個 arm 時無法被控制掉的額外變因。FIXED_SUMMARY 讓每個 case 的
prompt 結構完全相同，模態證據的差異因此是唯一的變數。

retry 取 2 而不是更多：bounded retry 的意義是「暫態失敗可以再試一次，
但系統性失敗必須被看見」。次數放大只會把系統性失敗磨成偶發失敗。
耗盡後 abort 而不是 drop，是因為丟掉一個算不出來的 case 會讓分母悄悄
變小 —— 那正是 SRC-SAI §28 / FR-013 要防的事：一個 100 case 的實驗
悄悄變成 97 case，而報告上仍然寫 100。

兩項都在 families 36-43 的 Formal E2 **產生之前**核定，且核定當下
沒有看過任何 final 結果。

**驗證**：

```
py -3.10 -m pcmef.cli config check
```

`agents.representation_mode` 與 `agents.retry.max_attempts` 不再出現在
待核定清單（由 10 項降為 8 項）。

---

## NOTE-044 Ambient 抖動的下界、σ_MC 退化的三個分支，與不可行登記界線的可行域推導

**決策日期**：2026-08-30

**適用範圍**：`pcmef/surrogate/ambient.py` 的 `map_ambient_rate()`；
`pcmef/experiments/calibration_stage0.py` 的 `leverage_of()`、
`sigma_mc_floor()`、`derive_feasible_edge()`；
`configs/amendments/AMD-004.yaml`；
`tests/surrogate/test_surrogate.py`、`tests/unit/test_calibration_stage0.py`。

**決策**：

1. **`ambient_jitter_relative` 的拒絕條件由 `<= 0` 改為 `< 0`。**
   0.0 是已凍結登記範圍 `[0.0, 0.5]` 的下界；jitter = 0 為精確決定性，
   且**不消耗亂數**（與 signal / distance / sigma 的 `if relative_sigma > 0`
   同一寫法），因此 CRN 在 jitter = 0 與 jitter > 0 之間仍然對齊。
2. **σ_MC 退化改為三個決定性分支**，以相對門檻
   `SIGMA_MC_RELATIVE_FLOOR = 1e-12` 判定：`RATIO` / `INERT`（槓桿恰為 0）/
   `DETERMINISTIC_RESPONSE`（具名結局 `ADMITTED_DETERMINISTIC`）。
   **任何分支都不回傳 inf，也不回傳憑空發明的大數。**
3. **不可行的登記界線改由預註冊二分法導出可行域**（20 步、相對容忍 1e-3，
   一律取可行側），凍結的 `parameter_ranges` 一個位元都不動。

**原因**：

**其一：一道斷言擋住了它自己無權更改的界線。** 原本的 `jitter <= 0` 是為了
執行 SRC-SAI §10「不得每類一個無抖動常數」。但 0.0 寫在
`initial_simulation.lock` 的 `parameter_ranges` 裡，而 `parameter_ranges`
在 ERR-001 的勘誤禁區內 —— 於是 stage 0 在原理上無法評估一個它不得修改的下界。
原顧慮沒有被丟掉，改由**目標函數**承擔：J 比的是分佈的 W1，真實 Ambient 的
離散度不為零，jitter → 0 會讓 ambient 那幾項變差而被 optimizer 自己排除。
用分佈距離擋，比用一道會擋住合法邊界的斷言擋更精確。

**其二：一個 None 蓋住了兩件方向相反的事。** 先前 σ_MC ≤ 0 一律回傳 None
並判為 UNDETERMINED。但「沒有雜訊也沒有反應」與「沒有雜訊卻會動」是相反的：
前者的槓桿**恰為 0**（這個 observable 對該參數毫無資訊，是完全確定的事實，
不該讓流程停下來等裁決），後者的訊噪比**發散**（是最強的可辨識證據）。
兩者都不得寫成 inf —— 寫成 inf 會讓一個沒有雜訊參考的量自動取得入場券。
因此第二種給一個**具名結局**而不是一個數字。

**其三：協定對「登記界線本身不可行」沒有規定。** 實測
`noise_relative_sigma` 在上界 0.5 使距離映射得到 −4.915 mm，`map_distance`
正確拒絕。既不能改凍結界線，也不能 clamp（clamp 會讓最佳點落在邊界上而看不出
它其實想往外走，`out_of_bounds_policy` 已明令禁止），唯一不越權的做法是
**由事前固定的規則導出**可行子域並連同規則一起記錄。二分法的起點、步數與
容忍值全部事前固定，且一律取可行側，因此導出的域必為登記域的子集 ——
它不是「把界線縮到好看為止」。

**驗證**：

```
tests/surrogate/test_surrogate.py::test_negative_ambient_jitter_is_refused
tests/surrogate/test_surrogate.py::test_zero_ambient_jitter_is_deterministic_rather_than_refused
tests/unit/test_calibration_stage0.py::test_leverage_never_returns_infinity_for_any_input
tests/unit/test_calibration_stage0.py::test_inert_and_deterministic_response_are_not_conflated
tests/unit/test_calibration_stage0.py::test_the_derived_domain_is_always_a_subset_of_the_registered_range
```

**維護邊界**：本條的三項改動全部屬於 **AMD-004**。在 AMD-004 與
CAL-PREREG-003 都凍結**之前**，`execute_stage0()` 會拒絕執行，
因此這些語意不會在宣稱 CAL-PREREG-002 的情況下被跑出來。

---

## NOTE-043 stage 0 的 observable 必須含離散度，且入場判定不得靠別的階段的通道

**決策日期**：2026-08-30

**適用範圍**：`pcmef/experiments/calibration_stage0.py`（新增）、
`pcmef/experiments/calibration_stage0_freeze.py`（新增）、
`pcmef/cli.py` 的 `calibration stage0`；
`tests/simulation/test_simulation.py`；`tests/unit/test_calibration_stage0.py`。

**決策**：

1. stage 0 的 observable 取 **4 類 × 4 特徵 × 2 統計量（median、IQR）= 32 項**，
   `IQR` **必須**在內。
2. 每個參數同時記錄 `max_leverage`（預註冊字面規則：對全部 observable 取 max）
   與 `max_leverage_declared`（限制在該參數**自己階段宣告要最佳化**的通道上）。
3. `tests/simulation/test_simulation.py` 的 surrogate 整合測試改餵 ambient pass，
   並新增成對測試確認只餵 active pass 仍被拒；另新增
   `PCMEF_REQUIRE_SIMULATION=1` 把缺 LLVM 的靜默 skip 變成失敗。

**原因**：

**其一：只用 median 會讓兩個參數在建構上不可辨識。** CAL-PREREG-002 stage 1
的可辨識性論證正是「兩個參數、兩個互相獨立的統計量」——
`ambient_energy_to_mcps` 只移動位置、`ambient_jitter_relative` 只改變離散度。
若 observable 只取 median，後者的槓桿**必然**接近 0 而被自動 gauge-fix，
那會與預註冊自己寫下的論證直接矛盾。

**其二：對全部 observable 取 max，會讓參數靠「自己階段從不最佳化的通道」
拿到入場券。** 實測：`_FOIL_SIZE_TO_DIAMETER_RATIO` 以 Water-filled 的
ambient 取得槓桿 **101.7** 而被 ADMITTED，但它在 stage 3 自己宣告的通道
（Empty 的 distance / signal / sigma_like）上只有 **0.0006**。
入場的意思是「optimizer 看得見這個參數」，而 optimizer 只看得見該階段目標
函數裡的項。兩個數字都記錄，因此舊規則的判定仍然可稽核。

**其三：假綠燈是這一輪最貴的缺陷。**
`test_surrogate_can_consume_the_rendered_transient` 從 NOTE-034／NOTE-037
之後就一直用舊契約，**LLVM 在 PATH 上時失敗、不在時靜默 skip**，
於是「測試沒過」與「測試沒跑」在 exit code 上完全相同，整整一輪沒有人看見。
修法是把 `render_transient()` 本來就已經寫進 manifest 的
`optical_transient_ambient` 接上，**production 契約一個字都沒有放寬**；
另加成對測試，讓「放寬契約」與「修好測試」在測試結果上分得開。

**驗證**：

```
$env:PATH = "C:\Program Files\LLVM\bin;$env:PATH"
$env:PCMEF_REQUIRE_SIMULATION = "1"
py -3.10 -m pytest            -> 1681 passed, 0 failed, 0 skipped
（不設 LLVM 時：1 failed + 11 skipped，exit 1，訊息具名指出缺 LLVM-C.dll）
py -3.10 -m pytest tests/unit/test_calibration_stage0.py -v
py -3.10 -m pcmef.cli calibration stage0 --out outputs/calibration/stage_0
```

**維護邊界**：不得為了讓整合測試變綠而讓 surrogate 接受只有 active pass 的
呼叫；`test_the_production_surrogate_contract_still_refuses_an_active_only_call`
就是為了讓那種「修法」留下痕跡。

---

## NOTE-042 CAL-PREREG-001 的三項 pre-execution 缺陷與 AMD-003 的裁決

**決策日期**：2026-08-30

**適用範圍**：`configs/calibration_preregistration.yaml` v0.2.0；
`pcmef/experiments/calibration_plan.py`（新增）；
`pcmef/experiments/calibration_prereg.py` 的 CP-13..CP-16；
`configs/amendments/AMD-003.yaml`、`freeze/amendments/AMD-003.amendment.json`；
`data/splits/calibration_access_ledger.json`（新增）；
`tests/unit/test_calibration_plan.py`、`tests/simulation/test_empty_topology.py`；
`pcmef/cli.py` 的 `amendment freeze` / `amendment status`。

**決策**：

在**第一次讀取 calibration partition 之前**，以 AMD-003 修正三項 pre-execution
缺陷，並把操作版本凍結為 **CAL-PREREG-002**。CAL-PREREG-001 保留不刪除
（append-only），但標記為**不得執行**。

1. **P0-1**：stage 0 的入場分母由 `s_f` 改為 **σ_MC**（模擬自身的種子間標準差），
   使它真的只用模擬。門檻 K = 10，BORDERLINE 帶 [5, 20] 停止等待裁決。
   原本以 s_f 提的問題降為 stage 1 開始時計算的**不具決定權**診斷。
2. **P0-2**：評估預算改由公式導出並由程式強制（`maxiter` 100，合計
   **116,352** 次評估）；移除第二條求解路徑；新增 `dimension_expansion`。
3. **P1-3**：Empty 拓樸經稽核確認**程式正確**，僅更正預註冊措辭。
4. 新增 `resolve_numeric_bounds()` 作為 bounds 的唯一重建路徑，
   並凍結 `bounds_resolution_hash`。
5. 新增 calibration partition 的 append-only access ledger。

**原因**：

**其一（P0-1）：stage 0 的判準在原理上不可能是 simulation-only。**
v0.1.0 一邊宣告 `reads_calibration_partition: false`，一邊把入場門檻寫成
`leverage = |Δo| / s_f`，而 `s_f` 在同一份文件裡的定義就是
**calibration split 真實值**的 pooled IQR。照字面執行必須先讀真實資料。

裁決取 **A（讓 stage 0 真的只用模擬）**，不取 B（把它正名為會讀資料的階段）。
兩個獨立理由：

| | 選 B 的後果 |
|---|---|
| **Leakage** | 「要擬合哪些參數」會由真實資料決定 —— 一個位於所有階段上游的**資料相依模型選擇**步驟。現有的守衛都攔不到它：verification seeds 查的是種子過擬合，不是選擇偏誤。而且它會把 first access 提前，卻換不到任何科學上的好處。 |
| **Identifiability** | stage 0 要問的是「optimizer 看不看得見這個參數」。對 Monte Carlo 模擬器而言，相關的雜訊底線是它自己的**種子間離散度**，不是真實資料的 IQR。s_f 回答的是另一個問題（這個參數對 J 重不重要）—— 合理，但那是診斷，不是入場券。 |

新分母為 **σ_MC**：在凍結的 initial 值上以 **R = 8** 組獨立種子各算一次，
取每個 observable 的樣本標準差。門檻 **K = 10**（約 10√8 ≈ 28 個平均值標準誤），
落在 **[5, 20]** 者標為 BORDERLINE 並**停止**等待裁決，不自動歸類 ——
與 estimator 那一輪的 TIE_BREAK_REQUIRED 是同一條規則。

共線性檢查同樣改以 σ_MC 逐分量正規化。原本對「原始響應向量」取餘弦，
而各分量單位不同（mm、MCPS…），那個餘弦會隨單位改變而改變，
0.98 這個門檻本來就沒有定義好。

s_f 的問題沒有被丟掉：降為 `s_f_relative_leverage_diagnostic`，
在 stage 1 開始、s_f 已合法產生之後計算，**明文禁止**用它改變任何階段的參數集合。

**其二（P0-2）：預算三個數字互相矛盾，且低估 8.6–25.7 倍。**
v0.1.0 同時寫了 `max_evaluations_per_stage: 3000` 與 `maxiter: 200`。
實測 scipy 1.15.3：

| N | 族群 P | nfev（單次重啟） | 相對 3000 |
|---|---|---|---|
| 2 | 32 | 4,192 | 1.4× |
| 3 | 64 | 11,584 | 3.9× |
| 4 | 64 | 12,864 | 4.3× |
| 7 | 128 | **25,728** | **8.6×**（三次重啟 25.7×） |

根因是把 `popsize` 當成族群大小，但它是**乘數**，而 `init='sobol'` 還會把族群
補到 2 的冪（`n_s = int(2 ** np.ceil(np.log2(...)))`）。改為由公式導出：

```
P(N)                  = 2 ** ceil(log2(popsize * N))
budget_per_restart(N) = P(N) * (maxiter + 1)
budget_per_stage(N)   = restarts * budget_per_restart(N)
```

`maxiter` 降為 100，展開後：AMBIENT 2d/P32/9,696；SIGNAL_SCALE 2d/P32/9,696；
GEOMETRY_SURFACE_FOIL 7d/P128/38,784；PARTICIPATING_MEDIA **6d**/P128/38,784；
SENSOR_SURROGATE 3d/P64/19,392。**合計 116,352 次評估**，
以每次約 2 秒估算約 65 小時 —— 這個成本寫進協定，
是為了讓「預算不夠用」在開始之前就被看見。

順帶消掉兩個相關的歧義：`scalar_stages.applies_to` 列的是**參數**而非階段，
於是「stage 1 的兩個參數要不要逐一純量搜尋」沒有答案 —— 整條路徑移除，
每個階段一律 DE；`_ALBEDO_BY_PRESET` 是三個 class-specific 純量，
究竟佔 1 還是 3 個維度會直接改變族群大小 —— 新增 `dimension_expansion`
明文宣告，未宣告的多值參數一律拒絕。**PARTICIPATING_MEDIA 因此是 6 維而非 4 維。**

**其三（P1-3）：Empty 拓樸是措辭缺陷，不是程式回歸。**
v0.1.0 寫「`build_scene_dict()` 只在 `medium_preset != "empty"` 時建立內部介質」，
與 NOTE-029 當初描述**缺陷**的句子幾乎相同，容易被讀成「Empty 不建內圓柱」。
逐行核對與實際建構後確認：

```
bottle_interior 無條件建立，四類皆然（只有 interior["interior"] = medium 是條件式）
_DENSITY_KEY_BY_PRESET 沒有 "empty" 鍵 -> _medium_dict() 回 None
_INTERIOR_BASE_IOR["empty"] = "air"
實測：四類 r_in 26.5 mm / r_out 28.5 mm（壁厚 2.0 mm）
      Empty int_ior=air ext_ior=bk7 has_participating_medium=False
```

**Empty 是玻璃殼＋空氣＋玻璃殼，沒有回歸。** 因此不觸發 STOP，
`initial_simulation.lock` 不受影響。措辭已更正並附程式碼引用，
另立 `tests/simulation/test_empty_topology.py` 逐次確認，不靠記憶。

**其四：bounds 唯一性。** 新增 `resolve_numeric_bounds()` 作為**唯一**路徑：
原生數值取自 lock，字串界線必須有宣告的解讀且
`float(凍結字面值)` 必須**恰好等於**宣告值（不符即拒絕 —— 那等於在不改 lock
的情況下放寬界線）。結果為 20 個維度，
`bounds_resolution_hash = c71c39995d7a64ab…`，凍進 AMD-003 與 CAL-PREREG-002。

**其五：calibration 也要有 access count。** held-out 有，calibration 沒有，
於是「還沒讀」只是一句話。新增 append-only
`data/splits/calibration_access_ledger.json`，明訂什麼算一次 access
（讀 recording **數值**算；對 recording id 集合取雜湊不算），
first access 在 stage 1 開始，以及順序規則：帳上 count > 0 而 stage 0 artifact
不存在，代表順序反了，流程必須停止。

**驗證**：

```
py -3.10 -m pcmef.cli amendment freeze --spec configs/amendments/AMD-003.yaml
  AMD-003  612dabf1b323a730…   9 contracts changed
  heldout access 0   calibration access 0
py -3.10 -m pcmef.cli calibration preregister --validate
  CP-01..CP-16 全 PASS      protocol_hash c6a0de86e866cc0d…
py -3.10 -m pytest tests/unit/test_calibration_plan.py \
                   tests/simulation/test_empty_topology.py -q   # 39 passed
py -3.10 -m pytest tests/unit/test_calibration_prereg.py -q     # 49 passed
```

`test_population_matches_scipy` 對 N=1,2,3,4,6,7 **實際呼叫 scipy**，
斷言 `nfev == P × (nit + 1)`；scipy 換版而規則改變時它必須失敗。
`test_a_real_de_run_that_exceeds_its_budget_fails` 把預算調到實際需要的四分之一，
DE 必須被 `BudgetExceeded` 中止；其對偶
`test_the_preregistered_budget_is_actually_sufficient` 防止守衛反過來
把合法執行擋掉（少了它，前者可以靠 `limit=0` 通過）。

驗證器上線後又抓到本次修訂自身的兩個缺口：`optimizer.seeds` 因插入
`evaluation_budget` 區塊而被縮排成它的子鍵（CP-07 FAIL）；
CP-15 原本掃整個 stage_0 區塊，把 `why_not_s_f` 這種**說明為什麼不用 s_f**
的欄位也判成違規（與 CP-04 守衛自我指涉是同一類錯誤），已改為只掃操作性欄位。

**維護邊界**：
- 不得把 stage 0 的分母改回 s_f，或讓 `s_f_relative_leverage_diagnostic`
  變成 gating。
- 不得手改 `evaluation_budget.resolved`；CP-13 逐項比對公式。
- 不得讓 `scalar_stages` 復活；求解路徑必須唯一。
- 不得在 `resolve_numeric_bounds()` 之外另算一次 bounds。
- 不得刪除 `tests/simulation/test_empty_topology.py`；stage 3 的整個
  可辨識性論證建立在它驗的那兩件事上。
- 本次仍**未**執行 stage 0、未讀 calibration partition、未開 held-out。

相關：[NOTE-041]、[NOTE-040]、[NOTE-035]、[NOTE-029]、[NOTE-032]、
[NOTE-037]、[NOTE-028]、[NOTE-012]

---

## NOTE-041 校準先預註冊：26 個參數拆成五個各自可辨識的階段

**決策日期**：2026-08-29

**適用範圍**：`configs/calibration_preregistration.yaml`（新增）；
`pcmef/experiments/calibration_prereg.py`（新增）；
`pcmef/cli.py` 的 `calibration preregister`；
`freeze/preregistrations/CAL-PREREG-001.prereg.json`。

**決策**：

1. 在**讀取 calibration partition 之前**凍結校準的全部規格：目標函數、
   正規化、分階段參數分組、optimizer、種子、邊界、收斂、重啟、平手、
   失敗處置與 artifact 記錄。
2. 目標函數為 **calibration split 上的 distribution-level 差異**：
   `J = Σ_{c,f} w[c,f] · NW(real[c,f], sim[c,f])`，
   `NW = W1 / s_f`，`s_f` 為 calibration-only pooled IQR，**只算一次並凍結**。
   權重事前固定為 **UNIFORM = 1.0**（16 項）。
3. **26 個 calibration-only 參數不得一起 fit**，拆為五階段依序進行，
   每階段結束後其參數即凍結：
   Ambient → active Signal scale → geometry/surface/foil → 參與介質 →
   sensor surrogate。另有 **stage 0 可辨識性探測**（純模擬，不碰真實資料）。
4. 18 項進入擬合、**8 項宣告不擬合**（數值/離散化設定與 gauge 固定項）。
5. optimizer 不得觸碰 `_SIGMA_T_REFERENCE_PER_M`、`lighting.irradiance`、
   `_ROOM_LIGHT_RADIANCE`、`optical_path_to_distance`，以及 estimator
   與其兩個可調參數。

**原因**：

**其一，分階段的依據是「結構性解耦」，不是「這樣比較好跑」。**
每一階段的可辨識性都建立在一個**已量測或由程式碼可驗證**的事實上：

| 階段 | 解耦的依據 | 性質 |
|---|---|---|
| 1 Ambient | ambient pass 的 VCSEL 是關的；實測 irradiance 1.0→4.0 時 ambient 3.603355→3.603355 | **精確**（NOTE-034 A2） |
| 2 Signal scale | 增益作用在 estimator **之後**；S2 實測距離變化 0.0 | **精確**（NOTE-035） |
| 3 幾何/箔片 | **Empty 沒有介質** —— `build_scene_dict()` 只在 `medium_preset != "empty"` 時建介質，故 Empty 的 observable 與三個密度無關 | **程式碼可驗證** |
| 4 參與介質 | 幾何已由 Empty 單獨釘死；三類各有自己的密度與 albedo，無交叉項 | 結構性 |
| 5 sigma 映射 | 波形已固定，本階段只擬合由波形算 sigma 的映射，不回頭改前三個通道 | 結構性 |

其中第 3 條是關鍵：**只用 Empty 擬合幾何**不是為了省事，而是唯一能讓
幾何與介質不互相污染的切法。

**其二，做這份分析時發現一組先前未登記的簡併，因此當場裁決。**
`_FOIL_GAP_TO_BOTTLE_RATIO` 與 `distance_offset_mm` 都讓四類的 distance
**一起平移**，在 distance 位置這個統計量上精確簡併 —— registry 的
CG-1/2/3 都沒有涵蓋它。登記為 **CG-4_absolute_distance**，並依 CG-1/CG-2
同一套理由（固定沒有物理內容的那一個）裁決：
**固定 `distance_offset_mm = 0.0`，擬合 `_FOIL_GAP_TO_BOTTLE_RATIO`**。
一個非零的 distance offset 等於宣稱「光程算對了但讀數要平移」，
那不是物理，是把殘差藏起來。

另記下一組**近似**簡併：`signal_energy_to_mcps` 與三個箔片振幅參數
在 signal 位準上難以區分，唯一的區分來自 distance 通道 ——
post-hoc 增益不移動 distance，物理振幅則經 LEADING_EDGE 的 range walk
移動它（實測振幅 10→500 時距離變動 10.0%，NOTE-037）。這個區分**很弱**，
因此三者能否進入擬合交由 stage 0 的 leverage 門檻決定，而不是假設。

**其三，stage 0 的存在是 NOTE-032 的教訓。** `_ROOM_LIGHT_RATIO` 當初
比例拉 25 倍、總能量只變 0.058% —— 一個「可以 fit、但 fit 出來由雜訊決定」
的參數。與其事後解釋，不如事前量：leverage 以 s_f 為單位，掃過整個登記
範圍造成的 observable 變化不到真實四分位距的一半（< 0.5）即 gauge-fix，
不擬合。門檻事前選定，落在 0.4–0.6 者必須標記 BORDERLINE 而非逕自歸類。

**其四，八個參數宣告不擬合，因為它們不是物理量。**
`spp` / `resolution` / `temporal_bins` / `max_depth` / `bounce_budget` /
`_LEADING_MARGIN_BINS` 是**數值與離散化設定**：把 spp 調高只是讓估計量的
變異變小，那不是「更像真實感測器」。用資料去 fit 它們會讓目標函數的改善
來自降噪而非來自物理。`foil_orientation` 只有一個朝向有實作，換朝向是
scene-level 變更；`distance_offset_mm` 是 CG-4 的 gauge 固定項。

**其五，NOT_CONVERGED 必須是合法結局，否則唯一的出路就是放寬門檻。**
這與 NOTE-035「選不出來也是合法結局」是同一條規則。評估上限用盡而未收斂時
記錄 best-so-far 但標記 NOT_CONVERGED，且 `calibrated_simulation` 不得凍結。

**驗證**：

```
py -3.10 -m pcmef.cli calibration preregister --validate
  CP-01..CP-12 全 PASS   26 項全部歸類：fit 18 / not_fitted 8
  protocol_hash 77365cc30413d4ea…
  preregistration freezable: YES
py -3.10 -m pytest tests/unit/test_calibration_prereg.py -q     # 36 passed
```

驗證器上線第一次跑就抓到本檔自身的三個真實缺口：
`distance_offset_mm` 同時被列在 stage 3 與 not_fitted（會被 fit 兩次）；
守衛清單自己含有 real class mean 字面值；artifact 欄位名混入中文說明
導致比對失去意義。三者都已修正 —— 這正是「規格要能被程式檢查」的意義。

另修掉驗證器自身的一個缺陷：`_as_floats()` 原本以 `float(v)` 救字串，
而 `float("1.0e6")` 會成功，於是 lock 內三個**字串**上界被安靜接受，
`declared_numeric_interpretations` 形同虛設。改為只接受原生數值型別後，
`test_cp03_fails_when_a_string_bound_has_no_declared_interpretation` 才真的會咬人。

**維護邊界**：
- 不得在讀過 calibration partition 之後修改預註冊；要改開 amendment 並新編號。
- 不得把四類 real class mean 寫進 `pcmef/` 或本預註冊檔（CP-04 會掃）。
- 不得讓 gauge 固定項或 estimator 參數進入搜尋空間（CP-02）。
- 不得放寬邊界；邊界必須與 `initial_simulation.lock` 逐字相同（CP-03）。
- 不得逐階段重算 `s_f`，也不得由模擬值計算 `s_f`。
- 不得在預註冊凍結前開始校準；CP-12 檢查尚無 calibration artifact。
- 本次**只做到預註冊凍結**：未執行 stage 0、未執行任何擬合、未開 held-out。

相關：[NOTE-035]、[NOTE-036]、[NOTE-031]、[NOTE-032]、[NOTE-034]、
[NOTE-037]、[NOTE-040]、[NOTE-024]、[NOTE-014]

---

## NOTE-040 已凍結 lock 的 metadata 缺陷以 append-only 勘誤更正，不刪除也不重凍

**決策日期**：2026-08-29

**適用範圍**：`pcmef/core/errata.py`（新增）；`pcmef/core/formal_loader.py`（新增）；
`configs/errata/ERR-001.yaml`（新增）；`freeze/errata/ERR-001.erratum.json`；
`pcmef/cli.py` 的 `erratum freeze` / `erratum status` / `locks resolve`。

**決策**：

1. NOTE-039 待裁決的兩條路取**保留並登記勘誤**。
   `freeze/initial_simulation.lock.json` **不刪除、不重凍**，一個位元都不動。
2. 新增 append-only 勘誤層。**ERR-001** 綁定原 lock 的
   `payload_hash = dc15c954…`，記錄 `environment.mitransient`
   recorded `"unavailable"` → corrected `"1.3.0"`，
   並宣告 `scientific_state_changed = false`。
3. formal 程式碼**不得**再直接呼叫 `LockStore.load()` 取用內容，
   一律走 `load_formal_lock()`；它每次載入都重驗
   **original lock + erratum + source evidence** 三者。
4. 勘誤可更正的欄位採**白名單**（目前只有 `environment.*`），
   並另設一層**禁區**涵蓋 parameter / estimator / seed / scene / config。
   禁區檢查先跑且不看白名單。
5. lock 的身分永遠是原始 `payload_hash`。勘誤**不產生**新的 lock hash。

**原因**：

**其一，刪除 lock 的代價不在這一份 lock，在先例。** 這份 lock 當時無任何下游
引用，刪掉它幾乎沒有直接成本 —— 但「凍結過的東西可以在不方便的時候消失」
一旦成立，後面每一個 lock 的可信度都要打折。而勘誤層的成本只是多一份記錄。

**其二，這個欄位不影響任何已算出的數字，而這一點是可檢查的而非宣稱的。**
`environment` 不進 `scene_hash`、不進 `parameter_set_hash`、不進
`surrogate_hash`，也不參與 IS-01..06 或 RP-01..04 的任何一項判定。
更正前後 RP-03 仍是 12/12 bitwise 相同、readiness 仍是六項全 PASS。
它的用途是讓後人**重建環境**：記錯讓重建指示失效，改正不改變任何結果。

**其三，「勘誤」與「不重跑就改科學」表面相同，必須在型別上分開。**
兩者都是「改一個已凍結的值」。差別在於前者改的是**對執行環境的描述**，
後者改的是**實驗本身**。因此禁區與白名單設計成成對鎖：
放寬白名單是一行改動，那一行不該足以讓改參數偽裝成修筆誤，所以禁區檢查
先跑、完全不看白名單，`test_forbidden_region_beats_the_allowlist` 盯著這一點。

**其四，證據必須綁回「被凍結的那一次 run」，不只是「某一次 run」。**
只比對 `dependencies.mitransient == "1.3.0"` 的話，任何一份用 1.3.0 跑出來的
manifest 都能當證據。因此每一條證據另帶 `binds` 斷言：
manifest 的 `content_hash` 必須等於 lock 的 `scene_hash`、
`run_identity_hash` 必須等於 `scene_topology.run_identity_hash`。
四條證據中另有兩條指向 scenario 層級的 `transient.mitransient_version`
（與 `dependencies` 分開寫入，不是同一個探測結果的複本）。

**驗證**：

```
py -3.10 -m pcmef.cli erratum freeze --spec configs/errata/ERR-001.yaml
  payload hash  : 363b06c1de6ec961…   evidence: 4 source(s), all verified
py -3.10 -m pcmef.cli locks resolve --lock initial_simulation \
    --field environment.mitransient
  payload_hash : dc15c9543a3aecac…   (original, unchanged)
  original  environment.mitransient = 'unavailable'
  resolved  environment.mitransient = '1.3.0'
py -3.10 -m pytest tests/unit/test_errata.py -q        # 31 passed
```

三次實際破壞確認負向測試會咬人（改完即還原）：讓
`FORBIDDEN_PATH_PREFIXES` 變空 → `test_forbidden_region_beats_the_allowlist`
FAIL；略過證據檔雜湊比對 → `test_tampered_evidence_file_is_refused` FAIL；
略過「原值必須符合 lock」→ `test_recorded_value_must_match_the_lock` FAIL。

**維護邊界**：
- 不得刪除或重凍任何 lock；更正一律走新的 erratum id。
- 不得放寬 `FORBIDDEN_PATH_PREFIXES`。
- 不得讓 `scientific_state_changed=true` 的記錄凍結成 erratum；
  那該走 amendment 或開新 run。
- 不得以更正後的 payload 重算並宣稱那是 lock 的 hash。
- 不得在證據檔缺失或雜湊不符時退回「就用原始值」繼續跑；
  fail-closed 是這一層唯一站得住腳的行為。

相關：[NOTE-039]、[NOTE-038]、[NOTE-028]、[NOTE-036]

---

## NOTE-039 initial_simulation.lock 的內容與「凍結當下重驗」規則

**決策日期**：2026-08-28

**適用範圍**：`pcmef/cli.py` 的 `freeze initial-simulation`；
`freeze/initial_simulation.lock.json`。

**決策**：

1. 凍結指令**重新執行** readiness 與 reproducibility 兩份稽核，
   不採信既有 artifact 的結論；任一項非 PASS 即 exit 2。
2. 工作區有未提交變更時**拒絕凍結**（除非顯式 `--allow-dirty`，
   且該情況會在 lock 內標記 `code_dirty_at_freeze`）。
3. lock 內容涵蓋十類：code commit、scene topology/version、registry/hash、
   initial parameter values、estimator config/hash、surrogate config/hash、
   seeds、integrator、environment manifest、amendment hashes。
4. lock 內含 `claim_boundary`，明寫這是 **pre-calibration** 模型。

**原因**：

**其一，lock 不可覆寫，因此前置條件必須在寫入的同一次執行中成立。**
「上次跑的時候是好的」不是凍結的依據 —— artifact 可能是三小時前、
不同 commit、不同 registry 下產生的。重跑成本只有幾秒（兩份稽核都不算圖），
換來的是 lock 與其證據同時成立。

**其二，一個宣稱 commit 的 lock 必須真的來自那個 commit。**
工作區髒的時候 `git rev-parse HEAD` 仍會給出一個 commit，但那個 commit
重建不出當下的程式。拒絕凍結是預設；若真有理由放行，
`code_dirty_at_freeze: true` 會留在 lock 裡，讓後人知道這份 lock
不能只靠 commit 重建。

**其三，`scene_hash` 取 `content_hash` 而非 `manifest_hash`。**
理由見 NOTE-038：後者涵蓋 wall-clock 與輸出路徑，用它當 lock 身分
會讓同一個場景每次凍出不同的值。

**其四，claim boundary 必須寫進 lock 本身而不只是 NOTE。**
26 個值仍是未校準的 placeholder/nuisance，這份 lock **不宣稱**模擬接近
真實分佈。它的用途是把起點固定下來，讓 calibration 無法悄悄移動它 ——
凍結的是「起點」，不是「正確性」。

**驗證**：

```
py -3.10 -m pcmef.cli audit initial-simulation      # PASS 6 FAIL 0 -> exit 0
py -3.10 -m pcmef.cli audit reproducibility \
    --run-a outputs/repro_a --run-b outputs/repro_b # PASS 4 FAIL 0 -> exit 0
py -3.10 -m pcmef.cli freeze initial-simulation
```

以不同內容重凍已由 `LockStore.write()` 的既有契約擋下（locks are immutable）。

**已知缺陷（首次凍結的 lock 帶有錯誤的環境記錄）**

首版 `freeze initial-simulation` 呼叫 `dependency_versions()` 在**freeze 行程**
內重新探測相依版本。freeze 行程沒有 `set_variant`，而 mitransient 在未設
variant 時 import 會拋例外，因此被記成 `"unavailable"` —— 但被凍結的那次 run
實際使用的是 **mitransient 1.3.0**（manifest 的 `dependencies` 與每個 scenario
的 `transient.mitransient_version` 都記著 1.3.0）。

`freeze/initial_simulation.lock.json`（payload_hash `dc15c9543a3aecac…`）
因此帶有一個**事實錯誤的 environment 欄位**。程式已修正為改讀
`manifest["dependencies"]`（被凍結那次 run 自己的記錄），但 lock 不可覆寫，
既有那一份無法就地更正。

處置**尚待裁決**，兩條路各有代價：
1. 刪除該 lock 重新凍結 —— 會拿到正確的 environment，但刪除 lock 本身
   違反「lock 不可覆寫」這條紅線，即使該 lock 只存在數分鐘且無任何下游引用。
2. 保留該 lock 並登記勘誤 —— 遵守紅線，但留下一份不足以重建環境的 lock。

在裁決之前**不得**逕自刪除。這一節存在的目的就是讓這個選擇被看見，
而不是被某一棒順手處理掉。

**維護邊界**：
- 凍結後**不得**再修改 initial model；要改就是新的 run。
- 不得為了通過而使用 `--allow-dirty`；那個旗標是為了記錄例外，不是繞過。
- 不得把 `parameter_ranges` 之外的參數交給 calibration 調整。
- environment 一律取自被凍結那次 run 的 manifest，**不得**在 freeze 行程
  重新探測；探測環境與執行環境不是同一個。

相關：[NOTE-036]、[NOTE-037]、[NOTE-038]、[NOTE-014]

---

## NOTE-038 run 身分不得涵蓋 wall-clock 與輸出路徑，並訂定可重現性容忍值

**決策日期**：2026-08-28

**適用範圍**：`pcmef/simulation/controller.py` 的 `_content_payload()`、
`_surrogate_identity()`、`content_hash`、`run_identity_hash`；
`pcmef/audit/reproducibility.py`（新增）；`pcmef/cli.py` 的
`audit reproducibility`。

**決策**：

1. 新增 `content_hash`：涵蓋 scenario 內容，**剝除** `runtime_s` 與 `outputs`。
2. `run_identity_hash` 改由 `content_hash` + `parameter_set_hash` +
   `surrogate_identity` 組成，不再引用 `manifest_hash`。
3. `manifest_hash` 定義維持不變（NOTE-030 的承諾），但**不參與 run 身分**。
4. 可重現性容忍值訂為 **bitwise 相同（0.0）**，非「近似」。

**原因**：

**其一，`manifest_hash` 在原理上不可重現，這是實測發現的。** 兩次相同輸入的
獨立執行：**12 個 .npy 全部 bitwise 相同**、seed/integrator/binning 全部相同，
但 `manifest_hash` 不同。逐欄位比對後，差異只有兩種：

| 欄位 | run A | run B |
|---|---|---|
| `rgb.runtime_s` | 0.1006 | 0.0613 |
| `transient.runtime_s` | 0.2163 | 0.2115 |
| `outputs.*` | `outputs/repro_a/…` | `outputs/repro_b/…` |

**沒有任何物理欄位不同。** 也就是說原本的 run 身分把「這次跑了多久、
存到哪裡」算進了「跑了什麼」。用它當凍結身分，等於保證每次凍結都得到
不同的答案 —— 那不是嚴格，是壞掉。

NOTE-030 曾寫「不得改變 manifest_hash 的涵蓋範圍」，理由是保護既有比對基準。
該理由在此不成立：它從來就沒有跨執行穩定過，因此沒有任何基準依賴它。
折衷做法是**保留原定義、另立 content_hash**，兩邊的承諾都不違背。

**其二，容忍值取 0.0 是量出來的結論，不是理想。** 在固定 seed、固定 spp、
固定 variant（`llvm_ad_rgb`）、同一台機器上，mitsuba/drjit 是決定性的，
實測 12/12 artifact bitwise 相同。既然實際做得到，容忍值就沒有理由放寬 ——
先訂一個寬鬆值再說「符合容忍」，會讓真正的非決定性永遠不被發現。

若日後換到會引入非決定性的後端（多執行緒 reduction 順序不固定的 GPU
variant），必須**先量測**其上界再據以放寬，並把量測寫進 NOTE。
**不得因為一次失敗就調大容忍值。**

**其三，surrogate 與 estimator 也是 run 身分的一部分。** 場景一模一樣但
estimator 換了，產出的四特徵就不同。因此 `surrogate_identity` 收錄
estimator、兩個可調參數、calibration hash 與 preregistration hash。

**驗證**：

```
py -3.10 -m pcmef.cli sim smoke --out outputs/repro_a
py -3.10 -m pcmef.cli sim smoke --out outputs/repro_b
py -3.10 -m pcmef.cli audit reproducibility --run-a outputs/repro_a --run-b outputs/repro_b
  RP-01 PASS  12 identity field(s) compared, all identical
  RP-02 PASS  4 scenario(s) x 9 field(s) compared, all identical
  RP-03 PASS  12/12 artifact(s) bitwise identical; tolerance all 0.0
  RP-04 PASS  estimator=LEADING_EDGE preregistration=172b82058460bdb0
  reproducibility: PASS        -> exit code 0
```

**維護邊界**：
- 不得把 `runtime_s` 或 `outputs` 放回 `content_hash`。
- 不得只比 manifest 而不比 `.npy`：manifest 相同而張量不同是最危險的情況，
  RP-03 存在的理由就是這個。
- 不得以「差不多」結案；非 bitwise 相同時必須寫出 max abs / max rel /
  相對能量差三個數字。

相關：[NOTE-030]、[NOTE-036]、[NOTE-037]

---

## NOTE-037 distance estimator 選定為 LEADING_EDGE，並記錄其 range walk

**決策日期**：2026-08-28

**適用範圍**：`pcmef/surrogate/distance.py` 的 `SELECTED_ESTIMATOR`；
`pcmef/surrogate/single_acquisition.py` 的 `SensorSurrogate.estimator` 預設值；
`configs/estimator_preregistration.yaml` v0.2.0；
`freeze/amendments/AMD-002.amendment.json`；
`outputs/estimator_select/estimator_selection.json`。

**決策**：

依 preregistration v0.2.0（含 AMD-002 的 S4）選定
**`LEADING_EDGE`**，可調參數 `detection_threshold_sigma = 5.0`、
`min_return_bins = 2`（**全類共用**）。

**原因**：

**其一，先回收 provenance，再決定 stage 3 能不能用。** 從採集當下的繪圖標題
取得一手證據：`Baseline` / `Shift left by 0.1 cm` / `Shift right by 0.1 cm`，
即位移為 **±1 mm**。**未**回收的是「移動的是感測器還是瓶子」—— 標題沒有主詞。

同一步同時證明錨點不能當 tie-break（三個理由詳見 AMD-002），其中最硬的一條是
**符號**：兩側都變小，而鏡像對稱場景的鏡像對稱位移做不到這件事。

**其二，S4 淘汰三個候選，且門檻不是關鍵。**

| 候選 | S1 幾何單調 | S4 最大變化（±5 mm 橫移） | 結果 |
|---|---|---|---|
| PEAK | +11.62 mm | **100.06 mm** | 淘汰 |
| ENERGY_CENTROID | **+16.96 mm**（超界） | 56.33 mm | 淘汰（S1+S4） |
| **LEADING_EDGE** | +9.70 mm | **0.00 mm** | **選定** |
| STRONGEST_RETURN_CENTROID | +12.21 mm | **100.54 mm** | 淘汰 |

門檻為 25 mm，而實測值是 0.00 對 56–101 —— 中間空了一個數量級，
**判定對門檻的選擇不敏感**，這正是 AMD-002 宣稱的性質。

`real_data_consulted: false`：stage 1/2 全程只用未校準場景的合成算圖，
沒有任何 real class mean、held-out 或 calibration split 進入選定程序。

**其三，選定的 estimator 有已知的 range walk，必須先寫下來。**
前緣觸發對回波振幅敏感：振幅越大，高斯前緣越早穿越固定門檻。
實測振幅 10 → 500（50 倍）時，估計距離變動 **10.0%**（89.97 → 80.97 mm）。

這是前緣式 ToF 的固有行為，不是缺陷，但它有兩個後果必須記住：
（a）`test_signal_is_not_derived_from_distance` 的距離容忍度因此由 5% 放寬到
20%，並改為斷言「Signal 的相對變化遠大於 Distance 的」；
（b）**calibration 期間任何改變回波振幅的參數**（箔片反射率、瓶壁粗糙度、
介質密度）**都會連帶移動 distance**。這是真實耦合，不得以「distance 只該由
幾何決定」為由把它消掉。

**驗證**：

```
py -3.10 -m pcmef.cli surrogate estimator-select --out outputs/estimator_select
  preregistration_hash 172b82058460bdb07f024fb78adc2315d2b682397b1aae6b6d1173771f50db6c
  outcome SELECTED / selected LEADING_EDGE / real_data_consulted false
py -3.10 -m pcmef.cli audit initial-simulation      # IS-04 PASS
```

**維護邊界**：
- 不得直接改 `SELECTED_ESTIMATOR` 而不重跑 selection；程式與選定證據分家時
  `RP-04` 會 FAIL。
- 不得因為 calibration 的結果回頭換 estimator。
- 兩個可調參數不得逐類設定。
- 論文措辭一律 **VL53L0X-inspired / VL53L0X-like**；ST 未公開最終 range 的
  產生方式，不得宣稱重現 internal algorithm。

相關：[NOTE-035]、[NOTE-036]、[NOTE-038]、AMD-002

---

## NOTE-036 initial_simulation 的凍結判準與 formal-run 防線刻意不同

**決策日期**：2026-08-28

**適用範圍**：`pcmef/audit/initial_simulation.py`（新增）；
`pcmef/cli.py` 的 `audit initial-simulation`；
`configs/parameter_registry.yaml` 的 `resolution.allowed_range`。

**決策**：

1. 新增獨立稽核 **IS-01..IS-06**，判定 `initial_simulation.lock` 可否凍結。
2. **不得**以 `params audit` 的 exit code 作為凍結判準。
3. IS-06 只要求「未校準值有明確狀態與搜尋邊界」，**不要求已校準**。
4. IS-04 不接受「尚未選定」的 estimator。

**原因**：

**其一，兩條線問的是不同問題，混用會讓 initial freeze 永遠不可能發生。**

| | `params audit` | `audit initial-simulation` |
|---|---|---|
| 問題 | 這組參數可否進 **formal run** | 這個 **pre-calibration 狀態**可否凍結 |
| 對 placeholder 的態度 | 阻塞 | **預期狀態** |
| 時序 | calibration **之後** | calibration **之前** |

`initial_simulation.lock` 的定義就是「校準開始前的起點」。若沿用 formal-run
的判準，就會要求「參數全部校準完才能凍結校準前的狀態」—— 循環。
本專案的 lock 相依鏈把 `initial_simulation` 放在
`calibrated_simulation` 之前，正是這個意思。

**其二，「未校準」與「未受管制」必須分開。** 前者可接受，後者不可。
IS-05 管的是後者（每個旋鈕都在 registry 內、綁得到程式、無漂移、
confounded group 全部裁決），IS-06 管的是前者的**表達方式**
（狀態明確、可校準者有搜尋邊界）。目前 26 個值仍未校準而 IS-06 PASS，
這是正確結果。

**其三，estimator 未定案時凍結等於凍了一個會變的東西。** estimator 是
surrogate 身分的一部分，`surrogate_hash` 會涵蓋它。因此 IS-04 只接受
`SELECTED`；`TIE_BREAK_REQUIRED` 與 `NO_ESTIMATOR_SELECTED` 都是 FAIL。

**驗證**：

```
py -3.10 -m pcmef.cli audit initial-simulation
  IS-01 PASS  IS-02 PASS  IS-03 PASS  IS-04 FAIL  IS-05 PASS  IS-06 PASS
  initial_simulation freezable: NO        -> exit code 2
```

IS-06 上線第一次跑就抓到一個真實缺口：`resolution` 是 `calibration_only`
卻 `allowed_range: null`，等於一個沒有搜尋邊界的可校準參數。已補為
**逐軸** [32, 512] 並在 note 說明它是每一軸的像素數而非 [width, height]。

**維護邊界**：
- 不得為了讓 IS-04 過關而在預註冊之外選定 estimator。
- 不得把「模擬距離接近真實分佈」加進本稽核；initial simulation
  **不要求**貼近真實，那是 calibration 之後的事。
- 凍結後本稽核的判準不得再更動。

相關：[NOTE-030]、[NOTE-034]、[NOTE-035]、[NOTE-014]

---

## NOTE-035 distance estimator 先預註冊再比較，且比較結果為「尚未選定」

**決策日期**：2026-08-28

**適用範圍**：`configs/estimator_preregistration.yaml`（新增）；
`pcmef/surrogate/estimator_selection.py`（新增）；
`pcmef/surrogate/distance.py` 的 `DistanceEstimator`／`find_returns`／
`map_distance`；`pcmef/cli.py` 的 `surrogate estimator-select`。

**決策**：

1. 候選 estimator、可調參數、判準與**套用順序**，在跑任何比較之前凍結。
   預註冊檔以**獨立 commit** 先進版控，commit 內不含任何結果。
   `preregistration_hash = 27ff55e2332ba8f7…`
2. 判準只有三種來源：physics、synthetic sanity、獨立 offset anchors。
   四類 real class mean 明列為**禁止輸入**。
3. 本次比較結果為 **TIE_BREAK_REQUIRED**，estimator **尚未選定**。
4. stage 3 經評估**不可執行**，理由見下。不得因此放寬門檻重跑。

**原因**：

**其一，先註冊後比較必須在 git 歷史上看得出來。** 一份和結果同時出現的
「判準」無法與事後合理化區分。因此預註冊單獨 commit（`68b21eb`），
其內容不含任何候選的實測數字。

**其二，硬門檻確實淘汰了東西，而且淘汰的正是最誘人的那一個。**

| 候選 | P1-P4 | S1 幾何單調 | S2 增益不變 | S3 四類可算 | 結果 |
|---|---|---|---|---|---|
| PEAK | ok | +11.62 mm | 0.0 | ok | **存活** |
| ENERGY_CENTROID | ok | **+16.96 mm** | 0.0 | ok | **淘汰** |
| LEADING_EDGE | ok | +9.70 mm | 0.0 | ok | **存活** |
| STRONGEST_RETURN_CENTROID | ok | +12.21 mm | 0.0 | ok | **存活** |

S1 把 `sensor_to_bottle_mm` 由 50 增到 60 mm（純幾何掃描），要求估計距離
增加 5–15 mm。`ENERGY_CENTROID` 增加 16.96 mm —— 它跟著整條波形的一階矩跑，
不是在量距離。**這正是先前被標記為陷阱的那個候選**：它在 Bubbly／Misty 上
看起來最接近真實均值。用預註冊的物理判準淘汰它，與「因為它看起來準所以選它」
是相反方向的兩件事。

**其三，stage 3 不可執行，這是實測結論不是藉口。** 偏移錨點為
normal 98.53 / left 90.91 / right 88.73 mm，即 Δleft = **−7.62**、
Δright = **−9.80** mm。三個獨立問題：

1. **自變數量級對不上。** `configs/base.yaml` 把兩側記為 `±0.1 cm`，
   即 ±1 mm。瓶半徑 28.5 mm 上橫移 1 mm 的弓形高只有
   `28.5 − sqrt(28.5² − 1²) = 0.018 mm`，比觀測到的 7.6–9.8 mm 小**三個數量級**。
2. **符號不可能。** 兩側**都變小**。對凸面前表面而言，橫移只會讓最近點變遠，
   不可能兩側都變近。場景本身鏡像對稱，物理上產生不出這個型態。
3. **模擬在該位移下沒有鑑別力。** 實測橫移 ±1 mm 時，三個存活候選的估計值
   變化**全部為 0.00 mm**（低於 3.12 mm 的 distance bin 寬）。

因此偏移錨點在目前的場景模型下無法區分這三個候選。強行使用它，只能靠
挑一個能讓某候選勝出的位移量 —— 那就是預註冊要防的事。

**其四，順帶量到一個尚未列入判準的不穩定性。** 橫移 +5 mm 時 `PEAK` 由
145.88 跳到 45.82 mm（−100.06），`STRONGEST_RETURN_CENTROID` 同樣跳
−101.06；橫移 −5 mm 則幾乎不動（+1.70 / +1.15）。場景鏡像對稱卻得到
不對稱結果，代表這不是幾何響應而是 **path family 之間的模式切換** ——
箔片家族與前玻璃家族能量接近，取樣雜訊決定誰是全域最大。
**本次不得用它選 estimator**：它不在預註冊的判準內，事後拿來用就是
發明新規則。記錄於此供未來 amendment 引用。

**驗證**：

```
py -3.10 -m pcmef.cli surrogate estimator-select --out outputs/estimator_select
  preregistration_hash 27ff55e2332ba8f7...
  survivors: PEAK / LEADING_EDGE / STRONGEST_RETURN_CENTROID
  outcome: TIE_BREAK_REQUIRED     -> exit code 2
  real_data_consulted: false
```

**維護邊界**：
- 不得在比較之後修改 `configs/estimator_preregistration.yaml`；
  要改判準必須開 amendment 並重跑。
- 不得以「反正三個裡面挑一個」為由隨意選定；未選定就是未選定。
- 解除路徑有二：(a) 取得偏移量測試的實際位移與符號慣例，使 stage 3 可執行；
  (b) 開 amendment 新增一條**物理性**判準（例如 path-family 穩定性），
  理由必須獨立於本次已知的結果。

相關：[NOTE-034]、[NOTE-036]、[NOTE-028]、[NOTE-014]

---

## NOTE-034 Ambient 改由獨立 ambient pass 取得，並與 VCSEL 功率解耦

**決策日期**：2026-08-28

**適用範圍**：`pcmef/simulation/mitsuba_adapter.py` 的 `Illumination`、
`_ROOM_LIGHT_RADIANCE`、`build_scene_dict()`；
`pcmef/simulation/mitransient_adapter.py` 的雙 pass 算圖；
`pcmef/simulation/ambient_audit.py`（新增）；
`pcmef/surrogate/features.py` 的 `out_of_window_energy`／`ambient_energy`；
`pcmef/surrogate/ambient.py`；`configs/parameter_registry.yaml` 的 `CG-3_ambient`。

**決策**：

1. 場景新增 `Illumination` 三態；每次 acquisition 算**兩個 pass**：
   `ACTIVE_ONLY`（VCSEL on／室內光 off）與 `AMBIENT_ONLY`（VCSEL off／室內光 on），
   共用同一組 binning 與 seed。
2. **Ambient Rate 只由 ambient pass 取得**；缺 ambient pass 時直接拒絕產出，
   不退回舊行為。
3. `background_energy` 更名 `out_of_window_energy`，只供 SNR 與 multipath 使用。
4. `_ROOM_LIGHT_RATIO` 更名 `_ROOM_LIGHT_RADIANCE` 並與 `lighting.irradiance`
   **解耦**；預設值下數值完全相同（1.0 × 0.02 = 0.02）。
5. CG-3 由 BLOCKED 改判 **RESOLVED**：固定 `_ROOM_LIGHT_RADIANCE`（gauge），
   校準 `ambient_energy_to_mcps`。

**原因**：

**其一，舊定義量的不是 ambient。** `background_energy = total − 主窗` 的內容
實測 99.97%（Empty）／99.996%（Water）是 VCSEL 自己的多重反射，
室內光只佔 0.033%／0.004%（NOTE-032）。名字叫 background，量的是 multipath。
更名是為了讓下一個人看到欄位名就知道它不是 ambient —— 這個誤解已經
花掉整整一輪。

**其二，室內光不該綁在 VCSEL 功率上。** 舊式 `radiance = irradiance × ratio`
等於宣稱「把雷射調亮，房間就跟著變亮」。除了物理上錯誤，它還讓
「改變 VCSEL 功率不應等比改變 Ambient」這條驗收條件**在結構上不可能成立**。
解耦後數值刻意保持不變，因此這次改動只解開耦合，不改變任何既有結果。

**其三，缺 ambient pass 時必須拒絕，不能退回舊行為。** 一個算得出來但量錯
東西的 Ambient，比一個算不出來的 Ambient 危險得多 —— 前者不會有任何症狀。

**驗證** —— `sim ambient-check`（Empty，32×32／spp=16／128 bins）：

| 檢查 | 量到的 |
|---|---|
| A1 室內光遞增 → Ambient 單調遞增 | radiance [0.005, 0.02, 0.08, 0.32] → [0.9008, 3.6034, 14.4134, 57.6537]；增益線性度 **4.0003 / 4.0000 / 4.0000** |
| A2 VCSEL 功率 1.0→4.0 | ambient **3.603355 → 3.603355**（完全相同）；active 38675.7 → 154702.9（×4.0000） |
| A3 VCSEL 關閉 | 主窗內 ambient 殘留 **恰為 0**（殘留比 0.000e+00） |
| A4 室內光關閉 | ambient 能量 **恰為 0** |

A1 的線性度證實 `_ROOM_LIGHT_RADIANCE` 是 Ambient 上的**精確純增益**，
與 `ambient_energy_to_mcps` 精確簡併；A2 證實 `lighting.irradiance` 對
Ambient 的影響**恰為 0**，因此它已不屬於 CG-3。本組由三項降為兩項精確簡併，
依 CG-2 同一套理由固定 scene 端、校準 MCPS 端（NOTE-031）。

**維護邊界**：
- 不得再由 active pass 的主窗外能量推導 Ambient，任何形式都不行。
- 不得把 `_ROOM_LIGHT_RADIANCE` 接回 `lighting.irradiance`；
  A2 會立刻 FAIL。
- 不得放寬 A3/A4 的容忍值；A4 要求**恰為零**，非零代表有第三個光源漏進
  ambient pass。
- 本節的通過只表示 Ambient 與 Signal 分得開，**不表示** Ambient 數值
  接近真實感測器讀值。

相關：[NOTE-026]、[NOTE-031]、[NOTE-032]、[NOTE-005]

---

## NOTE-033 角度慣例實測：fov 是全角、cutoff_angle 是半角，且硬編值一律參數化

**決策日期**：2026-08-28

**適用範圍**：`pcmef/simulation/mitsuba_adapter.py` 的 `_SENSOR_FOV_DEG`、
`_LIGHT_CUTOFF_ANGLE_DEG`、`_MAX_DEPTH`、`_FOIL_ORIENTATION` 與
`_foil_transform()`；`configs/parameter_registry.yaml` 的 `sensor.fov_deg`、
`light.cutoff_angle_deg`、`foil_orientation`、`max_depth`。

**決策**：

1. 把 `build_scene_dict()` 內 dict literal 的四個字面值提升為具名模組常數。
   **數值一個都沒有改。**
2. `sensor.fov_deg = 45` **不改成 25**，維持 UNKNOWN 且 formal-blocking。
3. `_foil_transform()` 只實作 `facing_camera`，其餘取值一律拒絕而非退回預設。
4. 兩個角度的慣例差異寫進檔頭維護提醒與 registry note。

**原因**：

**其一，硬編值不是任何防線看得見的東西。** 這四個量都會決定四特徵，卻不是
具名常數，因此 `parameter_registry` 的 live-value 綁定抓不到它們 ——
registry 可以宣稱 `sensor.fov_deg = 45`，程式改成 60 也不會有任何症狀。
參數化之後 `live_value_drift()` 才有對象可比。

**其二，「45 對 25 差近一倍」這句話裡的兩個數字慣例不同。** 這是本次實測結果，
不是查文件：

| 量 | 設定值 | 實測 | 慣例 |
|---|---|---|---|
| perspective `fov` | 45 | 單邊半角 **22.4959°**、對角 **30.3562°** | **全角**，預設綁 x 軸 |
| spot `cutoff_angle` | 25 | 24.9° 仍有輻射、25.1° 為 **0** | **半角**，即 50° 全角 |

量法：對 sensor 以 `sample_ray()` 取film 中心與邊緣的射線夾角；對 spot 以
`sample_direction()` 掃離軸角度找輻射歸零處。非方形 film（128×64）時 x 半角
維持 22.4959° 而 y 降為 11.6986°，證實 `fov` 綁 x 軸。
spot 在錐內的衰減恰為 `cos²θ`（24.9° 時 0.8227 = cos²24.9°），
即純平方反比、無角度衰減，因此 25° 是硬邊界而非柔化起點。

於是有兩個直接後果：
- 「光源比感測器窄」是錯覺。照明錐 **50° 全角** 其實**寬於**接收視野 45° 全角。
- 把 45 改成 25 會把接收半角壓到 12.5°，而瓶身對相機的張角是
  `asin(28.5/78.5) = 21.3°`（NOTE-026）—— 瓶身會超出視野邊界，
  這是一個場景層級的改變，不是「修正一個筆誤」。

**其三，25° 這個目標值本身沒有一手來源。** SRC-PLAN 與 SRC-HANDOFF 都沒有記載
VL53L0X 的 FoV，也沒有記載它是 full-cone 還是 half-angle。拿一個慣例不明的
數字去改一個慣例已知的數字，只會把不確定性藏進場景裡。
**因此本項維持 UNKNOWN 且 formal-blocking**，解除條件寫在 registry：
取得 ST 資料手冊對 25° 的定義，並裁決「方形 film 的角落 30.36°」
要如何對應到圓錐 FoV。

**其四，未實作的箔片朝向必須拒絕而不是近似。** SRC-HANDOFF §0 把箔片
orientation 列為未回收。若 `_foil_transform()` 對未知取值悄悄退回
`facing_camera`，「刻意選了正對相機」與「還沒實作傾角」就長得一樣 ——
這正是 NOTE-005 要避免的那種混淆。

**驗證**：

```
py -3.10 -m pytest tests/unit/test_parameter_registry.py -v
py -3.10 -m pcmef.cli params audit          # sensor.fov_deg[UNKNOWN] 仍在阻塞清單
```

角度量測腳本見本次 session 的 `probe_optics.py`（結果如上表）。
`live_value_drift()` 對 30 個 code-resident 參數回報 0 筆漂移，
其中包含本次新增的四個具名常數。

**維護邊界**：
- 不得因為「VL53L0X 是 25°」就把 `_SENSOR_FOV_DEG` 改成 25；先取得慣例定義。
- 不得把兩個角度的慣例統一「順手改一改」：改任一個都會改變哪些回波被收集，
  屬場景層級變更，須先有 provenance 再動。
- 不得為 `_FOIL_ORIENTATION` 新增取值卻不實作對應幾何。

相關：[NOTE-026]、[NOTE-029]、[NOTE-030]、[NOTE-005]

---

## NOTE-032 Ambient 通道量到的是雷射多重反射，CG-3 因此裁決為 BLOCKED

**決策日期**：2026-08-28

**適用範圍**：`pcmef/surrogate/features.py` 的 `background_energy`；
`pcmef/surrogate/ambient.py`；`configs/parameter_registry.yaml` 的
`CG-3_ambient`、`_ROOM_LIGHT_RATIO`、`ambient_energy_to_mcps`。

**決策**：

1. **CG-3 裁決為 BLOCKED**，不給任何成員預設值，不做部分校準。
2. 阻塞理由記為**觀測量定義錯誤**，而非「證據不足」。
3. 解除條件寫入 registry 的 `unblock_requires`，共三步。

**原因**：

NOTE-026 當時的判斷是「改成 monostatic 後場景只剩感測器發光，`ambient_rate`
會恆為 0」，據此加了一盞 `constant` 室內光。**該前提在現行場景下不成立，
而且加室內光沒有修好真正的問題。** 實測（Empty 與 Water-filled，
64×64／spp=16／128 bins，室內光比例 0.0 / 0.02 / 0.5 三點）：

| | Empty | Water-filled |
|---|---|---|
| `background_energy`（室內光**關閉**） | **26408.47** | **200336.76** |
| `background_energy`（室內光 0.02，現行值） | 26417.22 | 200344.72 |
| 室內光佔比 | **0.033%** | **0.004%** |
| 雷射多重反射佔比 | **99.967%** | **99.996%** |
| 純 ambient pass（移除 spot）總能量 | **14.17** | **12.32** |

三件事同時成立：

**其一，ambient 從來不是 0。** 室內光完全關閉時 `background_energy` 仍有
26408（Empty 總能量的 7%）。因為它的定義是
`total − 主窗能量` —— 而主窗外那些能量是**感測器自己打出去的光**經箔片與
內側介面回來的多重反射。NOTE-026 預測的「恆為 0」沒有發生，
所以加室內光解決的不是它宣稱要解決的問題。

**其二，真正的 ambient 訊號比它小三到四個數量級。** 移除 spot 只留室內光時，
整條 transient 的總能量只有 14.17 / 12.32，與 `background_energy` 相差
**1864 倍 / 16262 倍**。目前 Ambient 這一欄實際上是多重反射強度的代理量。

**其三，`_ROOM_LIGHT_RATIO` 因此不可辨識。** 它對 Ambient 輸出的槓桿只有
萬分之三；把比例從 0 拉到 0.5（25 倍於現值）也只讓總能量變動 0.058%。
在這個觀測量上校準它，得到的數字會收斂，但收斂到的是多重反射的大小。

還有一個結構性的旁證：純 ambient pass 只填滿 **23/128** 個 bin，
前六個 bin 恰為 0。真正的環境光 DC 速率應該均勻鋪滿整條時間軸；
`constant` environment emitter 在 transient 中只透過打到幾何再回來的路徑
出現，本來就不是 DC pedestal。

**因此這不是「再多量一點就能定」的證據不足，是校準目標定義錯誤。**
CG-3 的簡併結構其實可解（irradiance 已由 CG-2 固定，剩下兩項簡併固定其一即可），
但在觀測量修正之前，固定哪一項都沒有意義。給任何一個數值都等於把一個
不可辨識的量寫成已知 —— 那正是 registry 存在的理由。

**驗證**：

```
py -3.10 -m pcmef.cli params audit    # CG-3_ambient 顯示 BLOCKED 與三條解除條件
py -3.10 -m pytest tests/unit/test_parameter_registry.py -k blocked -v
```

上表數字由本次 session 的 `probe_ambient.py` 產出。

**維護邊界**：
- 不得因為 Ambient 欄「有數字、有類別差異」就當作它可用；
  那些差異來自多重反射，不是環境光。
- 不得為了讓 CG-3 變成 RESOLVED 而只固定 `_ROOM_LIGHT_RATIO`；
  觀測量沒改，固定誰都一樣。
- 修正觀測量時不得直接把 `background_energy` 減掉一個估計的多重反射量；
  那是用另一個未校準的量去修一個未校準的量。正解是獨立的 ambient pass。

相關：[NOTE-026]、[NOTE-030]、[NOTE-031]

---

## NOTE-031 CG-1／CG-2 以 gauge fixing 裁決，並新增受限的 CONVENTION 狀態

**決策日期**：2026-08-28

**適用範圍**：`configs/parameter_registry.yaml` 的 `CG-1_extinction`、
`CG-2_brightness`、`_SIGMA_T_REFERENCE_PER_M`、`lighting.irradiance`、
`_FOIL_REFLECTANCE_940NM`；`pcmef/core/parameters.py` 的 `GAUGE_STATUS`、
`sanctioned_gauges()`、`gauge_violations()`、`_validate_decision()`。

**決策**：

| 組 | 裁決 | 固定 | 校準 |
|---|---|---|---|
| CG-1 消光係數 | **RESOLVED** | `_SIGMA_T_REFERENCE_PER_M` | 三個密度 |
| CG-2 絕對能量 | **RESOLVED** | `lighting.irradiance` | `signal_energy_to_mcps` |

並新增 provenance 狀態 `CONVENTION`：**只有**在該參數是某個 RESOLVED
confounded group 的 `fixed` 成員、且該組寫明 `claim_boundary` 時才放行。

**原因**：

**其一，CG-2 的組成原本就是錯的，必須先更正。** registry 原記載
irradiance × foil_reflectance × energy_to_mcps「三者相乘」。實測推翻：

| 變動 | 總能量比 | 正規化波形逐 bin 差 |
|---|---|---|
| irradiance 1.0 → 2.0 | **2.0000** | **0.0（恰為零）** |
| foil 0.85 → 0.425 | 0.983 (Empty) / 0.768 (Water) | L1 **0.020 / 0.166** |

`irradiance` 是**精確的全域增益**，與 `signal_energy_to_mcps` 完全簡併 ——
波形裡不存在任何能分辨兩者的資訊。而 `_FOIL_REFLECTANCE_940NM` 只縮放箔片
那一支回波，會改變波形**形狀**（變化集中在 147.4 / 162.6 mm 的箔片 bin），
因此在原理上可與全域增益分離。**它不屬於這一組**，已移出並改為獨立的
calibration-only 參數。三項簡併其實是「兩項精確簡併 + 一項可分離」。

**其二，固定項的選擇不能用「哪個比較好 fit」決定。** 同組內固定任一項在
數值上都可行，因此判準只能是 provenance 與可解釋性：

*CG-1* —— SRC-SAI §9 的 scenario.yaml 草案把 `bubble_density` 標為
`<validation-frozen range>`，也就是規格本身把**密度**定位為由 validation
決定的量；規格從未提及「參考尺度」，那是本實作為了換算單位而引入的。
把規格指名要校準的量固定起來、去校準一個規格沒有的量，方向相反。
另外固定的必須是 shared 那一項：固定 `turbidity` 會讓「水有多濁」成為定義，
並使其餘兩類的 σt 都相對於一個關於水的任意選擇而定，把三類耦合在一起。
最後，`_SIGMA_T_REFERENCE_PER_M = 100.0` 的來源本身就證明它不是量測 ——
程式註解明寫「選 100 是為了讓密度 0.1–0.3 得到光學厚度 τ≈0.6–1.7」，
它是**與密度一起挑出來湊出某個 τ 範圍**的。一個由挑選產生的數字沒有資格
當校準結果，但完全有資格當 gauge。

*CG-2* —— mitsuba spot 的 `intensity` 單位是 W/sr，而 SRC-HANDOFF 未回收任何
VCSEL 光功率規格，這個絕對值沒有一手來源可對。反之 `signal_energy_to_mcps`
的定義**就是**「模擬能量換算成 MCPS」，任意的輻射尺度本來就該落在這個換算
係數上，因為 MCPS 才是可觀測、可校準的那一端。把尺度留在 scenario yaml
另有實務風險：yaml 逐 run 變動，全域亮度會在 run 之間無聲漂移。

**其三，gauge fixing 與「偷給預設值」的差別必須是機器可檢查的，不能靠自律。**
兩者表面上都是「把一個 PLACEHOLDER 固定下來」。差別在於 gauge 對應的是一個
**在原理上不可辨識**的自由度 —— 它沒有物理真值，固定它是選座標系。
因此 `CONVENTION` 設計成一把**配對鎖**：

- 單獨把某個 PLACEHOLDER 改標 `CONVENTION` **不會**放行，
  `gauge_violations()` 會指名擋下；
- 必須有一個 RESOLVED 的 group 指名它為 `fixed`；
- 該 group 必須寫出 `claim_boundary`，否則 schema 在載入時就拒絕。

`claim_boundary` 是這筆交易的代價，必須寫得下來才算成立：CG-1 的代價是
校準後的三個密度**不得**被解讀為絕對濁度／密度，可宣稱的只有 σt 本身；
CG-2 的代價是 `lighting.irradiance = 1.0` **不得**被引用為 VCSEL 實際發射
功率，校準後的 `signal_energy_to_mcps` 也只是相對值。

**驗證**：

```
py -3.10 -m pcmef.cli params audit
  CG-1_extinction  [RESOLVED] fixed=['_SIGMA_T_REFERENCE_PER_M']
  CG-2_brightness  [RESOLVED] fixed=['lighting.irradiance']
py -3.10 -m pytest tests/unit/test_parameter_registry.py -k "convention or resolved or group" -v
```

反向驗收（實測會失敗，不是宣稱）：把 `gauge_violations()` 改成一律回空，
`test_convention_without_a_sanctioning_group_fails` 與
`test_convention_needs_the_group_to_be_resolved_not_blocked` 兩條立即 FAIL。

**維護邊界**：
- 不得把 `CONVENTION` 加進 `FORMAL_ELIGIBLE_STATUSES`；它的放行必須繼續
  依賴 group 的授權，否則就成了繞過防線的萬用鑰匙。
- 不得在沒有 `claim_boundary` 的情況下把任何 group 標成 RESOLVED。
- 固定值變動時 `parameter_set_hash` 必然改變，須開新 run，不得就地重凍。
- 論文措辭不得出現「校準得到的水濁度為 X」或「VCSEL 發射功率為 Y」。

相關：[NOTE-030]、[NOTE-032]、[NOTE-005]、[NOTE-028]

---

## NOTE-030 parameter registry 由說明文件升為 formal firewall

**決策日期**：2026-08-28

**適用範圍**：`pcmef/core/parameters.py`（新增）；
`pcmef/surrogate/calibration.py` 的 `assert_formal_ready()`／`with_calibrated()`／
`OPTICAL_PATH_TO_DISTANCE`；`pcmef/simulation/scenario.py` 的 formal 建構；
`pcmef/simulation/controller.py` 的 manifest；`pcmef/cli.py` 的 `params audit`；
`tests/unit/test_parameter_registry.py`。

**決策**：

1. `configs/parameter_registry.yaml` 由資料層接上**實際會擋人的**防線：
   `ScenarioConfig(formal=True)` 與
   `SurrogateCalibration.assert_formal_ready()` 兩處都轉呼叫
   `core.parameters.assert_formal_ready()`。
2. 新增 `parameter_set_hash()`，涵蓋每個參數的
   name/source/value/status/kind/scope/formal_blocking/allowed_range/confounded_with
   與每一組 confounded group 的**裁決內容**；**不涵蓋** `role`/`note` 散文欄位。
3. 新增 `live_value_drift()`：逐項比對 registry 宣稱值與程式碼此刻真正在用的值。
4. simulation manifest 記錄 registry 摘要與 `parameter_set_hash`，
   並新增 `run_identity_hash = f(manifest_hash, parameter_set_hash)`。
5. `optical_path_to_distance` 改標 `derived=True`、移出可校準集合，
   `with_calibrated()` 拒絕覆寫它。

**原因**：

**其一，一份沒有被拿去對照程式的 registry 只是第二份文件。** 它會漂移，
而且漂移時沒有任何症狀 —— registry 寫 `sensor.fov_deg = 45`、程式改成 60，
不會有任何測試失敗。`live_value_drift()` 讓這件事變成可偵測的。
綁定涵蓋率也必須誠實回報而不是湊成 100%：目前 **30 項綁到程式常數、
8 項由 scenario yaml 逐 run 供應、0 項未綁定**，三種狀態分開列。

**其二，`assert_formal_ready()` 必須一次收齊全部理由。** 遇到第一個問題就
返回，會讓交接的人以為修掉它就過了 —— 而實際上還有二十幾條。
目前實測回報 **27 個 formal-blocking 參數 + CG-3 BLOCKED**。

**其三，`manifest_hash` 的涵蓋範圍不夠，但也不能就地擴大。** 現行
`manifest_hash` 只涵蓋 scenario 內容；把 27 個未校準建模常數全部換掉，
scenario 內容可以一個位元都不變 —— 那正是要防的靜默漂移。但直接擴大它的
定義會讓既有 E1-G03 證據的比對基準整批失效。因此**另立** `run_identity_hash`
同時涵蓋兩者，`manifest_hash` 的意義保持不變。

**其四，`optical_path_to_distance` 是 derived 而不是 calibration 常數。**
共置幾何使光程恰為單程距離的兩倍，係數由幾何得 0.5（NOTE-026）。
先前它被標成 placeholder，於是同時有兩個錯：formal 模式因為它而擋
（理由是錯的），而且它在型別上仍是一個可被 calibration 寫入的旋鈕 ——
SRC-SAI §10 明令禁止拿它去逼近真實均值，但**一句註解攔不住任何人**。
新增的 `derived` 旗標與 `with_calibrated()` 的拒絕才是實質防線。

**其五，順帶修掉一個 provenance 不實。** `TransientResult.extra["integrator"]`
先前硬編字串 `"transient_path"`，但帶參與介質的場景實際使用
`transient_prbvolpath`（NOTE-026 的自動選擇）。四個 smoke 場景中有三個的
manifest 記載了它們沒有用過的積分器。manifest 是 provenance，**記錯比不記更糟**。

**驗證**：

```
py -3.10 -m pcmef.cli params audit
  parameters 38 / unresolved 27 / bindings code=30 config=8 unbound=0
  live value drift 0 / formal-ready NO        -> exit code 2
py -3.10 -m pytest tests/unit/test_parameter_registry.py -q     # 28 passed
```

反向驗收（三次實際破壞，皆確認會 FAIL 而非只是宣稱）：

| 破壞 | 失敗的測試 |
|---|---|
| `FORMAL_ELIGIBLE_STATUSES` 加入 PLACEHOLDER/UNKNOWN/RECONSTRUCTED | `test_unresolved_status_fails` 三個參數化案例 |
| `gauge_violations()` 一律回空 | `test_convention_without_a_sanctioning_group_fails` 等兩條 |
| `with_calibrated()` 不再擋 derived | `test_optical_path_to_distance_is_derived_not_calibratable` |

`test_a_clean_registry_passes` 刻意保留：少了它，其餘負向測試會在
「防線永遠拒絕一切」的情況下全部通過，等於什麼都沒驗到。

**維護邊界**：
- 不得用 `check_registry=False` 讓 formal 路徑略過檢查；該參數只為單元測試存在。
- 不得為了消除 drift 而修改 registry 的 `value` 欄；drift 代表程式與登記
  不一致，要查的是哪一邊錯。
- 不得新增建模常數而不登記；`binding_coverage()` 的 `unbound` 必須維持為空，
  對應測試會失敗。
- 不得改變 `manifest_hash` 的涵蓋範圍。

相關：[NOTE-031]、[NOTE-032]、[NOTE-033]、[NOTE-026]、[NOTE-005]、[NOTE-028]

---

## NOTE-029 canonical physical scene 只放真實存在的物件；RGB 背景不得進 ToF 光路

**決策日期**：2026-08-28

**適用範圍**：`pcmef/simulation/mitsuba_adapter.py` 的 `build_scene_dict()`、
`scene_path_bounds()`、`foil_center_z()`、`_foil_transform()`、
`_FOIL_*` 與 `_INTERIOR_BASE_IOR`。

**決策**：

1. **移除 `backdrop`**（reflectance 0.5 的漫反射矩形），改建
   **far-side aluminum foil reflector**（`roughconductor`, material Al）。
2. **內圓柱一律建立，包含 Empty**。空瓶因此是「殼＋空氣＋殼」而非實心玻璃柱。
3. 內部基底折射率依類別語意決定：`empty/misty → air`、`water/bubbly → water`。
4. 時間窗的場景跨距改由箔片位置推得，不再依賴已移除的 backdrop。

**原因**：

**其一，把 backdrop 對 ToF 隱藏不是解法。** 若該物件不是實驗中的物理物件，
它就不該存在於 canonical physical scene —— 否則會變成
「RGB 看得到一個不存在的物體，ToF 看不到」，paired simulation 的物理一致性
無法辯護。因此場景分成兩層：canonical physical scene（sensor / bottle shell /
interior medium / far-side reflector）與 RGB presentation（環境照明）。
RGB 的背景由 `room_light`（`constant` environment emitter）提供 ——
它沒有幾何，不會形成有限距離的回波。

原 backdrop 在 SRC-PLAN 與 SRC-HANDOFF 都找不到依據，卻在 ToF 通道
佔掉 **47–55%** 的能量。

**其二，箔片的「結構已知」與「數值未知」必須分開。**

| 面向 | 狀態 |
|---|---|
| 反射體存在、材質為鋁箔、位於瓶身另一側 | **provenance-supported**（SRC-HANDOFF §0 把其光學參數列為「未回收」，反證物件存在） |
| offset / orientation / roughness / BRDF / 940 nm 有效反射率 | **UNKNOWN，calibration-only nuisance** |

`_FOIL_GAP_TO_BOTTLE_RATIO = 1.0` 是**幾何慣例**（一個瓶半徑），
不是量出來的，也不是為了得到任何特定距離。四個 `_FOIL_*` 常數
**一律不得依類別而異**。

**其三，Empty 的實心玻璃柱是拓樸錯誤，不是未校準參數。**
先前 `bottle_interior` 只在 `medium_preset != "empty"` 時建立，
於是 Empty 的 `bottle_wall`（半徑 28.5 mm 的 bk7 圓柱）成為**實心玻璃**，
光要穿過 57 mm 玻璃。空瓶裡面是空氣這件事不需要校準，因此直接修，
與「調係數去貼真實均值」是兩回事。

| Empty 遠側表觀距離 | 值 |
|---|---|
| 錯誤（實心玻璃 57 mm） | 136.46 mm |
| 正確（2 mm 殼＋53 mm 空氣＋2 mm 殼） | 109.07 mm |

**其四，Misty 的基底不該是水。** 霧是懸浮在空氣中的液滴，氣泡是水中的氣體。
先前所有非 Empty 類一律用 `int_ior: water`，對 Misty 是錯的。
這是類別語意（SRC-SAI FR-002）決定的**材質類別**，不是可調數值。

**驗證** —— bounce lineage 以 `max_depth` 遞增取得，非位置比對：

```
export PATH="/c/Program Files/LLVM/bin:$PATH"
py -3.10 -m pcmef.cli sim smoke --config configs/simulation/smoke.yaml \
    --out outputs/scene_v2
```

| 成分 | 首次出現的 max_depth | 交互作用次數 | 身分 |
|---|---|---|---|
| 44.9 mm | **2** | 1 | **前玻璃單次反射**（該深度佔 98%） |
| 60.2 mm | 5 | ~4 | 內側介面（玻璃↔內容物） |
| 146–162 mm | 12 | ~10 | **箔片**（穿過整個瓶子來回） |

`max_depth=2` 只允許 sensor → 一個表面 → sensor，因此 45 mm 成分的
front-glass 身分是 **tracing 證據**，不再是位置吻合的推測。

**修正後四類都出現箔片回波**（Empty 147.4 / Water 162.6 / Bubbly 162.6 /
Misty 145.7 mm），即存在**四類共同的 far-side return family** ——
這是修正前不存在的。

**維護邊界**：
- 不得把任何純視覺需求的幾何放回 canonical physical scene。
  RGB 需要背景就用 environment emitter，不要用會進光路的實體。
- 四個 `_FOIL_*` 常數不得依類別而異，不得為了讓 distance 貼近真實均值而挑選；
  它們與 NOTE-026／NOTE-027 的建模常數同屬 formal firewall 的已知缺口。
- 不得因為「Empty 沒有介質」就省略內圓柱；那正是實心玻璃柱錯誤的來源。
- 本節的峰值位置**不構成任何 fidelity 主張**：箔片位置與光學常數皆未校準。

相關：[NOTE-026]、[NOTE-027]、[NOTE-013]

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
py -3.10 -m pcmef.cli admin serve            # http://localhost:8790/console
docker compose up console                    # http://localhost:8790
```
實機驗收：在瀏覽器按下「開始模擬」，log 逐行出現、結束時徽章由 running
轉 succeeded、重新整理後四條 transient 曲線與能量長條圖正確呈現；
四類能量 3908.9 / 3898.9 / 3879.0 / 3875.9 與 CLI、Docker 三處完全一致。

**維護邊界**：
- 不得在 console 新增任何會寫 lock 或帶 `--formal` 的端點。
- 不得把 log 用 `innerHTML` 附加。伺服器輸出含使用者可控的檔名與錯誤字串，
  必須以 `createTextNode` 附加，否則就是 XSS；
  `test_the_live_log_is_appended_as_text_not_html` 守住這一條。
- Docker 的主機埠與本機 CLI **同為 8790**（2026-08-31 起，原為 8787 / 8801）。
  改動原因：8787 在開發機上被另一個常駐服務長期佔用，而 Flask 撞埠的錯誤
  只出現在終端機 —— 瀏覽器看到的是**佔用者**回的頁面（實測是一個 503），
  症狀因此看起來像「PC-MEF 壞了」，實際上它根本沒起來。
  兩邊同埠的代價是不能同時開；真要同時對照時用
  `PCMEF_CONSOLE_PORT=8791 docker compose up console`。

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
