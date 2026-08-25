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

## 決策記錄

見 `NOTES.md`。程式中任何 `NOTE(NOTE-NNN)` 標記在該檔都有同號完整條目，
包含決策、理由與套用方式。
