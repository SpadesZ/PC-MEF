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
