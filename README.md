# PC-MEF Research System

結合物理校準模擬與大型語言模型輔助多模態融合之管內液態狀態辨識
（Intra-Pipe Liquid-State Identification Integrating Physics-Calibrated
Simulation and Large Language Model-Assisted Multimodal Fusion）

碩士論文實驗平台。核心是兩個實驗：

- **E1** — 校準後 simulation 是否比 initial simulation 更接近 frozen held-out real
- **E2** — 在 paired synthetic stress scenarios 中，Adaptive PC-MEF (G5) 是否比
  Reliability Fusion (G4) 更穩定

## 規格來源

| ID | 文件 |
|---|---|
| `SRC-PLAN` | PC-MEF 實驗計畫 v0.9.7S — Evidence-Integrity Hardened Thesis Core |
| `SRC-SAI` | PC-MEF SAI v0.5.0 — LLM Setup / Task Binding Integrated |

兩份文件位於 `Desktop\pre碩論\正式可用\正式實驗`。
本 repo 不重寫論文內容，只把規格轉成可執行、可 freeze 的系統。

## 設計原則

CLI-first / local-first / provenance-first / truth-firewall /
real-split-lock / scientific-outcome / hard-freeze / paired-formal integrity。

三條非協商的紅線：

1. **未核定數值不得補預設值。** 所有待教授裁決的數字以 `!required` 標記，
   讀到即拋 `FormalBlockingError`。見 `NOTES.md` NOTE-005。
2. **Provider 只能看到 opaque evidence。** class / condition / severity /
   parent_scene_family / 檔名路徑一律不得進入 InferencePayload。
   見 `NOTES.md` NOTE-003、NOTE-004。
3. **Lock 不可覆寫、不可跳步。** 22 個 formal lock 有明確前置順序，
   內容變更必須開新 run。

## 環境

```powershell
py -3.10 -m pip install -e ".[dev]"
```

分組相依（依里程碑逐步安裝，避免早期批次被重依賴卡住）：

```powershell
py -3.10 -m pip install -e ".[simulation]"   # M1/M2: mitsuba, drjit
py -3.10 -m pip install -e ".[perception]"   # M4/M5: tensorflow, scikit-learn
py -3.10 -m pip install -e ".[agents]"       # M6: httpx, jsonschema
py -3.10 -m pip install -e ".[admin]"        # LLM Setup 管理頁: Flask
```

PATH 上的 `python` 指向 3.12 且缺相依，請一律使用 `py -3.10`。

## 常用指令

```powershell
py -3.10 -m pytest                              # 全部測試
py -3.10 -m pcmef.cli config check              # 列出待教授裁決的數值
py -3.10 -m pcmef.cli --formal config show      # formal 模式（有缺值即 exit 2）
py -3.10 -m pcmef.cli locks status              # 各 formal lock 的凍結狀態與前置條件
```

## 目前進度

見 `STATUS.md`。摘要：Batch 1（core schema + truth firewall + freeze 機制）
已完成，146 項測試通過；M0 因前研究原始資料不在本機而 BLOCKED（NOTE-008）。

## 檔頭與決策記錄規範

每個原始檔開頭都有十欄位維護契約，欄位順序固定：
`上下游／檔案路徑／產生時間／版本／功能說明／模組定位／主要責任／維護提醒／驗證方式`。
其中 `功能說明`（白話在做什麼）、`模組定位`（架構位置與邊界）、
`主要責任`（編號職責清單）三者分開寫，不合併。

決策層級的理由不寫檔頭，寫 `docs/NOTES.md`，每則條目五欄齊備：
`決策日期／適用範圍／決策／原因／驗證`。程式中的 `NOTE(NOTE-NNN)` 必須能在該檔
找到同號條目，禁止失效引用。

這兩條規範由 `tests/test_repo_integrity.py` 自動稽核：欄位缺漏、順序錯誤、
`檔案路徑` 與實際位置不符、`驗證方式` 指向不存在的檔案、NOTE 引用找不到條目、
NOTE 條目缺欄位、NOTE 編號重用 —— 全部會讓 CI 失敗。

```powershell
py -3.10 -m pytest tests/test_repo_integrity.py -v
```
