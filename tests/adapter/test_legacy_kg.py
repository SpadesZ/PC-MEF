# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 內合成 KG 彙總矩陣，
#         操作 adapters.legacy_kg；不依賴 data/raw_real/ 是否存在。
# 檔案路徑: tests/adapter/test_legacy_kg.py
# 產生時間: 2026-08-26 04:40 +08:00
# 版本: v0.1.0
# 功能說明: 驗證彙總格式 reader 能正確把 32 個矩陣重組成每筆 recording 的四特徵，
#           並在缺檔、形狀不一致、欄位標題異常時記進排除帳而不是靜默補值。
# 模組定位: NOTE-011 彙總來源的驗收測試。它同時鎖住一條研究邊界：
#           此格式的 e1_eligible 永遠為 0。
# 主要責任:
#   1. _make_kg() 合成 4 類 x 4 metric x 2 統計量的矩陣檔
#   2. 正常路徑：32 檔 -> 560 recordings、每筆 (n_windows, 4)
#   3. 欄序必須依 TOF_SCHEMA，不得依檔名掃描順序
#   4. 缺 metric、窗口數不一致、欄位標題異常的排除路徑
#   5. e1_eligible 恆為 0 的邊界
# 維護提醒:
#   - 不得放寬 e1_eligible 恆為 0 的斷言；彙總統計量不含 500 點序列，
#     宣稱它 e1-eligible 等於讓 E1 在錯誤的資料契約上進行（NOTE-011）。
#   - 不得為缺檔的 condition 以其他類別的矩陣代替。
#   - v0.1.0 新增：首版彙總 reader 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/adapter/test_legacy_kg.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pcmef.adapters.base import ExclusionReason
from pcmef.adapters.legacy_kg import (
    KG_METRIC_TO_CANONICAL,
    LegacyKGAdapter,
    LegacyKGError,
)
from pcmef.core.constants import TOF_SCHEMA

CONDITIONS = ("nowater", "water", "bubble", "smoke")
KG_METRICS = tuple(KG_METRIC_TO_CANONICAL)


def _make_kg(
    root: Path, n_windows: int = 99, n_recordings: int = 140, skip=None
) -> Path:
    """合成 KG_<class>_<Metric>_<Stat>.csv；skip=(cond, metric, stat) 可缺一檔。"""
    root.mkdir(parents=True, exist_ok=True)
    for condition in CONDITIONS:
        for metric_index, metric in enumerate(KG_METRICS):
            for stat in ("Mean", "Std"):
                if skip == (condition, metric, stat):
                    continue
                base = (metric_index + 1) * 10.0 + (0.5 if stat == "Std" else 0.0)
                data = np.full((n_windows, n_recordings), base)
                data += np.arange(n_recordings) * 1e-3
                frame = pd.DataFrame(
                    data, columns=[f"No.{i}" for i in range(1, n_recordings + 1)]
                )
                frame.to_csv(root / f"KG_{condition}_{metric}_{stat}.csv", index=False)
    return root


# ---------------------------------------------------------------------------
# 正常路徑
# ---------------------------------------------------------------------------


def test_complete_source_yields_one_recording_per_column(tmp_path):
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path))
    recordings, exclusions, counts = adapter.audit_alignment(inventory)

    assert counts.physical_source_files == 32          # 4 類 x 4 metric x 2 統計量
    assert counts.canonical_recordings == 560          # 4 x 140
    assert counts.valid_recordings == 560
    assert exclusions == ()
    assert len(recordings) == 560


def test_each_recording_carries_four_metric_columns(tmp_path):
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path, n_windows=99))
    recordings, _, _ = adapter.audit_alignment(inventory)
    first = recordings[0]
    assert first.window_mean.shape == (99, len(TOF_SCHEMA))
    assert first.window_std.shape == (99, len(TOF_SCHEMA))
    assert first.n_windows == 99


def test_metric_columns_follow_canonical_schema_order(tmp_path):
    """欄序必須依 TOF_SCHEMA，不得依檔案掃描順序（字母序會是 Ambient 先）。"""
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path))
    recordings, _, _ = adapter.audit_alignment(inventory)
    means = recordings[0].window_mean.mean(axis=0)
    # _make_kg 讓第 i 個 KG metric 的基準值為 (i+1)*10，而 KG_METRIC_TO_CANONICAL
    # 的宣告順序與 TOF_SCHEMA 一致，因此欄均值必須嚴格遞增。
    assert list(np.argsort(means)) == [0, 1, 2, 3]


def test_class_labels_are_mapped_from_conditions(tmp_path):
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path, n_recordings=2))
    recordings, _, _ = adapter.audit_alignment(inventory)
    labels = {r.class_label for r in recordings}
    assert labels == {"Empty", "Water-filled", "Bubbly", "Misty"}


# ---------------------------------------------------------------------------
# 研究邊界
# ---------------------------------------------------------------------------


def test_aggregated_source_is_never_e1_eligible(tmp_path):
    """NOTE-011：彙總統計量不含 500 點序列，永遠不得標為 e1-eligible。"""
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path))
    _, _, counts = adapter.audit_alignment(inventory)
    assert counts.e1_eligible_recordings == 0


def test_adapter_exposes_no_tof_sequence_attribute(tmp_path):
    """命名上就不該讓下游把窗口統計量誤用為 500 點序列。"""
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path, n_recordings=1))
    recordings, _, _ = adapter.audit_alignment(inventory)
    assert not hasattr(recordings[0], "tof_sequence")


# ---------------------------------------------------------------------------
# 排除路徑
# ---------------------------------------------------------------------------


def test_missing_metric_file_excludes_the_whole_condition(tmp_path):
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(
        _make_kg(tmp_path, skip=("smoke", "Sigma_mm", "Std"))
    )
    recordings, exclusions, counts = adapter.audit_alignment(inventory)

    assert {r.condition for r in recordings} == {"nowater", "water", "bubble"}
    missing = [e for e in exclusions if e.reason is ExclusionReason.MISSING_METRIC]
    assert len(missing) == 1
    assert missing[0].identifier == "smoke"
    assert "sigma_like/Std" in missing[0].detail["missing"]
    assert counts.canonical_recordings == 420


def test_window_count_mismatch_is_excluded(tmp_path):
    root = _make_kg(tmp_path)
    # 讓 water 的一個矩陣少幾列，模擬窗口數不一致。
    path = root / "KG_water_Distance_mm_Mean.csv"
    pd.read_csv(path).iloc[:90].to_csv(path, index=False)

    adapter = LegacyKGAdapter()
    recordings, exclusions, _ = adapter.audit_alignment(adapter.build_inventory(root))
    assert "water" not in {r.condition for r in recordings}
    assert any(e.reason is ExclusionReason.ROW_COUNT_MISMATCH for e in exclusions)


def test_unexpected_column_headers_are_excluded(tmp_path):
    root = _make_kg(tmp_path, n_recordings=3)
    path = root / "KG_bubble_Sigma_mm_Mean.csv"
    frame = pd.read_csv(path)
    frame.columns = ["a", "b", "c"]
    frame.to_csv(path, index=False)

    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(root)
    assert any(
        e.reason is ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY
        for e in inventory.exclusions
    )


def test_unknown_filename_is_recorded(tmp_path):
    root = _make_kg(tmp_path, n_recordings=2)
    (root / "KG_unknownclass_Distance_mm_Mean.csv").write_text(
        "No.1\n1.0\n", encoding="utf-8"
    )
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(root)
    assert any(
        "unknown_condition" in e.detail for e in inventory.exclusions
    )


def test_missing_source_root_is_rejected(tmp_path):
    with pytest.raises(LegacyKGError, match="not found"):
        LegacyKGAdapter().build_inventory(tmp_path / "absent")


def test_directory_without_kg_files_is_rejected(tmp_path):
    tmp_path.joinpath("empty").mkdir()
    with pytest.raises(LegacyKGError, match="no parsable KG matrices"):
        LegacyKGAdapter().build_inventory(tmp_path / "empty")


# ---------------------------------------------------------------------------
# 取值
# ---------------------------------------------------------------------------


def test_metric_values_returns_per_condition_arrays(tmp_path):
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path, n_windows=10, n_recordings=5))
    values = adapter.metric_values(inventory, "sigma_like", "Mean")
    assert set(values) == set(CONDITIONS)
    assert all(v.size == 50 for v in values.values())


def test_metric_values_rejects_unknown_metric(tmp_path):
    adapter = LegacyKGAdapter()
    inventory = adapter.build_inventory(_make_kg(tmp_path, n_recordings=1))
    with pytest.raises(LegacyKGError, match="unknown metric"):
        adapter.metric_values(inventory, "temperature")
