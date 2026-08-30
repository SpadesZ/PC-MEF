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
#: CAL-PREREG-003 的 stage ID。順序即執行順序：physical scene / media 先，
#: final measurement mappings 後（AMD-004）。**不得與 CAL-PREREG-002 的舊名
#: 混用** —— 舊名為 AMBIENT / SIGNAL_SCALE / GEOMETRY_SURFACE_FOIL /
#: PARTICIPATING_MEDIA / SENSOR_SURROGATE，它們在本 repo 內已全數退役。
DECLARED_CHANNELS: dict[str, dict[str, tuple[str, ...]]] = {
    "SCENE_GEOMETRY_SURFACE_FOIL": {
        "classes": ("Empty",),
        "features": ("distance_mm", "signal_rate_mcps", "sigma_like"),
    },
    "SCENE_PARTICIPATING_MEDIA": {
        "classes": ("Water-filled", "Bubbly", "Misty"),
        "features": ("distance_mm", "signal_rate_mcps", "sigma_like"),
    },
    "MAPPING_AMBIENT": {
        "classes": CLASS_ORDER,
        "features": ("ambient_rate_mcps",),
    },
    "MAPPING_SIGNAL": {
        "classes": CLASS_ORDER,
        "features": ("signal_rate_mcps",),
    },
    "MAPPING_SIGMA": {
        "classes": CLASS_ORDER,
        "features": ("sigma_like",),
    },
}

#: CAL-PREREG-002 的舊 stage ID。留著只為了讓「混用」在測試裡抓得到，
#: **不得**用它們查 DECLARED_CHANNELS。
RETIRED_STAGE_IDS: frozenset[str] = frozenset(
    {
        "AMBIENT",
        "SIGNAL_SCALE",
        "GEOMETRY_SURFACE_FOIL",
        "PARTICIPATING_MEDIA",
        "SENSOR_SURROGATE",
    }
)


def declared_observables(stage_id: str | None) -> list[str]:
    """該階段宣告要最佳化的 observable 名稱。未知階段回傳空清單。"""
    if stage_id in RETIRED_STAGE_IDS:
        raise Stage0Error(
            f"stage id {stage_id!r} is a retired CAL-PREREG-002 name. The protocol "
            "and this runner must not mix old and new stage ids; use the "
            f"CAL-PREREG-003 names {sorted(DECLARED_CHANNELS)}."
        )
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


#: 具名的 borderline 次級裁決規則。**只有列在這裡的參數**可以在落入
#: BORDERLINE band 之後還有機會進入搜尋空間；其餘一律保守 gauge-fix。
#:
#: 這張表是**事前**寫定的：AMD-004 的 generic conservative rule 明令
#: 「不得為個別 borderline parameter 臨時新增 rescue criterion」，
#: 因此在看過某個參數的槓桿值之後才把它加進來，就是那條禁令要防的事。
SECONDARY_ADJUDICATION_RULES: dict[str, str] = {
    "sigma_snr_weight": "S0-B1..B5",
}

#: BORDERLINE 且無具名次級規則時的結局。名稱刻意寫全，讓 artifact 上
#: 「因為分不出來所以固定」與「因為槓桿太小所以固定」看得出差別。
BORDERLINE_DEFAULT_OUTCOME = "GAUGE_FIXED_AT_FROZEN_INITIAL_VALUE"


def classify_leverage(
    leverage: float | None, threshold: float, band: tuple[float, float]
) -> str:
    """依 admission_threshold 判定，不做任何四捨五入。

    回傳的 BORDERLINE 是**原始判定**；是否救得回來由
    `resolve_borderline()` 依事前寫定的具名規則決定。
    """
    low, high = band
    if leverage is None:
        return "UNDETERMINED"
    if low <= leverage <= high:
        return "BORDERLINE"
    if leverage >= threshold:
        return "ADMITTED"
    return "GAUGE_FIXED"


def resolve_borderline(dimension: str, outcome: str) -> tuple[str, str]:
    """AMD-004 B：generic conservative BORDERLINE rule。

    落在 band 內、且在本 amendment **之前**沒有具名 secondary rule 的參數，
    一律固定於凍結的 initial 值。回傳 (outcome, reason)。

    這條規則是 parameter-agnostic 的，這正是重點：一個一個去救 borderline
    參數，等於每次都在已經看過那個數字之後才發明判準。
    """
    if outcome != "BORDERLINE":
        return outcome, ""
    rule = SECONDARY_ADJUDICATION_RULES.get(dimension)
    if rule is None:
        return BORDERLINE_DEFAULT_OUTCOME, (
            "own-stage leverage fell inside the preregistered borderline band and "
            "no named secondary adjudication rule existed for this parameter "
            "before the amendment, so the conservative default applies: hold it "
            "at the frozen initial value. Inventing a rescue criterion for an "
            "individual borderline parameter is forbidden."
        )
    return "BORDERLINE_PENDING_SECONDARY", (
        f"own-stage leverage is inside the borderline band; the preregistered "
        f"secondary criterion {rule} decides this parameter."
    )


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
    secondary_probe: dict[str, dict[str, Any]] | None = None,
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

        # AMD-004 P0-1：**入場判定的分母是該階段自己宣告要最佳化的 observable。**
        # 全域 max 降為診斷。入場的意思是「optimizer 看得見這個參數」，
        # 而 optimizer 只看得見該階段目標函數裡的那些項；一個參數若只在別的
        # 階段才會被最佳化的通道上有反應，它在自己的階段裡就是看不見的。
        stage_declared = declared_observables(entry["stage"])
        if not stage_declared:
            raise Stage0Error(
                f"stage {entry['stage']!r} (dimension {dimension!r}) declares no "
                "observables. Admission is measured on the stage's own declared "
                "channels, so a stage without them cannot be adjudicated; add it to "
                "DECLARED_CHANNELS and to the frozen protocol."
            )
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
        declared_deterministic = [n for n in deterministic if n in stage_declared]
        declared_non_finite = [n for n in non_finite if n in stage_declared]

        entry["declared_observables"] = stage_declared
        entry["max_leverage_declared"] = declared_best
        entry["max_leverage_declared_observable"] = declared_best_name
        entry["deterministic_response_declared"] = declared_deterministic
        # 舊規則（對全部 observable 取 max）的判定保留為診斷，因此
        # 「改判準之後結論變了哪些」永遠可稽核。
        entry["outcome_under_global_scope"] = classify_leverage(best, threshold, band)

        outcome = classify_leverage(declared_best, threshold, band)
        if declared_non_finite:
            outcome = "UNDETERMINED"
            entry["why"] = (
                f"{len(declared_non_finite)} declared observable(s) produced a "
                "non-finite value or non-finite sigma_MC; no classification is "
                "defensible."
            )
        elif declared_deterministic and outcome != "ADMITTED":
            # 無雜訊、卻在整個登記範圍上會動 -> 訊噪比發散。
            # 這是可辨識的最強證據，但它是**具名分支**而不是一個假造的 inf。
            outcome = "ADMITTED_DETERMINISTIC"
            entry["why"] = (
                f"{len(declared_deterministic)} declared observable(s) have sigma_MC "
                f"at or below the degeneracy floor (relative "
                f"{SIGMA_MC_RELATIVE_FLOOR:g}) yet move across the registered range. "
                "A noiseless observable that responds is the strongest possible "
                "identifiability evidence, so this is recorded as its own outcome "
                "rather than as an invented infinite leverage."
            )
        # AMD-004 B：borderline 的保守預設，或交給事前寫定的具名次級規則。
        outcome, borderline_reason = resolve_borderline(dimension, outcome)
        if borderline_reason:
            entry["borderline_resolution"] = borderline_reason
            entry["secondary_rule"] = SECONDARY_ADJUDICATION_RULES.get(dimension)
        entry["outcome"] = outcome

        # AMD-004：響應向量只用**該階段宣告的 observable**。
        # 簡併是關於 optimizer 搜尋空間的敘述，而該階段的目標函數只看得到
        # 自己那些項；用全部 32 個分量算餘弦，會把「在別的階段才會被最佳化的
        # 通道上很像」也算成本階段的簡併。
        #
        # 退化（INERT / DETERMINISTIC_RESPONSE）的分量記 0：後者沒有可用的
        # 正規化尺度，硬給一個大數會讓餘弦被那一個分量獨佔。
        vector = np.array(
            [
                deltas[name] / sigma_mc[name]
                if branches[name] == "RATIO"
                else 0.0
                for name in stage_declared
            ]
        )
        response_vectors[dimension] = vector
        parameters.append(entry)

    # -- S0-B1..B5：sigma 階段的 borderline 次級判準（AMD-004，事前寫定） ----
    # 必須在共線性之前跑完，因為 S0-B4 要看「其餘 admitted 成員」，
    # 而它自己的結論又會改變 admitted 集合。
    outcomes = {e["dimension"]: e["outcome"] for e in parameters}
    for entry in parameters:
        if entry["outcome"] != "BORDERLINE_PENDING_SECONDARY":
            continue
        dimension = entry["dimension"]
        stage_declared = entry["declared_observables"]
        peers = [
            e["dimension"]
            for e in parameters
            if e["stage"] == entry["stage"]
            and e["dimension"] != dimension
            and outcomes.get(e["dimension"], "").startswith("ADMITTED")
        ]
        checks: dict[str, Any] = {}

        # S0-B1 通道支配：最大槓桿必須落在該階段的通道上（此處即 sigma_like）。
        best_obs = entry.get("max_leverage_declared_observable")
        checks["S0-B1_channel_dominance"] = bool(
            best_obs is not None and best_obs in stage_declared
        )

        # S0-B4 共線性：與**每一個** admitted 同階段成員的 |cos| 都必須 < 0.98。
        own_vector = response_vectors.get(dimension)
        worst_cos, worst_peer = 0.0, None
        for peer in peers:
            cosine = cosine_similarity(own_vector, response_vectors[peer])
            if cosine is not None and abs(cosine) > worst_cos:
                worst_cos, worst_peer = abs(cosine), peer
        checks["S0-B4_collinearity"] = bool(worst_cos < 0.98)
        entry["s0b4_worst_abs_cosine"] = worst_cos
        entry["s0b4_worst_peer"] = worst_peer

        # S0-B2 / B3 / B5 需要五點網格與驗證種子；由呼叫端提供的
        # secondary_probe 執行。缺它時**不得**視為通過。
        probe = (secondary_probe or {}).get(dimension)
        if probe is None:
            checks["S0-B2_monotonicity"] = None
            checks["S0-B3_seed_stability"] = None
            checks["S0-B5_no_unphysical_side_effect"] = None
        else:
            checks.update(probe)

        entry["secondary_checks"] = checks
        failed = [k for k, v in checks.items() if v is not True]
        entry["secondary_failed"] = failed
        if failed:
            entry["outcome"] = BORDERLINE_DEFAULT_OUTCOME
            entry["borderline_resolution"] = (
                f"secondary criterion {SECONDARY_ADJUDICATION_RULES[dimension]} did "
                f"not pass ({failed}); the preregistered consequence is to hold the "
                "parameter at its frozen initial value. The measured borderline "
                "leverage is explicitly not a reason to admit."
            )
        else:
            entry["outcome"] = "ADMITTED"
            entry["borderline_resolution"] = (
                f"secondary criterion {SECONDARY_ADJUDICATION_RULES[dimension]} "
                "passed in full"
            )
        outcomes[dimension] = entry["outcome"]

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
                # AMD-004：只有**兩端都仍在搜尋空間內**的配對才形成 formal
                # blocker。簡併是關於 optimizer 搜尋空間的敘述；被 gauge-fix
                # 的參數不在那個空間裡，它與誰共線都不影響任何擬合，
                # 因此把它報成 blocker 等於要求裁決兩個永遠不會一起被擬合的量。
                both_admitted = (
                    outcomes.get(left, "").startswith("ADMITTED")
                    and outcomes.get(right, "").startswith("ADMITTED")
                )
                collinearity.append(
                    {
                        "stage": stage_id,
                        "pair": [left, right],
                        "cosine": cosine,
                        "abs_cosine": None if cosine is None else abs(cosine),
                        "both_admitted": both_admitted,
                        "outcomes": [outcomes.get(left), outcomes.get(right)],
                        # `degenerate` 是**形式上的 blocker 判定**，
                        # 因此它同時要求「夠像」與「兩端都還在搜尋空間裡」。
                        "degenerate": (
                            None
                            if cosine is None
                            else bool(abs(cosine) >= 0.98 and both_admitted)
                        ),
                        "abs_cosine_exceeds_threshold": (
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
        # AMD-004 C：0 維階段是**合法且必須具名記錄**的結局。
        "stage_status": _stage_status(parameters, stage_parameters, outcomes),
    }


def _stage_status(
    parameters: list[dict[str, Any]],
    stage_parameters: dict[str, list[str]],
    outcomes: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """逐階段的維度數與狀態。

    AMD-004 C：沒有任何 admitted 參數的階段**不是錯誤，也不得靜默跳過**。
    它是一個具名結局：物理模型保留、參數維持凍結初值、optimizer 不執行，
    理由是它沒有通過事前寫定的可辨識性入場判定。
    """
    out: dict[str, dict[str, Any]] = {}
    for stage_id in stage_parameters:
        members = [e for e in parameters if e["stage"] == stage_id]
        admitted = sorted(
            e["dimension"] for e in members
            if outcomes.get(e["dimension"], "").startswith("ADMITTED")
        )
        held = sorted(
            e["dimension"] for e in members
            if not outcomes.get(e["dimension"], "").startswith("ADMITTED")
        )
        zero = not admitted
        out[stage_id] = {
            "dimensions": len(admitted),
            "admitted": admitted,
            "held_at_frozen_initial_value": held,
            "status": "NO_FREE_PARAMETERS" if zero else "HAS_FREE_PARAMETERS",
            "optimizer_run": not zero,
            "physical_model_retained": True,
            "values": "frozen initial values" if zero else "fitted within frozen bounds",
            "reason": (
                "failed preregistered identifiability admission"
                if zero
                else "at least one parameter passed own-stage admission"
            ),
        }
    return out


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
    # 兩種固定分開記錄：「槓桿太小」與「分不出來所以保守固定」是不同的理由，
    # 合併成一個數字會讓後者看不見。
    gauge_fixed = sorted(by_outcome.get("GAUGE_FIXED", []))
    gauge_fixed_borderline = sorted(by_outcome.get(BORDERLINE_DEFAULT_OUTCOME, []))
    new_degeneracies = [
        c for c in result["collinearity"] if c.get("degenerate") is True
    ]
    # AMD-004 P0-1 之後這是**回報項而不是 blocker**：舊規則（全域 max）會放行、
    # 新規則（該階段宣告的通道）擋下的那些參數。它們現在確實被擋下了，
    # 記錄下來是為了讓「改判準之後結論變了哪些」看得見。
    would_have_passed_under_global_scope = [
        {
            "dimension": e["dimension"],
            "stage": e["stage"],
            "max_leverage_global": e["max_leverage"],
            "max_leverage_global_observable": e["max_leverage_observable"],
            "max_leverage_declared": e.get("max_leverage_declared"),
            "max_leverage_declared_observable": e.get(
                "max_leverage_declared_observable"
            ),
            "outcome_binding": e["outcome"],
            "outcome_under_global_scope": e.get("outcome_under_global_scope"),
        }
        for e in result["parameters"]
        if str(e.get("outcome_under_global_scope", "")).startswith("ADMITTED")
        and not e["outcome"].startswith("ADMITTED")
    ]

    blockers: list[str] = []
    # AMD-004 B：BORDERLINE 本身不再是 blocker —— generic conservative rule
    # 已經給了它一個決定性的結局（固定於凍結初值）。只有**還沒被解決**的
    # borderline（即仍掛在具名次級規則上）才算未決。
    unresolved_borderline = sorted(
        e["dimension"] for e in result["parameters"]
        if e["outcome"] in {"BORDERLINE", "BORDERLINE_PENDING_SECONDARY"}
    )
    if unresolved_borderline:
        blockers.append(
            f"{len(unresolved_borderline)} dimension(s) are still awaiting a "
            f"secondary adjudication: {unresolved_borderline}"
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
            "The observables each stage declares it optimises, recorded so the "
            "reading can be checked. Under AMD-004 these are the BINDING scope of "
            "the admission rule: a parameter is admitted on max leverage over its "
            "OWN stage's declared observables. The global max over all 32 is kept "
            "as the diagnostic field outcome_under_global_scope."
        ),
        "admission_scope": "own_stage_declared_observables",
        "would_have_passed_under_global_scope": would_have_passed_under_global_scope,
        "sigma_mc_relative_floor": SIGMA_MC_RELATIVE_FLOOR,
        "feasible_domains": result["feasible_domains"],
        "stage_status": result["stage_status"],
        "secondary_adjudication_rules": dict(SECONDARY_ADJUDICATION_RULES),
        "borderline_policy": {
            "default_outcome": BORDERLINE_DEFAULT_OUTCOME,
            "rule": (
                "A parameter whose own-stage leverage falls inside the "
                "preregistered borderline band, and for which no NAMED secondary "
                "adjudication rule existed before AMD-004, is held at its frozen "
                "initial value. Inventing a rescue criterion for an individual "
                "borderline parameter after seeing its leverage is forbidden."
            ),
            "named_exception": "MAPPING_SIGMA.sigma_snr_weight via S0-B1..B5",
        },
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
            "gauge_fixed_at_frozen_initial_value_borderline": gauge_fixed_borderline,
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
