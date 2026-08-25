# PC-MEF Research System — 進度真相

本檔是進度的唯一真相來源。聊天訊息裡的說明不算完成。

最後更新：2026-08-25

---

## 目前狀態

| 里程碑 | 狀態 | 說明 |
|---|---|---|
| M0 Data Audit | **BLOCKED** | 前研究原始資料不在本機（見 NOTE-008） |
| M1 Simulation | 未開始 | 需先安裝 mitsuba / drjit |
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
| Batch 2 | LegacyCSVAdapter + nominal/usable count ledger + alignment | 未開始 |
| Batch 3 | Sigma/timing provenance resolver | 未開始 |
| Batch 4 | Mitsuba/mitransient optical transient smoke adapter | 未開始 |
| Batch 5 | single-acquisition surrogate + 500-point temporal model | 未開始 |
| Batch 6 | E1 metrics + dual-lock + scientific rule state machine | 未開始 |
| Batch 7 | E1-G01..G12 audit + heldout firewall + real split policy audit | 未開始 |
| Batch 8 | post-E1 split generators + parent-family checks | 未開始 |

---

## Batch 1 已完成內容（2026-08-25）

測試：**288 passed**（`py -3.10 -m pytest`）

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

## 待教授裁決事項（formal-blocking，共 17 項）

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

對應 SRC-PLAN Appendix A 的六個教授討論題目，其中第 1、5 題直接決定上表的
Real split 與 E2 兩組數值。

---

## 環境阻塞

| 項目 | 狀態 | 影響里程碑 |
|---|---|---|
| 前研究原始 ToF CSV / RGB 影像 | **不在本機**（已掃描 Desktop/Documents/Downloads） | M0、E1 全部 |
| `mitsuba` / `drjit` | 未安裝 | M1、M2 |
| `tensorflow` / `scikit-learn` | 未安裝 | M4、M5 |
| `jsonschema` | 未安裝 | M6 agent schema 驗證 |

Python 執行環境：`py -3.10`（3.10.11，numpy 2.2.6 / scipy 1.15.3 / pandas 2.3.3
/ PyYAML 6.0.3 / pytest 9.0.3 已就緒）。
注意 PATH 上的 `python` 指向 3.12 且缺相依，一律使用 `py -3.10`。

---

## 下一步

1. **Batch 2**：`adapters/legacy_csv.py` + `pcmef audit real-data`，
   以合成 fixture 完成全部 FAIL 路徑測試（真實資料到位後不需改碼）。
2. 取得前研究原始資料，或確認其存放位置。
3. 向教授確認上表 17 項數值中至少 Real split 與 E2 兩組。
