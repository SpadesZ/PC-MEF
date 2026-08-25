# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 sim smoke 呼叫；讀 configs/simulation/*.yaml，
#         驅動 mitsuba_adapter 與 mitransient_adapter，
#         寫出 artifact 與 simulation_smoke_manifest.json（E1-G03 證據）。
# 檔案路徑: pcmef/simulation/controller.py
# 產生時間: 2026-08-26 08:05 +08:00
# 版本: v0.1.0
# 功能說明: 把一份場景設定跑完整套模擬 —— 算 RGB、算 transient、記錄兩者共用的
#           場景識別碼與所有版本資訊，失敗時也把錯誤留成可稽核的檔案而不是消失。
# 模組定位: 模擬層的編排者。它不決定物理參數，也不做校準；
#           本批次只負責證明「同一場景能穩定產出兩種輸出」。
# 主要責任:
#   1. ScenarioRun 保存單一場景的執行狀態與產物
#   2. SimulationController.run_scenario() 依序算 RGB 與 transient 並捕捉例外
#   3. SimulationController.write_manifest() 產生 E1-G03 的 smoke manifest
#   4. dependency_versions() 收集 mitsuba/mitransient/drjit 版本供 freeze 引用
# 維護提醒:
#   - 不得在 render 失敗時更換 seed 重試；SRC-SAI §28 規定只能以相同
#     config/seed 重試，換 seed 等於偷換實驗條件。
#   - 不得把失敗的場景從 manifest 中略去；FAILED 與其錯誤訊息本身就是稽核結論。
#   - 不得以本批次的產物宣稱任何 physics fidelity；材質參數尚未校準，
#     claim boundary 由 E1-G12 管制。
#   - v0.1.0 新增：首版 smoke controller。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_simulation.py -k "manifest or smoke_run or four_classes" -v
#   - py -3.10 -m pcmef.cli sim smoke --config configs/simulation/smoke.yaml
# ------------------------------------------------------------

from __future__ import annotations

import json
import platform
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.core.hash import hash_object
from pcmef.simulation.mitransient_adapter import MiTransientAdapter, TransientResult
from pcmef.simulation.mitsuba_adapter import (
    DEFAULT_VARIANT,
    MitsubaAdapter,
    RenderResult,
)
from pcmef.simulation.scenario import ScenarioConfig

__all__ = ["ScenarioStatus", "ScenarioRun", "SimulationController"]


class ScenarioStatus:
    """場景執行狀態。刻意用字串常數而非列舉，讓 manifest 直接可讀。"""

    OK = "OK"
    FAILED = "FAILED"


@dataclass
class ScenarioRun:
    """單一場景的執行結果。"""

    scenario_id: str
    scenario_hash: str
    status: str
    rgb: RenderResult | None = None
    transient: TransientResult | None = None
    error: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "status": self.status,
        }
        if self.rgb is not None:
            payload["rgb"] = self.rgb.to_dict()
        if self.transient is not None:
            payload["transient"] = self.transient.to_dict()
        if self.error:
            payload["error"] = self.error
            payload["stderr"] = self.stderr
        return payload


def dependency_versions(variant: str = DEFAULT_VARIANT) -> dict[str, str]:
    """收集模擬相依的版本。任一缺失都以 'unavailable' 記錄而非拋例外。"""
    versions: dict[str, str] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "variant": variant,
    }
    for name in ("mitsuba", "drjit", "mitransient"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "unknown")
        except Exception:  # noqa: BLE001
            versions[name] = "unavailable"
    return versions


class SimulationController:
    """場景執行的編排者。"""

    def __init__(self, variant: str = DEFAULT_VARIANT) -> None:
        self.variant = variant
        self.mitsuba = MitsubaAdapter(variant)
        self.mitransient = MiTransientAdapter(variant)

    def run_scenario(
        self,
        scenario_id: str,
        config: ScenarioConfig,
        out_root: str | Path,
        temporal_bins: int = 256,
    ) -> ScenarioRun:
        """跑完一個場景。任何例外都轉成 FAILED 並保留 traceback。

        RGB 與 transient 共用同一個 scenario_hash —— SRC-SAI FR-005 要求
        兩者必須來自同一 scenario，hash 相同是這件事的可驗證證據。
        """
        scenario_dir = Path(out_root) / scenario_id
        run = ScenarioRun(
            scenario_id=scenario_id,
            scenario_hash=config.scene_hash(),
            status=ScenarioStatus.OK,
        )
        try:
            if config.render_rgb:
                run.rgb = self.mitsuba.render_rgb(config, scenario_dir)
            if config.render_transient:
                run.transient = self.mitransient.render_transient(
                    config, scenario_dir, temporal_bins=temporal_bins
                )
        except Exception as error:  # noqa: BLE001
            run.status = ScenarioStatus.FAILED
            run.error = f"{type(error).__name__}: {error}"
            run.stderr = traceback.format_exc()
            scenario_dir.mkdir(parents=True, exist_ok=True)
            (scenario_dir / "stderr.txt").write_text(run.stderr, encoding="utf-8")
        return run

    def write_manifest(
        self,
        runs: list[ScenarioRun],
        out_dir: str | Path,
        filename: str = "simulation_smoke_manifest.json",
    ) -> Path:
        """寫出 E1-G03 的 smoke manifest。

        manifest 同時收錄成功與失敗的場景；只記成功的那份不叫 manifest，
        叫挑好看的結果。
        """
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        scenarios = [run.to_dict() for run in runs]
        manifest = {
            "gate": "E1-G03",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dependencies": dependency_versions(self.variant),
            "counts": {
                "total": len(runs),
                "ok": sum(1 for r in runs if r.status == ScenarioStatus.OK),
                "failed": sum(1 for r in runs if r.status == ScenarioStatus.FAILED),
            },
            "scenarios": scenarios,
            "claim_boundary": (
                "Smoke only. Material and medium parameters are not calibrated; "
                "these artifacts must not be used to claim any physics fidelity "
                "(see E1-G12 claim boundary)."
            ),
        }
        manifest["manifest_hash"] = hash_object(scenarios)

        path = target_dir / filename
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path
