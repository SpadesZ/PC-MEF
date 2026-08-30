# PC-MEF Research System source maintenance contract
# 上下游: 讀已凍結的 CAL-PREREG-002、configs/calibration_preregistration.yaml、
#         經 core.formal_loader 解析的 freeze/initial_simulation.lock.json 與
#         parameter registry；由 cli 的 calibration stage0 在子行程呼叫
#         （NOTE-012）；產出 stage0_identifiability.json，其 payload 之後由
#         calibration stage0 --freeze 凍成 CAL-STAGE0-001。
# 檔案路徑: pcmef/experiments/calibration_stage0.py
# 產生時間: 2026-08-30 14:10 +08:00
# 版本: v0.1.0
# 功能說明: 在**完全不碰真實資料**的前提下，量每一個候選校準參數「optimizer
#           看不看得見它」—— 把它掃過整個已凍結的登記範圍，看它造成的觀測量
#           變化有沒有大過模擬自己的種子間雜訊（sigma_MC）。
# 模組定位: CAL-PREREG-002 stage_0 的執行器。它「不是」判準的來源 ——
#           門檻 K、borderline 區間、replicates 與 seed set 全部只能來自
#           已凍結的預註冊，本模組照著跑，跑完不得回頭改判準。
# 主要責任:
#   1. DIMENSION_BINDINGS 把 20 個 optimizer 維度對應到它真正作用的位置
#   2. sigma_MC：在凍結 initial 值上以 8 組獨立種子量每個 observable 的 SD
#   3. leverage_sim = |o(hi) - o(lo)| / sigma_MC(o)，逐 observable 記錄
#   4. 依 K 與 borderline_band 判定 ADMITTED / GAUGE_FIXED / BORDERLINE
#   5. 階段內兩兩共線性（響應向量先除以 sigma_MC 才無單位）
# 維護提醒:
#   - 不得在此讀取 calibration partition 或 held-out 的任何數值；stage 0
#     的整個存在理由就是它先於 first access（AMD-003 P0-1）。
#   - 不得對 sigma_MC = 0 的 observable 回傳 inf 或極大值。除零後當成
#     「槓桿無限大」會讓一個**沒有雜訊參考**的量自動取得入場券；
#     預註冊對此沒有規定，因此正確處置是回報 DEGENERATE 並要求裁決。
#   - 不得把落在 [5, 20] 的參數四捨五入到某一邊；那是
#     STAGE0_ADJUDICATION_REQUIRED，與 estimator 那一輪的 TIE_BREAK 同一條規則。
#   - 不得在看過結果後新增/移除候選參數或改動參數分組；發現新的簡併只能
#     回報，改組是 amendment 的事。
#   - 不得為了讓某個維度可評估而放寬已凍結的 bounds；bounds 只有
#     calibration_plan.resolve_numeric_bounds 一條路徑。
#   - v0.1.0 新增：首版 stage 0 可辨識性探測（AMD-003 之後的 CAL-PREREG-002）。
# 驗證方式:
#   - py -3.10 -m pcmef.cli calibration stage0 --out outputs/calibration/stage_0
#   - py -3.10 -m pytest tests/unit/test_calibration_stage0.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA

__all__ = [
    "STAGE0_ID",
    "Stage0Error",
    "DimensionBinding",
    "DIMENSION_BINDINGS",
    "OBSERVABLE_STATISTICS",
    "observable_names",
    "summarise_recording",
    "leverage_of",
    "classify_leverage",
    "cosine_similarity",
    "declared_observables",
    "derive_feasible_edge",
    "sigma_mc_floor",
    "SIGMA_MC_RELATIVE_FLOOR",
    "run_stage0",
    "execute_stage0",
]

STAGE0_ID = "CAL-STAGE0-001"

#: 每個 (class, feature) 取兩個統計量。**兩個都必須有**，不是實作偏好：
#: CAL-PREREG-002 的 stagewise 逐階段把 observable 寫成「分佈**位置**（median）」
#: 與「分佈**離散度**（IQR）」兩項；而且只用 median 的話，
#: `ambient_jitter_relative` 與 `noise_relative_sigma` 這種**只改離散度不改位置**
#: 的參數，其槓桿會在建構上恆為 ~0 而被自動 gauge-fix ——
#: 那會與預註冊 stage 1 自己寫下的可辨識性論證（「兩個參數、兩個互相獨立的
#: 統計量」）直接矛盾。
OBSERVABLE_STATISTICS: tuple[str, ...] = ("median", "iqr")

#: 模擬的取樣間隔取自已凍結 lock 的 initial 值，不是全域常數（NOTE-011）。
FROZEN_SAMPLE_INTERVAL_S = 0.08200001312
FROZEN_SAMPLE_INTERVAL_SOURCE = "initial_simulation.lock:initial_parameter_values"


class Stage0Error(RuntimeError):
    """stage 0 無法在不違反預註冊的前提下執行完畢。"""


@dataclass(frozen=True)
class DimensionBinding:
    """一個 optimizer 維度實際作用到哪裡。

    `requires_render` 是**結構事實**而非最佳化猜測：`build_transient_scene_dict()`
    只吃 `ScenarioConfig` 與 mitsuba_adapter 的模組常數，`SurrogateCalibration`
    的比例常數根本進不了場景，它們作用在算圖**之後**的映射。因此 surrogate
    維度可以重用同一批 cube。這一點另由 RENDER_INVARIANCE 實測抽查確認，
    不只靠這段話。
    """

    dimension: str
    target: str
    attribute: str
    class_label: str | None
    requires_render: bool


_SCENE = "scene_constant"
_ALBEDO = "scene_albedo"
_MEDIUM = "medium"
_SURROGATE = "surrogate"

#: 20 個維度的完整綁定表。缺一個就拒絕執行 —— 一個沒有綁定的維度會被
#: 安靜跳過，而它在 optimizer 裡仍然是一個自由度。
DIMENSION_BINDINGS: tuple[DimensionBinding, ...] = (
    # stage 1 / 2 / 5：surrogate 映射常數，作用在算圖之後。
    DimensionBinding("ambient_energy_to_mcps", _SURROGATE, "ambient_energy_to_mcps", None, False),
    DimensionBinding("ambient_jitter_relative", _SURROGATE, "ambient_jitter_relative", None, False),
    DimensionBinding("signal_energy_to_mcps", _SURROGATE, "signal_energy_to_mcps", None, False),
    DimensionBinding("noise_relative_sigma", _SURROGATE, "noise_relative_sigma", None, False),
    DimensionBinding("sigma_width_to_mm", _SURROGATE, "sigma_width_to_mm", None, False),
    DimensionBinding("sigma_snr_weight", _SURROGATE, "sigma_snr_weight", None, False),
    DimensionBinding("sigma_multipath_weight", _SURROGATE, "sigma_multipath_weight", None, False),
    # stage 3：幾何 / 表面 / 箔片。全部是 mitsuba_adapter 的模組常數。
    DimensionBinding("_FOIL_GAP_TO_BOTTLE_RATIO", _SCENE, "_FOIL_GAP_TO_BOTTLE_RATIO", None, True),
    DimensionBinding("_FOIL_REFLECTANCE_940NM", _SCENE, "_FOIL_REFLECTANCE_940NM", None, True),
    DimensionBinding("_FOIL_SURFACE_ALPHA", _SCENE, "_FOIL_SURFACE_ALPHA", None, True),
    DimensionBinding("_FOIL_SIZE_TO_DIAMETER_RATIO", _SCENE, "_FOIL_SIZE_TO_DIAMETER_RATIO", None, True),
    DimensionBinding("_BOTTLE_SURFACE_ALPHA", _SCENE, "_BOTTLE_SURFACE_ALPHA", None, True),
    DimensionBinding("sensor.fov_deg", _SCENE, "_SENSOR_FOV_DEG", None, True),
    DimensionBinding("light.cutoff_angle_deg", _SCENE, "_LIGHT_CUTOFF_ANGLE_DEG", None, True),
    # stage 4：參與介質。三個密度由 scenario 的 medium 供應，三個 albedo 是模組 dict。
    DimensionBinding("medium.turbidity", _MEDIUM, "turbidity", "Water-filled", True),
    DimensionBinding("medium.bubble_density", _MEDIUM, "bubble_density", "Bubbly", True),
    DimensionBinding("medium.mist_density", _MEDIUM, "mist_density", "Misty", True),
    DimensionBinding("_ALBEDO_BY_PRESET.water", _ALBEDO, "water", "Water-filled", True),
    DimensionBinding("_ALBEDO_BY_PRESET.bubbly", _ALBEDO, "bubbly", "Bubbly", True),
    DimensionBinding("_ALBEDO_BY_PRESET.misty", _ALBEDO, "misty", "Misty", True),
)

#: 四類的介質鍵與凍結的 CRN 種子（CAL-PREREG-002 optimizer.common_random_numbers）。
CRN_SEEDS: dict[str, int] = {
    "Empty": 1001,
    "Water-filled": 1002,
    "Bubbly": 1042,
    "Misty": 1004,
}

#: 四類的 medium 欄位與其凍結 initial 值（lock 的 initial_parameter_values）。
MEDIUM_KEY_BY_CLASS: dict[str, str | None] = {
    "Empty": None,
    "Water-filled": "turbidity",
    "Bubbly": "bubble_density",
    "Misty": "mist_density",
}


#: 各階段在預註冊 `stagewise[].observables` 裡**自己宣告要最佳化**的通道。
#:
#: 這是對預註冊散文的一個**讀法**，不是新規則，因此連同結果一起寫進 artifact
#: 供裁決者核對。它只用來產生診斷欄位 `max_leverage_declared`；
#: **入場判定仍嚴格採用預註冊的字面規則**（max over 全部 observable）。
#: 兩個數字都記錄，是因為它們可能不一致 —— 而那個不一致本身就是 stage 0
#: 應該回報的東西：一個參數可能靠某個「它所屬階段從不最佳化」的 observable
#: 取得入場券，然後在該階段的目標函數裡幾乎不可見。
DECLARED_CHANNELS: dict[str, dict[str, tuple[str, ...]]] = {
    "AMBIENT": {
        "classes": CLASS_ORDER,
        "features": ("ambient_rate_mcps",),
    },
    "SIGNAL_SCALE": {
        "classes": CLASS_ORDER,
        "features": ("signal_rate_mcps",),
    },
    "GEOMETRY_SURFACE_FOIL": {
        "classes": ("Empty",),
        "features": ("distance_mm", "signal_rate_mcps", "sigma_like"),
    },
    "PARTICIPATING_MEDIA": {
        "classes": ("Water-filled", "Bubbly", "Misty"),
        "features": ("distance_mm", "signal_rate_mcps", "sigma_like"),
    },
    "SENSOR_SURROGATE": {
        "classes": CLASS_ORDER,
        "features": ("sigma_like",),
    },
}


def declared_observables(stage_id: str | None) -> list[str]:
    """該階段宣告要最佳化的 observable 名稱。未知階段回傳空清單。"""
    spec = DECLARED_CHANNELS.get(stage_id or "")
    if spec is None:
        return []
    return [
        f"{cls}|{feature}|{stat}"
        for cls in spec["classes"]
        for feature in spec["features"]
        for stat in OBSERVABLE_STATISTICS
    ]


def observable_names() -> list[str]:
    """32 個 observable 的 canonical 名稱與順序。

    順序固定為 class -> feature -> statistic，且 feature 取自 TOF_SCHEMA 而非
    硬寫（NOTE-001）。響應向量的分量順序必須穩定，否則餘弦相似度會隨
    dict 順序改變。
    """
    return [
        f"{cls}|{feature}|{stat}"
        for cls in CLASS_ORDER
        for feature in TOF_SCHEMA
        for stat in OBSERVABLE_STATISTICS
    ]


def summarise_recording(values: np.ndarray, class_label: str) -> dict[str, float]:
    """把一筆 (n, 4) recording 變成該類別的 8 個 observable。"""
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != len(TOF_SCHEMA):
        raise Stage0Error(
            f"recording for {class_label} has shape {array.shape}; expected "
            f"(n, {len(TOF_SCHEMA)}) in TOF_SCHEMA order"
        )
    out: dict[str, float] = {}
    for index, feature in enumerate(TOF_SCHEMA):
        column = array[:, index]
        q25, q50, q75 = np.percentile(column, [25.0, 50.0, 75.0])
        out[f"{class_label}|{feature}|median"] = float(q50)
        out[f"{class_label}|{feature}|iqr"] = float(q75 - q25)
    return out


# ---------------------------------------------------------------------------
# 判定規則
# ---------------------------------------------------------------------------


#: sigma_MC 的退化判準（AMD-004）。相對於該 observable 自己的尺度，
#: 因此不同單位的 observable 用同一條規則。1e-12 的用意是「浮點意義下就是 0」，
#: 不是一個可以擋下真實參數的軟門檻。
SIGMA_MC_RELATIVE_FLOOR = 1.0e-12


def sigma_mc_floor(reference_value: float) -> float:
    """該 observable 的 sigma_MC 退化門檻。"""
    return SIGMA_MC_RELATIVE_FLOOR * abs(float(reference_value))


def leverage_of(
    delta: float, sigma_mc: float, reference_value: float | None = None
) -> tuple[float | None, str]:
    """回傳 (leverage, branch)。**任何情況下都不回傳 inf。**

    AMD-004（NOTE-044）把 sigma_MC ≈ 0 從「一律未定、交付裁決」細分成三個
    **決定性**分支，因為原本的處置把兩件性質相反的事混為一談：

    | 分支 | 條件 | leverage | 理由 |
    |---|---|---|---|
    | `RATIO` | sigma_MC > floor | \\|Δo\\|/sigma_MC | 正常情況 |
    | `INERT` | sigma_MC ≤ floor 且 \\|Δo\\| ≤ floor | **0.0** | 這個 observable 既沒有雜訊也沒有反應，它對這個參數完全沒有資訊。定義成 0 是精確的，不是保守猜測。 |
    | `DETERMINISTIC_RESPONSE` | sigma_MC ≤ floor 且 \\|Δo\\| > floor | None | 無雜訊卻會動 —— 訊噪比在數學上發散。**不得**寫成 inf 或某個大數，那是憑空發明數值；改為具名分支並由呼叫端判為可辨識。 |

    `INERT` 與 `DETERMINISTIC_RESPONSE` 先前都落在同一個 None，於是
    「這個量對這個參數沒有資訊」與「這個量對這個參數是完美證據」長得一樣。
    """
    if not np.isfinite(delta) or not np.isfinite(sigma_mc):
        return None, "NON_FINITE"
    floor = sigma_mc_floor(reference_value if reference_value is not None else delta)
    if sigma_mc > floor and sigma_mc > 0.0:
        return float(abs(delta) / sigma_mc), "RATIO"
    if abs(delta) <= floor:
        return 0.0, "INERT"
    return None, "DETERMINISTIC_RESPONSE"


def classify_leverage(
    leverage: float | None, threshold: float, band: tuple[float, float]
) -> str:
    """依 CAL-PREREG-002 的 admission_threshold 判定，不做任何四捨五入。"""
    low, high = band
    if leverage is None:
        return "UNDETERMINED"
    if low <= leverage <= high:
        return "BORDERLINE"
    if leverage >= threshold:
        return "ADMITTED"
    return "GAUGE_FIXED"


#: 可行域推導的固定設定（AMD-004）。事前選定，執行中不得調整。
FEASIBILITY_BISECTION_STEPS = 20
FEASIBILITY_RELATIVE_TOLERANCE = 1.0e-3


def derive_feasible_edge(
    initial: float,
    bound: float,
    is_feasible: Callable[[float], bool],
    steps: int = FEASIBILITY_BISECTION_STEPS,
    tolerance: float = FEASIBILITY_RELATIVE_TOLERANCE,
) -> dict[str, Any]:
    """由**預註冊的**二分法導出朝某個界線方向最遠的可行值。

    這不是「把界線縮小到好看為止」：
      * 起點固定為凍結的 initial 值（依定義可行）與凍結的登記界線；
      * 步數與容忍值事前固定，不得在看到結果後調整；
      * 回傳值一律**取可行側**，因此導出的域必為原登記域的子集。

    凍結的 `parameter_ranges` 一個位元都沒有被改 —— 它仍然是實驗的登記範圍。
    這裡導出的是「在凍結的 initial 場景下，模擬器真的算得出物理上可受理結果」
    的子區間，並連同判準一起寫進 artifact。
    """
    if is_feasible(bound):
        return {
            "edge": float(bound),
            "status": "BOUND_IS_FEASIBLE",
            "iterations": 0,
            "relative_width_retained": 1.0,
        }
    feasible, infeasible = float(initial), float(bound)
    if not is_feasible(feasible):
        return {
            "edge": None,
            "status": "INITIAL_VALUE_INFEASIBLE",
            "iterations": 0,
            "relative_width_retained": 0.0,
        }
    span = abs(bound - initial)
    iterations = 0
    for _ in range(steps):
        if span > 0 and abs(infeasible - feasible) <= tolerance * span:
            break
        midpoint = 0.5 * (feasible + infeasible)
        iterations += 1
        if is_feasible(midpoint):
            feasible = midpoint
        else:
            infeasible = midpoint
    return {
        "edge": float(feasible),
        "status": "DERIVED_BY_BISECTION",
        "iterations": iterations,
        "relative_width_retained": (
            abs(feasible - initial) / span if span > 0 else 0.0
        ),
    }


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float | None:
    """兩個 sigma_MC 正規化響應向量的餘弦。零向量回傳 None，不回傳 0。

    零響應向量代表「這個參數在整個 observable 集合上什麼都沒動」，
    它與任何向量的夾角在數學上沒有定義。把它記成 0（正交、可辨識）
    會讓一個完全看不見的參數看起來與別人無關，那是最糟的誤導方向。
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0.0 or nb <= 0.0:
        return None
    return float(np.dot(a, b) / (na * nb))


# ---------------------------------------------------------------------------
# 執行
# ---------------------------------------------------------------------------


def _make_overrides(binding: DimensionBinding, value: float) -> dict[str, Any]:
    return {"binding": binding, "value": value}


def _is_feasible(
    evaluate: Callable[[dict[str, Any] | None, int], dict[str, float]],
    binding: DimensionBinding,
    value: float,
) -> bool:
    """可行 = 四類在凍結的 CRN 種子下都算得出有限、物理上可受理的觀測。

    不吞例外的細節：呼叫端會把導出的域寫進 artifact，因此「哪裡不可行」
    仍然看得到；這裡只需要一個布林。
    """
    try:
        row = evaluate(_make_overrides(binding, value), -1)
    except Exception:  # noqa: BLE001
        return False
    return all(np.isfinite(v) for v in row.values())


def run_stage0(
    resolved_bounds: dict[str, tuple[float, float]],
    initial_values: dict[str, Any],
    stage_parameters: dict[str, list[str]],
    stage0_settings: dict[str, Any],
    evaluate: Callable[[dict[str, Any] | None, int], dict[str, float]],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """執行 stage 0 並回傳完整結果。

    `evaluate(override, seed)` 由呼叫端提供，負責「在給定覆寫與種子下算出
    32 個 observable」。把算圖注入進來而不是寫死在這裡，是為了讓判定邏輯
    可以在沒有 mitsuba 的機器上被測試 —— 判定規則才是本模組的研究內容。
    """
    say = progress or (lambda _message: None)

    threshold = float(stage0_settings["threshold"])
    band = (
        float(stage0_settings["borderline_band"][0]),
        float(stage0_settings["borderline_band"][1]),
    )
    seed_set = [int(s) for s in stage0_settings["seed_set"]]
    replicates = int(stage0_settings["replicates"])
    if len(seed_set) != replicates:
        raise Stage0Error(
            f"the frozen preregistration declares replicates={replicates} but "
            f"{len(seed_set)} seeds; stage 0 must use exactly the frozen seed set"
        )

    bindings = {b.dimension: b for b in DIMENSION_BINDINGS}
    missing = sorted(set(resolved_bounds) - set(bindings))
    if missing:
        raise Stage0Error(
            f"no binding for optimizer dimension(s) {missing}. An unbound dimension "
            "would be silently skipped here while remaining a free parameter in the "
            "optimizer, which is exactly the failure stage 0 exists to prevent."
        )
    names = observable_names()

    # -- sigma_MC：凍結 initial 值上的種子間標準差 -------------------------
    say(f"sigma_MC: {replicates} replicate seeds at the frozen initial values")
    replicate_rows: list[dict[str, float]] = []
    for index, seed in enumerate(seed_set, start=1):
        say(f"  replicate {index}/{replicates} seed={seed}")
        replicate_rows.append(evaluate(None, seed))

    matrix = np.array([[row[name] for name in names] for row in replicate_rows])
    # ddof=1：這是**樣本**標準差。用母體公式會系統性低估雜訊底線，
    # 也就是系統性高估每一個參數的槓桿。
    sigma_mc_values = matrix.std(axis=0, ddof=1)
    sigma_mc = {name: float(v) for name, v in zip(names, sigma_mc_values)}
    initial_mean = {
        name: float(v) for name, v in zip(names, matrix.mean(axis=0))
    }

    degenerate = sorted(n for n, v in sigma_mc.items() if not (v > 0.0))

    # -- 逐維度掃到邊界 ----------------------------------------------------
    crn_baseline = evaluate(None, -1)  # -1 = 用凍結的 CRN 種子，見執行器
    parameters: list[dict[str, Any]] = []
    response_vectors: dict[str, np.ndarray] = {}

    for dimension in sorted(resolved_bounds):
        binding = bindings[dimension]
        lo, hi = resolved_bounds[dimension]
        say(f"sweep {dimension}: [{lo!r}, {hi!r}]")

        entry: dict[str, Any] = {
            "dimension": dimension,
            "target": binding.target,
            "attribute": binding.attribute,
            "class_scope": binding.class_label or "shared",
            "requires_render": binding.requires_render,
            "low": lo,
            "high": hi,
            "current": _current_value(dimension, initial_values),
            "stage": _stage_of(dimension, stage_parameters),
        }

        # 兩個界線各自 try：一邊失敗時仍要知道另一邊算不算得出來。
        # 把兩者包在同一個 try 裡會讓「下界不可行」與「兩端都不可行」
        # 在報告上長得一樣，而裁決需要的正是這個區別。
        evaluated: dict[str, dict[str, float]] = {}
        for edge, value in (("low", lo), ("high", hi)):
            try:
                evaluated[edge] = evaluate(_make_overrides(binding, value), -1)
                entry[f"{edge}_status"] = "OK"
            except Exception as error:  # noqa: BLE001
                entry[f"{edge}_status"] = "FAILED"
                entry[f"{edge}_error"] = f"{type(error).__name__}: {error}"

        if len(evaluated) != 2:
            failed = [e for e in ("low", "high") if entry[f"{e}_status"] == "FAILED"]
            entry["evaluation_status"] = "INFEASIBLE_BOUND"
            entry["error"] = "; ".join(entry[f"{e}_error"] for e in failed)
            entry["infeasible_edges"] = failed

            # AMD-004：以**預註冊的**二分法導出可行域，而不是宣告未定。
            # 凍結的 parameter_ranges 不動；導出的域一定是它的子集。
            current = entry["current"]
            feasible: dict[str, Any] = {}
            usable = True
            for edge in ("low", "high"):
                bound_value = lo if edge == "low" else hi
                if entry[f"{edge}_status"] == "OK":
                    feasible[edge] = {
                        "edge": float(bound_value),
                        "status": "BOUND_IS_FEASIBLE",
                        "iterations": 0,
                        "relative_width_retained": 1.0,
                    }
                    continue
                if not isinstance(current, (int, float)):
                    usable = False
                    break
                derived = derive_feasible_edge(
                    float(current),
                    float(bound_value),
                    lambda v: _is_feasible(evaluate, binding, v),
                )
                feasible[edge] = derived
                if derived["edge"] is None:
                    usable = False
            entry["feasible_domain"] = feasible

            if not usable:
                entry["outcome"] = "UNDETERMINED"
                entry["why"] = (
                    "the frozen initial value itself is infeasible, or the parameter "
                    "is not a scalar; no feasible sub-domain can be derived"
                )
                parameters.append(entry)
                continue

            f_lo = feasible["low"]["edge"]
            f_hi = feasible["high"]["edge"]
            say(f"  infeasible {failed}; derived feasible domain [{f_lo!r}, {f_hi!r}]")
            try:
                at_low = evaluate(_make_overrides(binding, f_lo), -1)
                at_high = evaluate(_make_overrides(binding, f_hi), -1)
            except Exception as error:  # noqa: BLE001
                entry["outcome"] = "UNDETERMINED"
                entry["why"] = (
                    "the derived feasible domain still could not be evaluated: "
                    f"{type(error).__name__}: {error}"
                )
                parameters.append(entry)
                continue
            entry["swept_low"] = f_lo
            entry["swept_high"] = f_hi
            evaluated = {"low": at_low, "high": at_high}
        else:
            entry["swept_low"] = lo
            entry["swept_high"] = hi

        at_low, at_high = evaluated["low"], evaluated["high"]
        entry.setdefault("evaluation_status", "OK")
        deltas = {name: at_high[name] - at_low[name] for name in names}
        computed = {
            name: leverage_of(deltas[name], sigma_mc[name], initial_mean[name])
            for name in names
        }
        leverages = {n: v for n, (v, _branch) in computed.items()}
        branches = {n: b for n, (_v, b) in computed.items()}
        defined = {n: v for n, v in leverages.items() if v is not None}
        # 無雜訊卻會動的 observable：訊噪比發散，是**可辨識的最強證據**，
        # 不是缺資訊。它不進 max（沒有數值可比），但單獨足以判為可辨識。
        deterministic = sorted(
            n for n in names if branches[n] == "DETERMINISTIC_RESPONSE"
        )
        non_finite = sorted(n for n in names if branches[n] == "NON_FINITE")

        best_name = max(defined, key=lambda n: defined[n]) if defined else None
        best = defined[best_name] if best_name is not None else None

        entry["observables_moved"] = sorted(
            n for n in names if abs(deltas[n]) > 0.0
        )
        entry["classes_moved"] = sorted(
            {n.split("|", 1)[0] for n in entry["observables_moved"]}
        )
        entry["delta"] = {n: deltas[n] for n in names}
        entry["leverage"] = {n: leverages[n] for n in names}
        entry["max_leverage"] = best
        entry["max_leverage_observable"] = best_name
        entry["top_leverage"] = [
            {"observable": n, "leverage": defined[n]}
            for n in sorted(defined, key=lambda n: defined[n], reverse=True)[:5]
        ]
        entry["leverage_branch"] = branches
        entry["deterministic_response_observables"] = deterministic
        entry["non_finite_observables"] = non_finite

        # 診斷：把 max 限制在「該階段自己宣告要最佳化的 observable」上。
        # 這**不**改變入場判定，只是把「靠一個自己階段從不最佳化的 observable
        # 拿到入場券」這件事變成看得見的數字。
        stage_declared = declared_observables(entry["stage"])
        declared_defined = {n: v for n, v in defined.items() if n in stage_declared}
        declared_best_name = (
            max(declared_defined, key=lambda n: declared_defined[n])
            if declared_defined
            else None
        )
        declared_best = (
            declared_defined[declared_best_name]
            if declared_best_name is not None
            else None
        )
        entry["declared_observables"] = stage_declared
        entry["max_leverage_declared"] = declared_best
        entry["max_leverage_declared_observable"] = declared_best_name
        entry["outcome_if_restricted_to_declared"] = classify_leverage(
            declared_best, threshold, band
        )

        outcome = classify_leverage(best, threshold, band)
        if non_finite:
            outcome = "UNDETERMINED"
            entry["why"] = (
                f"{len(non_finite)} observable(s) produced a non-finite value or "
                "non-finite sigma_MC; no classification is defensible."
            )
        elif deterministic and outcome != "ADMITTED":
            # 無雜訊、卻在整個登記範圍上會動 -> 訊噪比發散。
            # 這是可辨識的最強證據，但它是**具名分支**而不是一個假造的 inf。
            outcome = "ADMITTED_DETERMINISTIC"
            entry["why"] = (
                f"{len(deterministic)} observable(s) have sigma_MC at or below the "
                f"degeneracy floor (relative {SIGMA_MC_RELATIVE_FLOOR:g}) yet move "
                "across the registered range. A noiseless observable that responds "
                "is the strongest possible identifiability evidence, so this is "
                "recorded as its own outcome rather than as an invented infinite "
                "leverage."
            )
        entry["outcome"] = outcome

        # 響應向量：退化（INERT / DETERMINISTIC_RESPONSE）的分量記 0。
        # DETERMINISTIC_RESPONSE 記 0 是刻意保守 —— 它沒有可用的正規化尺度，
        # 硬給一個大數會讓餘弦被那一個分量獨佔，等於用發明的數字決定簡併。
        vector = np.array(
            [
                deltas[name] / sigma_mc[name]
                if branches[name] == "RATIO"
                else 0.0
                for name in names
            ]
        )
        response_vectors[dimension] = vector
        parameters.append(entry)

    # -- 階段內共線性 ------------------------------------------------------
    collinearity: list[dict[str, Any]] = []
    for stage_id in stage_parameters:
        dims = [
            d for d in sorted(resolved_bounds)
            if _stage_of(d, stage_parameters) == stage_id
        ]
        for i, left in enumerate(dims):
            for right in dims[i + 1 :]:
                if left not in response_vectors or right not in response_vectors:
                    continue
                cosine = cosine_similarity(
                    response_vectors[left], response_vectors[right]
                )
                collinearity.append(
                    {
                        "stage": stage_id,
                        "pair": [left, right],
                        "cosine": cosine,
                        "abs_cosine": None if cosine is None else abs(cosine),
                        "degenerate": (
                            None if cosine is None else bool(abs(cosine) >= 0.98)
                        ),
                    }
                )

    return {
        "observable_names": names,
        "sigma_mc": sigma_mc,
        "sigma_mc_degenerate": degenerate,
        "initial_mean": initial_mean,
        "crn_baseline": crn_baseline,
        "replicate_values": [
            {"seed": seed, "observables": row}
            for seed, row in zip(seed_set, replicate_rows)
        ],
        "parameters": parameters,
        "collinearity": collinearity,
        "threshold": threshold,
        "borderline_band": list(band),
        "feasible_domains": {
            e["dimension"]: {
                "registered": [e["low"], e["high"]],
                "swept": [e.get("swept_low"), e.get("swept_high")],
                "infeasible_edges": e.get("infeasible_edges", []),
                "derivation": e.get("feasible_domain"),
            }
            for e in parameters
            if e.get("infeasible_edges")
        },
    }


def _registry_name(dimension: str) -> str:
    """把展開後的維度名映射回 registry 的參數名。"""
    if dimension.startswith("_ALBEDO_BY_PRESET."):
        return "_ALBEDO_BY_PRESET"
    return dimension


def _current_value(dimension: str, initial_values: dict[str, Any]) -> Any:
    """該維度在凍結 lock 裡的 initial 值。

    多值參數必須取到**該維度自己**那個純量：回傳整個 dict 會讓
    「這個維度目前在哪裡」這一欄對三個 albedo 維度顯示同一份字典，
    而它們是三個不同的自由度。
    """
    registry_name = _registry_name(dimension)
    value = initial_values.get(registry_name)
    if registry_name != dimension and isinstance(value, dict):
        return value.get(dimension.split(".", 1)[1])
    return value


#: 本執行器實作的是哪一份預註冊。改動判定規則時**必須**同步改這個常數，
#: 否則程式會用新規則產出一份宣稱舊協定的 artifact。
IMPLEMENTS_PREREGISTRATION = "CAL-PREREG-003"


def _assert_protocol_is_frozen(freeze_dir: str | Path) -> Path:
    """NOTE(NOTE-044): 沒有對應的凍結協定就拒絕執行。

    本模組實作的是 **AMD-004** 的語意 —— 以階段宣告的通道判定入場、
    sigma_MC 的三個決定性分支、以及不可行界線的可行域推導。
    CAL-PREREG-002 沒有這些規則；在 CAL-PREREG-003 凍結之前跑 stage 0，
    產出的 artifact 會宣稱一份它其實沒有遵守的協定。
    **拒絕執行，比產出一份說謊的 artifact 好。**
    """
    directory = Path(freeze_dir) / "preregistrations"
    target = directory / f"{IMPLEMENTS_PREREGISTRATION}.prereg.json"
    if target.exists():
        return target
    frozen = sorted(p.name for p in directory.glob("CAL-PREREG-*.prereg.json"))
    raise Stage0Error(
        f"{target} not found, so stage 0 must not run. This runner implements "
        f"{IMPLEMENTS_PREREGISTRATION} (AMD-004): stage-declared admission scope, "
        "the three deterministic sigma_MC branches, and the derived feasible "
        f"domain. Frozen preregistrations present: {frozen or 'none'}. "
        "None of those contain those rules, so running against one would produce "
        "an artifact claiming a protocol it did not follow. Freeze AMD-004 and "
        f"{IMPLEMENTS_PREREGISTRATION} first."
    )


# ---------------------------------------------------------------------------
# 算圖後端（需要 mitsuba；必須在已 set_variant 的行程內呼叫）
# ---------------------------------------------------------------------------


def execute_stage0(
    spp: int = 16,
    resolution: tuple[int, int] = (64, 64),
    temporal_bins: int = 128,
    n_samples: int = 500,
    freeze_dir: str = "freeze",
    repo_root: str = ".",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """在凍結的 initial model 上跑完整 stage 0，回傳可凍結的 artifact payload。

    本函式**不讀取**任何真實資料。四類的觀測量全部來自 mitsuba/mitransient
    對未校準場景的算圖，經 SensorSurrogate 與 TemporalModel 映射而來。
    """
    # NOTE(NOTE-044): 協定閘門必須在 **import mitsuba 之前** ——
    # 否則在沒有 LLVM 的機器上，使用者看到的是一則關於 drjit 後端的錯誤，
    # 而真正的原因是「CAL-PREREG-003 還沒凍結」。錯的理由比沒有理由更難查。
    _assert_protocol_is_frozen(freeze_dir)

    import mitsuba as mi
    import mitransient  # noqa: F401
    from dataclasses import replace

    from pcmef.core.formal_loader import load_formal_lock
    from pcmef.core.hash import hash_object
    from pcmef.core.parameters import ParameterRegistry
    from pcmef.experiments.calibration_plan import (
        bounds_resolution_hash,
        resolve_numeric_bounds,
    )
    from pcmef.experiments.calibration_prereg import load_protocol
    from pcmef.simulation import mitsuba_adapter as scene_module
    from pcmef.simulation.mitransient_adapter import (
        MiTransientAdapter,
        build_transient_scene_dict,
    )
    from pcmef.simulation.mitsuba_adapter import Illumination
    from pcmef.simulation.scenario import Geometry, Lighting, ScenarioConfig
    from pcmef.surrogate.calibration import (
        PLACEHOLDER_SMOKE_CALIBRATION,
        CalibratedScale,
    )
    from pcmef.surrogate.single_acquisition import SensorSurrogate
    from pcmef.surrogate.temporal_model import TemporalModel

    say = progress or (lambda _message: None)
    root = Path(repo_root)

    # -- 前置：一切都必須來自凍結物，且順序必須還沒被違反 -------------------
    prereg_path = _assert_protocol_is_frozen(freeze_dir)
    frozen = json.loads(prereg_path.read_text(encoding="utf-8"))
    frozen_payload = frozen["payload"]

    protocol = load_protocol()
    if hash_object(protocol) != frozen_payload["protocol_hash"]:
        raise Stage0Error(
            "configs/calibration_preregistration.yaml no longer hashes to the "
            f"protocol_hash frozen in {frozen['preregistration_id']}. The working "
            "copy of the protocol has drifted from the frozen one; stage 0 must "
            "run against the frozen protocol, not the edited file."
        )

    registry = ParameterRegistry.load()
    resolved = load_formal_lock("initial_simulation", freeze_dir, root)
    if resolved.payload_hash != frozen_payload["initial_simulation"]["lock_hash"]:
        raise Stage0Error(
            "initial_simulation.lock hash does not match the one frozen into "
            "CAL-PREREG-002"
        )

    bounds = resolve_numeric_bounds(protocol, resolved.payload, registry)
    stale = sorted({b.dimension for b in DIMENSION_BINDINGS} - set(bounds))
    if stale:
        raise Stage0Error(
            f"the binding table carries dimension(s) {stale} that the frozen bounds "
            "do not contain. A stale entry means the table and the frozen protocol "
            "have diverged, and stage 0 would be sweeping something the optimizer "
            "will never see."
        )
    bounds_hash = bounds_resolution_hash(protocol, resolved.payload, registry)
    if bounds_hash != frozen_payload["bounds_resolution_hash"]:
        raise Stage0Error(
            f"bounds_resolution_hash {bounds_hash} != frozen "
            f"{frozen_payload['bounds_resolution_hash']}; there is a second, "
            "unfrozen set of bounds"
        )

    ledger_path = root / protocol["data"]["first_access"]["ledger"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    calibration_access_count = int(ledger["calibration_access_count"])
    if calibration_access_count != 0:
        raise Stage0Error(
            f"calibration_access_count is {calibration_access_count}, not 0. "
            "CAL-PREREG-002 requires stage 0 to complete and freeze BEFORE the "
            "first calibration access; a non-zero count here means the "
            "preregistered ordering was already violated."
        )

    # split_registry.json 只記錄 split **結構**與計數，不含任何 recording 數值。
    # 依 AMD-003 的 access 定義，讀它不算一次 calibration access。
    split_registry = json.loads(
        (root / "data" / "splits" / "split_registry.json").read_text(encoding="utf-8")
    )
    heldout_access_count = int(split_registry["heldout_access_count"])
    if heldout_access_count != 0:
        raise Stage0Error(
            f"heldout_access_count is {heldout_access_count}, not 0; held-out must "
            "stay sealed until E1 final"
        )

    initial_values = dict(resolved.payload["initial_parameter_values"])
    stage_parameters = {
        str(stage["id"]): list(stage.get("parameters") or [])
        for stage in protocol["stagewise"]
    }

    # -- 算圖與映射 --------------------------------------------------------
    adapter = MiTransientAdapter()
    base_calibration = PLACEHOLDER_SMOKE_CALIBRATION
    surrogate_analysis = int(
        base_calibration.analysis.get("main_window_halfwidth_bins", 8)
    )

    def _base_medium(label: str) -> dict[str, Any]:
        key = MEDIUM_KEY_BY_CLASS[label]
        if key is None:
            return {}
        return {key: {"value": float(initial_values[f"medium.{key}"]), "placeholder": True}}

    render_cache: dict[tuple[str, int], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    render_calls = {"count": 0, "cache_hits": 0}

    def _render(config: ScenarioConfig, cacheable: bool, cache_key):
        if cacheable and cache_key in render_cache:
            render_calls["cache_hits"] += 1
            return render_cache[cache_key]
        start, width = adapter.default_binning(config, temporal_bins)
        cubes = []
        for illum in (Illumination.ACTIVE_ONLY, Illumination.AMBIENT_ONLY):
            scene_dict = build_transient_scene_dict(
                mi, config, temporal_bins, start, width, illumination=illum
            )
            scene = mi.load_dict(scene_dict)
            mi.render(scene, spp=int(config.spp), seed=int(config.seed))
            cube = np.array(scene.sensors()[0].film().develop_transient_())
            if not np.all(np.isfinite(cube)):
                raise Stage0Error(
                    f"{config.class_label} produced a non-finite transient; the "
                    "integrator parameters are already unphysical at this bound"
                )
            cubes.append(cube)
        axis = (start + (np.arange(temporal_bins) + 0.5) * width) / 299792458.0
        result = (cubes[0], cubes[1], axis)
        render_calls["count"] += 1
        if cacheable:
            render_cache[cache_key] = result
        return result

    def evaluate(override: dict[str, Any] | None, seed: int) -> dict[str, float]:
        binding: DimensionBinding | None = override["binding"] if override else None
        value = override["value"] if override else None
        scene_override = binding is not None and binding.requires_render

        calibration = base_calibration
        if binding is not None and binding.target == _SURROGATE:
            calibration = replace(
                calibration,
                **{
                    binding.attribute: CalibratedScale(
                        value=float(value),
                        placeholder=True,
                        source="stage 0 identifiability sweep",
                    )
                },
            )
        surrogate = SensorSurrogate(calibration)

        out: dict[str, float] = {}
        saved: dict[str, Any] = {}
        try:
            if scene_override and binding.target == _SCENE:
                saved["attr"] = getattr(scene_module, binding.attribute)
                setattr(scene_module, binding.attribute, float(value))
            elif scene_override and binding.target == _ALBEDO:
                saved["albedo"] = scene_module._ALBEDO_BY_PRESET[binding.attribute]
                scene_module._ALBEDO_BY_PRESET[binding.attribute] = float(value)

            for label in CLASS_ORDER:
                use_seed = CRN_SEEDS[label] if seed < 0 else int(seed)
                medium = _base_medium(label)
                if (
                    binding is not None
                    and binding.target == _MEDIUM
                    and binding.class_label == label
                ):
                    medium = {
                        binding.attribute: {"value": float(value), "placeholder": True}
                    }
                config = ScenarioConfig(
                    class_label=label,
                    seed=use_seed,
                    geometry=Geometry(
                        sensor_to_bottle_mm=float(initial_values["geometry.sensor_to_bottle_mm"]),
                        bottle_diameter_mm=float(initial_values["geometry.bottle_diameter_mm"]),
                        wall_thickness_mm=float(initial_values["geometry.wall_thickness_mm"]),
                        lateral_offset_mm=float(initial_values["geometry.lateral_offset_mm"]),
                    ),
                    lighting=Lighting(
                        preset="nominal",
                        irradiance=float(initial_values["lighting.irradiance"]),
                    ),
                    medium_parameters=medium,
                    spp=spp,
                    resolution=tuple(resolution),
                )
                # 只 cache CRN baseline：surrogate 維度不改場景，14 次評估
                # 全部重用同一批 cube。sigma_MC 的 8 組種子各只用一次，
                # cache 它們只會把 400 MB 的 cube 留在記憶體裡而沒有任何收益。
                cacheable = not scene_override and seed < 0
                active, ambient, axis = _render(config, cacheable, (label, use_seed))
                recording = TemporalModel(surrogate).generate_recording(
                    active,
                    axis,
                    sample_interval_s=FROZEN_SAMPLE_INTERVAL_S,
                    sample_interval_source=FROZEN_SAMPLE_INTERVAL_SOURCE,
                    seed=use_seed,
                    n_samples=n_samples,
                    ambient_transient=ambient,
                )
                out.update(summarise_recording(recording.values, label))
        finally:
            if "attr" in saved:
                setattr(scene_module, binding.attribute, saved["attr"])
            if "albedo" in saved:
                scene_module._ALBEDO_BY_PRESET[binding.attribute] = saved["albedo"]
        return out

    result = run_stage0(
        resolved_bounds=bounds,
        initial_values=initial_values,
        stage_parameters=stage_parameters,
        stage0_settings=frozen_payload["stage_0"],
        evaluate=evaluate,
        progress=say,
    )

    # -- 判定 --------------------------------------------------------------
    by_outcome: dict[str, list[str]] = {}
    for entry in result["parameters"]:
        by_outcome.setdefault(entry["outcome"], []).append(entry["dimension"])

    borderline = sorted(by_outcome.get("BORDERLINE", []))
    undetermined = sorted(by_outcome.get("UNDETERMINED", []))
    admitted = sorted(
        by_outcome.get("ADMITTED", []) + by_outcome.get("ADMITTED_DETERMINISTIC", [])
    )
    admitted_deterministic = sorted(by_outcome.get("ADMITTED_DETERMINISTIC", []))
    gauge_fixed = sorted(by_outcome.get("GAUGE_FIXED", []))
    new_degeneracies = [
        c for c in result["collinearity"] if c.get("degenerate") is True
    ]
    # 靠「自己階段從不最佳化的 observable」取得入場券的參數。
    admitted_elsewhere = [
        {
            "dimension": e["dimension"],
            "stage": e["stage"],
            "max_leverage": e["max_leverage"],
            "max_leverage_observable": e["max_leverage_observable"],
            "max_leverage_declared": e.get("max_leverage_declared"),
            "max_leverage_declared_observable": e.get(
                "max_leverage_declared_observable"
            ),
            "outcome_if_restricted_to_declared": e.get(
                "outcome_if_restricted_to_declared"
            ),
        }
        for e in result["parameters"]
        if e["outcome"].startswith("ADMITTED")
        and not str(e.get("outcome_if_restricted_to_declared", "")).startswith(
            "ADMITTED"
        )
    ]

    blockers: list[str] = []
    if borderline:
        blockers.append(
            f"{len(borderline)} dimension(s) landed inside the preregistered "
            f"borderline band {result['borderline_band']}: {borderline}"
        )
    if undetermined:
        blockers.append(
            f"{len(undetermined)} dimension(s) could not be classified: {undetermined}"
        )
    if new_degeneracies:
        blockers.append(
            f"{len(new_degeneracies)} within-stage pair(s) exceeded the "
            "collinearity threshold 0.98 and are not covered by CG-1..CG-4"
        )
    if admitted_elsewhere:
        blockers.append(
            f"{len(admitted_elsewhere)} dimension(s) were admitted on the strength "
            "of an observable that their own stage never optimises: "
            f"{[a['dimension'] for a in admitted_elsewhere]}. The preregistered "
            "admission rule is max over every observable, so this classification is "
            "literal-compliant, but those parameters would be near-invisible to the "
            "objective terms their stage actually minimises."
        )

    outcome = "STAGE0_ADJUDICATION_REQUIRED" if blockers else "STAGE0_COMPLETE"

    return {
        "stage": 0,
        "stage_id": STAGE0_ID,
        "name": "identifiability probe",
        "outcome": outcome,
        "adjudication_blockers": blockers,
        "reads_calibration_partition": False,
        "reads_heldout": False,
        "real_data_consulted": False,
        "calibration_access_count": calibration_access_count,
        "heldout_access_count": heldout_access_count,
        "preregistration_id": frozen["preregistration_id"],
        "preregistration_hash": frozen["payload_hash"],
        "protocol_hash": frozen_payload["protocol_hash"],
        "amendments": frozen_payload["amendments"],
        "initial_simulation_lock_hash": resolved.payload_hash,
        "errata": resolved.provenance()["errata"],
        "parameter_registry": {
            "version": registry.registry_version,
            "parameter_set_hash": registry.parameter_set_hash(),
        },
        "bounds_resolution_hash": bounds_hash,
        "resolved_bounds": {k: list(v) for k, v in sorted(bounds.items())},
        "simulation": {
            "spp": spp,
            "resolution": list(resolution),
            "temporal_bins": temporal_bins,
            "n_samples_per_recording": n_samples,
            "sample_interval_s": FROZEN_SAMPLE_INTERVAL_S,
            "variant": adapter.variant,
            "bounce_budget": adapter.bounce_budget,
            "main_window_halfwidth_bins": surrogate_analysis,
            # estimator 取自**已凍結的 lock**，不是取自 stage_0 區塊。
            # 先前這裡誤填 normaliser（"sigma_MC"），那會讓 artifact 記載一個
            # 它沒有用過的 estimator —— manifest 是 provenance，記錯比不記更糟
            # （NOTE-030 的同一個教訓）。
            "estimator": resolved.payload["estimator"]["selected"],
            "estimator_preregistration_hash": resolved.payload["estimator"][
                "preregistration_hash"
            ],
            "detection_threshold_sigma": resolved.payload["estimator"][
                "detection_threshold_sigma"
            ],
            "min_return_bins": resolved.payload["estimator"]["min_return_bins"],
            "mitsuba": mi.__version__,
            "mitransient": getattr(mitransient, "__version__", "unknown"),
            "scenario_renders": render_calls["count"],
            "render_cache_hits": render_calls["cache_hits"],
        },
        "seeds": {
            "sigma_mc_replicates": frozen_payload["stage_0"]["seed_set"],
            "sweep_common_random_numbers": CRN_SEEDS,
        },
        "observable_definition": {
            "names": result["observable_names"],
            "count": len(result["observable_names"]),
            "construction": "class x feature x statistic, feature order from TOF_SCHEMA",
            "statistics": list(OBSERVABLE_STATISTICS),
            "why_both_statistics": (
                "CAL-PREREG-002 stagewise names both the distribution location "
                "(median) and its dispersion (IQR) as observables. Using the "
                "median alone would give ambient_jitter_relative and "
                "noise_relative_sigma a leverage of ~0 by construction, "
                "contradicting the preregistration's own stage-1 identifiability "
                "argument that those two parameters are separated precisely "
                "because one moves location and the other moves dispersion."
            ),
        },
        "sigma_mc": result["sigma_mc"],
        "sigma_mc_degenerate_observables": result["sigma_mc_degenerate"],
        "initial_mean_over_replicates": result["initial_mean"],
        "crn_baseline": result["crn_baseline"],
        "replicate_values": result["replicate_values"],
        "threshold": result["threshold"],
        "borderline_band": result["borderline_band"],
        "parameters": result["parameters"],
        "collinearity": result["collinearity"],
        "collinearity_threshold": 0.98,
        "declared_channels": {
            stage: {k: list(v) for k, v in spec.items()}
            for stage, spec in DECLARED_CHANNELS.items()
        },
        "declared_channels_note": (
            "A reading of the stagewise observables prose in CAL-PREREG-002, "
            "recorded so it can be checked. It drives only the diagnostic field "
            "max_leverage_declared; the binding admission rule remains the "
            "preregistered literal one (max over every observable)."
        ),
        "admitted_via_undeclared_observable": admitted_elsewhere,
        "sigma_mc_relative_floor": SIGMA_MC_RELATIVE_FLOOR,
        "feasible_domains": result["feasible_domains"],
        "feasibility_rule": {
            "definition": (
                "A value is feasible when all four classes, at the frozen initial "
                "scene and the frozen CRN seeds, produce finite and physically "
                "admissible observables (no exception, no non-finite value)."
            ),
            "derivation": "bisection from the frozen initial value towards the bound",
            "steps": FEASIBILITY_BISECTION_STEPS,
            "relative_tolerance": FEASIBILITY_RELATIVE_TOLERANCE,
            "boundary": (
                "The frozen parameter_ranges are unchanged; a derived domain is "
                "always a subset of the registered range and is recorded with the "
                "rule that produced it. Narrowing a range by hand is forbidden."
            ),
        },
        "classification": {
            "admitted": admitted,
            "admitted_deterministic": admitted_deterministic,
            "gauge_fixed": gauge_fixed,
            "borderline": borderline,
            "undetermined": undetermined,
            "not_fitted": frozen_payload["not_fitted"],
        },
        "anti_leakage_statement": (
            "No calibration-partition value, no held-out record and no real class "
            "mean entered this stage. Every observable was computed from mitsuba "
            "renders of the uncalibrated frozen scene, mapped through the frozen "
            "surrogate. The normaliser is sigma_MC (the simulator's own "
            "seed-to-seed spread), which is exactly why AMD-003 replaced s_f: "
            "s_f would have required reading the calibration split before the "
            "preregistered first access."
        ),
        "claim_boundary": (
            "Stage 0 answers one question only: can the optimizer see this "
            "parameter above the simulator's own Monte-Carlo noise. It says "
            "nothing about whether the parameter matters to the objective J, "
            "nothing about the model's agreement with the real sensor, and it "
            "does not authorise starting stage 1."
        ),
    }


def _stage_of(dimension: str, stage_parameters: dict[str, list[str]]) -> str | None:
    target = _registry_name(dimension)
    for stage_id, members in stage_parameters.items():
        if target in members:
            return stage_id
    return None
