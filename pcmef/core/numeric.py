# PC-MEF Research System source maintenance contract
# 上下游: 由 gate.jsd、gate.entropy、gate.adaptive、fusion.pcmef、fusion.support_bridge、
#         reliability 與 stats 匯入；輸入為記憶體中的機率向量與 reliability 純量，
#         輸出的 D/U/Q/g 與 s_A 直接流入 PC-MEF 融合公式與 ResultRecord。
# 檔案路徑: pcmef/core/numeric.py
# 產生時間: 2026-08-25 20:25 +08:00
# 版本: v0.1.0
# 功能說明: 把論文式 (1)-(8) 用到的機率運算做成單一份實作 —— 機率向量正規化、
#           兩個分佈的分歧度 D、分佈的不確定度 U、reliability 權重，以及 agent
#           回傳的 class support 轉成數值向量。任一條數值契約違反就直接中斷。
# 模組定位: 全系統唯一的機率數值 API。它不決定 alpha/beta/gamma 的值（那是 gate 的
#           搜尋結果），也不做任何 I/O；只保證數值語意一致且可重現。
# 主要責任:
#   1. stabilize_prob() 為唯一的機率 clipping 與正規化入口
#   2. normalized_js_divergence() 計算以 ln 2 正規化的 D，回傳 divergence 而非距離
#   3. normalized_entropy() 計算以 ln 4 正規化的 U
#   4. support_to_vector() / normalize_support() 完成 agent JSON 到 s_A 的橋接
#   5. check_reliability() / reliability_weights() 產生式 (3) 的 w_T 與 w_V
#   6. check_gate_coefficients() 驗證 alpha/beta/gamma 落在 simplex 上
#   7. assert_finite_nonnegative_sum1() 作為 p_rel 與 F 的最後一道 regression invariant
# 維護提醒:
#   - 不得在任何其他模組定義第二份 stabilize_prob，也不得改寫其 eps 預設值；
#     不一致的 eps 會讓 D 的上界偏移，而 alpha/beta/gamma 是在 validation 上選出來的，
#     等於 gate 被一個不會出現在 freeze hash 的數值差異偷偷調參（NOTE-006）。
#   - 不得把 all-zero 的機率或 support 救成 uniform 後繼續；那是語意失敗，必須中斷。
#   - 不得用 top-1 confidence 代替 entropy，也不得回傳 sqrt(JSD) 這個 JS distance。
#   - v0.1.0 新增：首版數值 API，對應 NOTE-006。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_numeric.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np

from pcmef.core.constants import (
    AGENT_SUPPORT_MAX,
    AGENT_SUPPORT_MIN,
    CLASS_ORDER,
    EPS_P,
    EPS_R,
    EPS_S,
    N_CLASSES,
)

__all__ = [
    "InvalidProbability",
    "InvalidAgentSupport",
    "InvalidReliability",
    "InvalidGateCoefficients",
    "NumericalInvariantError",
    "stabilize_prob",
    "normalized_js_divergence",
    "normalized_entropy",
    "support_to_vector",
    "normalize_support",
    "reliability_weights",
    "check_reliability",
    "check_gate_coefficients",
    "assert_finite_nonnegative_sum1",
]


# ---------------------------------------------------------------------------
# 例外型別
# ---------------------------------------------------------------------------
# 皆繼承 ValueError：SRC-SAI Appendix G1 的 reference implementation 丟 ValueError，
# §20 的 pseudocode 丟具名例外，兩種 catch 寫法都要能接住。


class InvalidProbability(ValueError):
    """機率向量不符合 shape=(4,)、finite、non-negative、positive mass 契約。"""


class InvalidAgentSupport(ValueError):
    """Agent class_support 為 all-zero、含 NaN/Inf 或超出 0-100 範圍。"""


class InvalidReliability(ValueError):
    """r_T / r_V 非有限或不在 [0,1]。"""


class InvalidGateCoefficients(ValueError):
    """alpha/beta/gamma 非有限、為負、或總和不等於 1。"""


class NumericalInvariantError(ValueError):
    """最終輸出違反 finite / non-negative / sum=1 的 regression invariant。"""


# ---------------------------------------------------------------------------
# 機率正規化：全系統唯一 API
# ---------------------------------------------------------------------------


def stabilize_prob(p, eps: float = EPS_P) -> np.ndarray:
    """把長度 4 的機率向量 clip 到 eps 之上並重新正規化。

    NOTE(NOTE-006): 這是全系統唯一的 probability 正規化 API。任何模組都必須
    import 本函式，不得自行定義同名函式或改寫 eps 預設值 —— 不一致的 eps 會讓
    D 的上界偏移，而 alpha/beta/gamma 是在 validation 上選出來的，等於 gate 被
    一個不會出現在 freeze hash 的數值差異偷偷調參。

    契約（SRC-SAI §16 invariant 表）：
      - shape 必須是 (4,)
      - 所有元素必須 finite
      - 不得有負值
      - 總和必須 > 0（all-zero 是語意失敗，不得靠 eps 救成 uniform）
    """
    arr = np.asarray(p, dtype=np.float64)
    if (
        arr.shape != (N_CLASSES,)
        or not np.all(np.isfinite(arr))
        or np.any(arr < 0)
        or arr.sum() <= 0
    ):
        raise InvalidProbability(
            f"invalid probability vector: shape={getattr(arr, 'shape', None)} "
            f"finite={bool(np.all(np.isfinite(arr))) if arr.size else False} "
            f"min={float(arr.min()) if arr.size else float('nan')} "
            f"sum={float(arr.sum()) if arr.size else float('nan')}"
        )
    arr = np.maximum(arr, eps)
    return arr / arr.sum()


# ---------------------------------------------------------------------------
# D：normalized Jensen-Shannon divergence
# ---------------------------------------------------------------------------


def normalized_js_divergence(p, q) -> float:
    """回傳以 ln 2 正規化的 Jensen-Shannon divergence，值域 [0,1]。

    SRC-PLAN 式 (5) 與 SRC-SAI Appendix G1：必須以自然對數計算 KL 後除以 ln 2，
    回傳 divergence 本身，**不是** sqrt(JSD) 這個 JS distance。
    兩者在 D 接近 0 時差異巨大（sqrt 會把小 divergence 放大一個數量級），
    直接影響 g = clip(alpha*D + ...) 的分佈。
    """
    p_s = stabilize_prob(p)
    q_s = stabilize_prob(q)
    m = 0.5 * (p_s + q_s)
    divergence = (
        0.5 * np.sum(p_s * np.log(p_s / m)) + 0.5 * np.sum(q_s * np.log(q_s / m))
    ) / np.log(2.0)
    return float(np.clip(divergence, 0.0, 1.0))


# ---------------------------------------------------------------------------
# U：normalized entropy
# ---------------------------------------------------------------------------


def normalized_entropy(p) -> float:
    """回傳 H(p)/ln(4)，值域 [0,1]。

    SRC-PLAN 式 (6)：H 以自然對數計算，分母固定 ln 4（四類）。
    輸入先經 stabilize_prob，因此不會因 log(0) 產生 NaN
    （SRC-SAI §16 invariant：U 不得因 log(0) 產生 NaN）。

    SRC-SAI §16 另有一條禁令：禁止用 top-1 confidence 替代 entropy。
    U 必須是 reliability-fused distribution p_rel 的完整熵。
    """
    p_s = stabilize_prob(p)
    entropy = -np.sum(p_s * np.log(p_s))
    return float(np.clip(entropy / np.log(float(N_CLASSES)), 0.0, 1.0))


# ---------------------------------------------------------------------------
# Agent support 轉向量
# ---------------------------------------------------------------------------


def support_to_vector(agent_output: dict) -> np.ndarray:
    """把 Arbitration 的 class_support keyed object 依 CLASS_ORDER 轉成 float64[4]。

    SRC-PLAN §2.2.3 與 SRC-SAI §21 的 JSON-to-vector bridge：class_support 是
    keyed object，numerical engine **不得依 dict iteration order** 取值，必須以
    CLASS_ORDER 顯式取鍵。Python 3.7+ 的 dict 雖保序，但那個順序是 provider
    回傳 JSON 的鍵序，不是本研究的 class 順序 —— 依賴它等於讓 LLM 的輸出格式
    決定 array 的語意。

    SRC-SAI FR-024：all-zero 或含 NaN/Inf 視為 semantic failure，
    不得靠 EPS_S 轉成 uniform 再繼續。
    """
    if not isinstance(agent_output, dict):
        raise InvalidAgentSupport(
            f"agent_output must be a dict, got {type(agent_output).__name__}"
        )
    support = agent_output.get("class_support")
    if not isinstance(support, dict):
        raise InvalidAgentSupport("agent_output.class_support must be an object")

    missing = [c for c in CLASS_ORDER if c not in support]
    if missing:
        raise InvalidAgentSupport(f"class_support missing required keys: {missing}")
    extra = [k for k in support if k not in CLASS_ORDER]
    if extra:
        raise InvalidAgentSupport(f"class_support has unexpected keys: {extra}")

    try:
        vector = np.asarray(
            [float(support[c]) for c in CLASS_ORDER], dtype=np.float64
        )
    except (TypeError, ValueError) as error:
        raise InvalidAgentSupport(
            f"class_support values must be numeric: {error}"
        ) from None

    if not np.all(np.isfinite(vector)):
        raise InvalidAgentSupport("class_support contains NaN or Inf")
    if np.any(vector < AGENT_SUPPORT_MIN) or np.any(vector > AGENT_SUPPORT_MAX):
        raise InvalidAgentSupport(
            f"class_support values must lie in "
            f"[{AGENT_SUPPORT_MIN}, {AGENT_SUPPORT_MAX}], got {vector.tolist()}"
        )
    if vector.sum() <= 0:
        raise InvalidAgentSupport(
            "class_support sums to zero; this is a semantic failure and must not "
            "be rescued into a uniform distribution"
        )
    return vector


def normalize_support(a_support, eps: float = EPS_S) -> np.ndarray:
    """把 raw support a_A 正規化成 s_A（SRC-PLAN 式 (2)）。

    s_A 是 Normalized Evidence-Support Score，**不是** calibrated posterior
    probability。正規化在應用端做，Agent 不得自行改尺度、eps 或 class order
    （SRC-SAI §21 的 s_A 語意說明）。
    """
    arr = np.asarray(a_support, dtype=np.float64)
    if arr.shape != (N_CLASSES,) or not np.all(np.isfinite(arr)) or np.any(arr < 0):
        raise InvalidAgentSupport(
            f"invalid raw support vector: shape={getattr(arr, 'shape', None)}"
        )
    if arr.sum() <= 0:
        raise InvalidAgentSupport(
            "raw support sums to zero; must not be rescued into uniform"
        )
    shifted = arr + eps
    return shifted / shifted.sum()


# ---------------------------------------------------------------------------
# Reliability 權重與守衛
# ---------------------------------------------------------------------------


def check_reliability(r_t: float, r_v: float) -> tuple[float, float]:
    """驗證 r_T / r_V 為有限且各自落於 [0,1]（SRC-SAI FR-030）。"""
    r_t_f = float(r_t)
    r_v_f = float(r_v)
    if not (np.isfinite(r_t_f) and np.isfinite(r_v_f)):
        raise InvalidReliability(f"r_T/r_V must be finite, got {r_t_f}, {r_v_f}")
    if not (0.0 <= r_t_f <= 1.0 and 0.0 <= r_v_f <= 1.0):
        raise InvalidReliability(
            f"r_T/r_V must lie in [0,1], got r_T={r_t_f}, r_V={r_v_f}"
        )
    return r_t_f, r_v_f


def reliability_weights(r_t: float, r_v: float) -> tuple[float, float]:
    """回傳 (w_T, w_V)，即 SRC-PLAN 式 (3)。

    w_T = (r_T + EPS_R) / (r_T + r_V + 2*EPS_R)，w_V 同理。
    EPS_R 僅作數值穩定用途：r_T = r_V = 0 時分母不為零，權重回到各 0.5。
    """
    r_t_f, r_v_f = check_reliability(r_t, r_v)
    denominator = r_t_f + r_v_f + 2.0 * EPS_R
    return (r_t_f + EPS_R) / denominator, (r_v_f + EPS_R) / denominator


def check_gate_coefficients(alpha: float, beta: float, gamma: float) -> np.ndarray:
    """驗證 alpha/beta/gamma 為有限、非負且總和為 1（SRC-PLAN 式 (7)）。

    容差採 SRC-SAI §20 指定的 np.isclose(sum, 1.0, rtol=0.0, atol=1e-12)。
    刻意用 rtol=0：這三個係數來自 simplex grid search（step 0.1），
    本來就應該精確加總為 1，任何相對容差都只會掩蓋 freeze 內容被改動的情況。
    """
    coefficients = np.asarray([alpha, beta, gamma], dtype=np.float64)
    if not np.all(np.isfinite(coefficients)):
        raise InvalidGateCoefficients(
            f"alpha/beta/gamma must be finite, got {coefficients.tolist()}"
        )
    if np.any(coefficients < 0):
        raise InvalidGateCoefficients(
            f"alpha/beta/gamma must be non-negative, got {coefficients.tolist()}"
        )
    if not np.isclose(coefficients.sum(), 1.0, rtol=0.0, atol=1e-12):
        raise InvalidGateCoefficients(
            f"alpha+beta+gamma must equal 1, got {float(coefficients.sum())!r}"
        )
    return coefficients


# ---------------------------------------------------------------------------
# 最終輸出 regression invariant
# ---------------------------------------------------------------------------


def assert_finite_nonnegative_sum1(vector, label: str = "vector") -> np.ndarray:
    """斷言向量 finite、non-negative 且 sum=1。

    SRC-PLAN Appendix C 的 Audit note 把這條升級為 regression invariant：
    p_rel 與 F 必須 finite、non-negative 且 sum=1。這是 formal run 結束前
    最後一道數值防線，違反時直接 raise，不允許 silent continue（NFR-04）。
    """
    arr = np.asarray(vector, dtype=np.float64)
    if arr.shape != (N_CLASSES,):
        raise NumericalInvariantError(
            f"{label} must have shape ({N_CLASSES},), got {arr.shape}"
        )
    if not np.all(np.isfinite(arr)):
        raise NumericalInvariantError(f"{label} contains NaN or Inf: {arr.tolist()}")
    if np.any(arr < 0):
        raise NumericalInvariantError(f"{label} contains negative values: {arr.tolist()}")
    if not np.isclose(arr.sum(), 1.0, rtol=0.0, atol=1e-9):
        raise NumericalInvariantError(
            f"{label} must sum to 1, got {float(arr.sum())!r}"
        )
    return arr
