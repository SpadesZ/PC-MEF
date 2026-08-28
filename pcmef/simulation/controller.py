# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 sim smoke 呼叫；讀 configs/simulation/*.yaml，
#         驅動 mitsuba_adapter 與 mitransient_adapter，
#         寫出 artifact 與 simulation_smoke_manifest.json（E1-G03 證據）。
# 檔案路徑: pcmef/simulation/controller.py
# 產生時間: 2026-08-26 08:05 +08:00
# 版本: v0.3.0
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
#   - 不得從 manifest 移除 parameter_registry 區塊，也不得在 registry 讀不到時
#     靜默略過它；一份不知道自己用了哪組參數的 artifact 無法支持任何主張。
#   - 不得改變 manifest_hash 的涵蓋範圍（僅 scenario 內容，含 runtime/paths）；
#     它保留原義以免既有引用失效，但**不參與 run 身分**。
#   - 不得把 runtime_s 或 outputs 放回 content_hash：它們描述「在哪裡跑、
#     跑多久」，放進去會讓 run 身分在原理上不可重現（NOTE-038 實測）。
#   - v0.1.0 新增：首版 smoke controller。
#   - v0.2.0 manifest 記錄 parameter_registry 摘要與 parameter_set_hash，
#     並新增 run_identity_hash = f(manifest_hash, parameter_set_hash)（NOTE-030）。
#   - v0.3.0 新增 content_hash（剝除 runtime_s/outputs）與 surrogate_identity；
#     run_identity_hash 改由 content_hash + parameter_set_hash + surrogate 組成。
#     原因：實測兩次相同輸入的執行，12 個 .npy 全部 bitwise 相同，
#     manifest_hash 卻不同 —— 它涵蓋了 wall-clock 與輸出路徑（NOTE-038）。
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


#: 不進 content_hash 的欄位。它們描述「這次在哪裡跑、跑多久」，
#: 不描述「跑了什麼」。把它們算進去會讓 run 身分在原理上不可重現（NOTE-038）。
_EXECUTION_ONLY_FIELDS = frozenset({"runtime_s", "outputs"})


def _content_payload(node: Any) -> Any:
    """遞迴剝除只描述執行環境的欄位，留下決定內容的部分。"""
    if isinstance(node, dict):
        return {
            key: _content_payload(value)
            for key, value in node.items()
            if key not in _EXECUTION_ONLY_FIELDS
        }
    if isinstance(node, list):
        return [_content_payload(item) for item in node]
    return node


def _surrogate_identity() -> dict[str, Any]:
    """surrogate 與 estimator 的身分。它們同樣決定 initial simulation 的內容。"""
    from pcmef.surrogate.calibration import PLACEHOLDER_SMOKE_CALIBRATION
    from pcmef.surrogate.distance import (
        DEFAULT_DETECTION_THRESHOLD_SIGMA,
        DEFAULT_MIN_RETURN_BINS,
        SELECTED_ESTIMATOR,
    )

    identity: dict[str, Any] = {
        "estimator": SELECTED_ESTIMATOR.value,
        "detection_threshold_sigma": DEFAULT_DETECTION_THRESHOLD_SIGMA,
        "min_return_bins": DEFAULT_MIN_RETURN_BINS,
        "calibration_hash": PLACEHOLDER_SMOKE_CALIBRATION.calibration_hash(),
        "calibration_placeholders": PLACEHOLDER_SMOKE_CALIBRATION.placeholder_names(),
    }
    try:
        from pcmef.surrogate.estimator_selection import preregistration_hash

        identity["preregistration_hash"] = preregistration_hash()
    except Exception as error:  # noqa: BLE001
        identity["preregistration_hash"] = f"unavailable: {error}"
    return identity


def _parameter_registry_block() -> dict[str, Any]:
    """manifest 內的參數身分區塊。

    registry 讀不到時如實記錄錯誤而不是省略欄位：一份沒有這個區塊的 manifest
    與一份記著「registry 壞了」的 manifest，事後的意義完全不同。
    """
    from pcmef.core.parameters import ParameterRegistry, ParameterRegistryError

    try:
        registry = ParameterRegistry.load()
    except ParameterRegistryError as error:
        return {"available": False, "error": str(error)}
    summary = registry.summary()
    summary["available"] = True
    return summary


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
            # NOTE(NOTE-030): 每份 artifact 都必須帶著「這次用的是哪一組參數」。
            # 沒有 parameter_set_hash 的產物，事後無法判定它是在哪一組未校準
            # 常數下算出來的 —— 而那正是 fidelity 主張成立與否的前提。
            "parameter_registry": _parameter_registry_block(),
            "surrogate_identity": _surrogate_identity(),
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
        # NOTE(NOTE-038): manifest_hash 涵蓋 runtime_s 與 outputs 路徑，因此它
        # **在原理上不可重現** —— 兩次相同輸入的執行必然得到不同的值。實測：
        # 兩次獨立執行的 12 個 .npy 全部 bitwise 相同，manifest_hash 卻不同，
        # 唯一差異就是 wall-clock 與輸出目錄名。
        #
        # 它的定義維持不變（NOTE-030 的承諾），但**不再**參與 run 身分：
        # 執行耗時與存放路徑是「這次跑在哪裡跑多久」，不是「跑了什麼」。
        # content_hash 只涵蓋決定內容的欄位。
        manifest["content_hash"] = hash_object(_content_payload(scenarios))
        manifest["run_identity_hash"] = hash_object(
            {
                "content_hash": manifest["content_hash"],
                "parameter_set_hash": manifest["parameter_registry"].get(
                    "parameter_set_hash"
                ),
                "surrogate": manifest["surrogate_identity"],
            }
        )

        path = target_dir / filename
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path
