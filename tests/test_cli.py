# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 tmp_path 產生暫時 config 與 freeze 目錄後呼叫
#         pcmef.cli.main()；驗證 stdout 內容與 exit code，不寫出常駐 artifact。
# 檔案路徑: tests/test_cli.py
# 產生時間: 2026-08-25 23:05 +08:00
# 版本: v0.1.0
# 功能說明: 驗證命令列的四組指令行為正確 —— 特別是 formal 模式遇到未核定數值時
#           必須以非零 exit code 中斷，而不是印個警告就繼續跑下去。
# 模組定位: CLI 契約的驗收測試。它不驗證 config 與 lock 的內部邏輯
#           （那由 tests/unit/test_config_and_locks.py 負責），只驗證進入點行為。
# 主要責任:
#   1. test_version_command_succeeds() 驗證基本進入點可用
#   2. test_config_check_lists_pending_values() 驗證待裁決清單會被列出
#   3. test_formal_mode_exits_non_zero() 驗證 formal-blocking 反映在 exit code
#   4. test_locks_status_reports_blocked_prerequisites() 驗證狀態機可視化
#   5. test_cli_override_is_rejected_in_formal_mode() 驗證 formal 不接受覆蓋
# 維護提醒:
#   - 不得把 formal 模式的預期 exit code 改成 0；非零是 CI 判定 formal-blocking
#     的唯一依據。
#   - 不得在測試中使用 repo 的 configs/base.yaml 以外的真實路徑做寫入操作。
#   - 新增子指令時要在此補一條最小 smoke 案例。
#   - v0.1.0 新增：首版 CLI 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/test_cli.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcmef.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG = REPO_ROOT / "configs" / "base.yaml"


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "experiment.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_version_command_succeeds(capsys):
    assert main(["version"]) == 0
    assert "pcmef" in capsys.readouterr().out


def test_config_check_lists_pending_values(capsys, tmp_path):
    config = _write_config(
        tmp_path,
        "gate:\n  alpha: !required\n    source: SRC-SAI 16\n    reason: pending\n",
    )
    assert main(["--config", str(config), "config", "check"]) == 0
    out = capsys.readouterr().out
    assert "gate.alpha" in out
    assert "await advisor approval" in out


def test_config_check_reports_a_fully_resolved_config(capsys, tmp_path):
    config = _write_config(tmp_path, "gate:\n  alpha: 0.4\n")
    assert main(["--config", str(config), "config", "check"]) == 0
    assert "All config values are resolved." in capsys.readouterr().out


def test_formal_mode_exits_non_zero_when_values_are_pending(capsys, tmp_path):
    """formal-blocking 必須反映在 exit code，否則 CI 抓不到。"""
    config = _write_config(tmp_path, "gate:\n  alpha: !required\n")
    assert main(["--config", str(config), "--formal", "config", "check"]) == 2


def test_shipped_base_config_is_still_formal_blocking(capsys):
    """repo 內的 base.yaml 目前必然有待裁決數值；若哪天變成 0 代表有人偷填了值。"""
    assert BASE_CONFIG.exists()
    assert main(["--config", str(BASE_CONFIG), "--formal", "config", "show"]) == 2


def test_cli_override_is_rejected_in_formal_mode(tmp_path):
    config = _write_config(tmp_path, "gate:\n  alpha: 0.4\n")
    assert (
        main(["--config", str(config), "--formal", "--set", "gate.alpha=0.9",
              "config", "show"])
        == 1
    )


def test_cli_override_works_outside_formal_mode(capsys, tmp_path):
    config = _write_config(tmp_path, "gate:\n  alpha: 0.4\n")
    assert (
        main(["--config", str(config), "--set", "gate.alpha=0.9",
              "config", "show", "--key", "gate.alpha"])
        == 0
    )
    assert "0.9" in capsys.readouterr().out


def test_malformed_override_is_rejected(tmp_path):
    config = _write_config(tmp_path, "gate:\n  alpha: 0.4\n")
    assert main(["--config", str(config), "--set", "no-equals-sign", "config", "show"]) == 1


def test_missing_config_file_is_reported(tmp_path):
    assert main(["--config", str(tmp_path / "absent.yaml"), "config", "show"]) == 1


def test_locks_status_reports_blocked_prerequisites(capsys, tmp_path):
    assert main(["locks", "status", "--freeze-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "real_split_policy" in out
    assert "BLOCKED" in out
    assert "waiting on real_split_policy" in out


@pytest.mark.parametrize("argv", [["config"], ["locks"], ["audit"]])
def test_subcommand_group_without_action_is_rejected(argv):
    with pytest.raises(SystemExit):
        main(argv)


# ---------------------------------------------------------------------------
# audit real-data（M0）
# ---------------------------------------------------------------------------


def _make_source(root: Path, per_class: int = 2, drop=None) -> Path:
    """合成 <condition>/<metric>/*.csv；drop=(condition, folder, n) 可刻意缺一檔。"""
    import numpy as np
    import pandas as pd

    for condition in ("nowater", "water", "bubble", "smoke"):
        for metric_index, folder in enumerate(
            ("distance", "ambient", "signal", "sigma")
        ):
            for n in range(1, per_class + 1):
                if drop == (condition, folder, n):
                    continue
                path = root / condition / folder / f"{folder}_{n:03d}.csv"
                path.parent.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(
                    {
                        "timestamp": np.arange(500) * 0.082,
                        "value": np.full(500, (metric_index + 1) * 10.0)
                        + np.arange(500) * 1e-3,
                    }
                ).to_csv(path, index=False)
    return root


def test_audit_real_data_writes_all_m0_artifacts(capsys, tmp_path):
    source = _make_source(tmp_path / "raw")
    out = tmp_path / "inventory"
    assert main(["audit", "real-data", "--source", str(source), "--out", str(out)]) == 0

    for artifact in (
        "source_inventory.csv",
        "measurement_alignment.csv",
        "exclusion_ledger.csv",
        "audit_report.json",
    ):
        assert (out / artifact).exists(), artifact

    report = json.loads((out / "audit_report.json").read_text(encoding="utf-8"))
    assert report["counts"]["physical_source_files"] == 32
    assert report["counts"]["canonical_recordings"] == 8
    assert report["counts"]["valid_recordings"] == 8


def test_audit_reports_nominal_separately_from_actual(tmp_path):
    """SRC-SAI §7.9：nominal logical count 不得被當成 usable N。"""
    source = _make_source(tmp_path / "raw")
    out = tmp_path / "inventory"
    main(["audit", "real-data", "--source", str(source), "--out", str(out)])
    counts = json.loads((out / "audit_report.json").read_text(encoding="utf-8"))["counts"]
    assert counts["nominal_logical_recordings"] == 560
    assert counts["canonical_recordings"] == 8
    assert counts["nominal_logical_recordings"] != counts["canonical_recordings"]


def test_audit_excludes_only_the_affected_measurement(tmp_path):
    """NOTE-010：缺一個檔只該掉一筆，不得讓後續整批錯位。"""
    source = _make_source(tmp_path / "raw", per_class=5, drop=("smoke", "sigma", 2))
    out = tmp_path / "inventory"
    main(["audit", "real-data", "--source", str(source), "--out", str(out)])

    report = json.loads((out / "audit_report.json").read_text(encoding="utf-8"))
    assert report["aligned_by_class"]["Misty"] == 4
    assert report["aligned_by_class"]["Empty"] == 5
    assert report["exclusions_by_reason"]["missing_metric"] == 1

    alignment = (out / "measurement_alignment.csv").read_text(encoding="utf-8")
    # 被排除的是 smoke 的第 2 筆；其他 condition 的 sigma_002 不受影響，
    # 因此必須比對完整路徑而不是檔名。
    assert "smoke/sigma/sigma_002.csv" not in alignment
    assert "nowater/sigma/sigma_002.csv" in alignment
    # smoke 的其餘四筆完好，尤其是被排除那筆「之後」的編號。
    for n in (1, 3, 4, 5):
        assert f"smoke/sigma/sigma_{n:03d}.csv" in alignment


def test_audit_defaults_to_unresolved_sigma_and_blocks_e1_eligibility(capsys, tmp_path):
    """M0 盤點本身就是產生 Sigma 證據的步驟，預設不得宣稱已解析。"""
    source = _make_source(tmp_path / "raw")
    out = tmp_path / "inventory"
    main(["audit", "real-data", "--source", str(source), "--out", str(out)])
    report = json.loads((out / "audit_report.json").read_text(encoding="utf-8"))
    assert report["sigma_status"] == "UNRESOLVED"
    assert report["counts"]["e1_eligible_recordings"] == 0
    assert "E1-G08" in capsys.readouterr().err


def test_audit_with_resolved_sigma_reports_eligibility(tmp_path):
    source = _make_source(tmp_path / "raw")
    out = tmp_path / "inventory"
    main([
        "audit", "real-data", "--source", str(source), "--out", str(out),
        "--sigma-status", "RESOLVED",
    ])
    report = json.loads((out / "audit_report.json").read_text(encoding="utf-8"))
    assert report["counts"]["e1_eligible_recordings"] == 8


def test_audit_on_missing_source_returns_error_code(tmp_path):
    assert (
        main([
            "audit", "real-data",
            "--source", str(tmp_path / "absent"),
            "--out", str(tmp_path / "out"),
        ])
        == 1
    )
