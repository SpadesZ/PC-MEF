# PC-MEF Research System source maintenance contract
# 上下游: 被 core.schema、core.numeric、core.inference_payload、adapters、surrogate、
#         models、gate、agents、fusion、stats 匯入。本檔不讀寫任何檔案，
#         常數直接流向各模組的 array index、驗證條件與 formal freeze payload。
# 檔案路徑: pcmef/core/constants.py
# 產生時間: 2026-08-25 20:10 +08:00
# 版本: v0.1.0
# 功能說明: 存放全系統共用且不可變的常數 —— 四類液態狀態的順序、ToF 四特徵的欄位
#           順序、三個數值 epsilon、前研究標籤對照表、七類資料角色，以及 E2 的四個
#           condition 與五個比較組名稱。
# 模組定位: 凍結常數的唯一來源。它不是設定檔（可調參數走 core.config），
#           也不做驗證邏輯（驗證走 core.schema 與 core.numeric）。
# 主要責任:
#   1. CLASS_ORDER / N_CLASSES / class_index() 定義四類機率向量的 index 意義
#   2. TOF_SCHEMA / tof_index() 定義四特徵 canonical array 的欄位順序
#   3. TOF_DISPLAY_ORDER 提供報表排版順序，刻意與 array index 分離
#   4. LEGACY_LABEL_MAP 固定 nowater/water/bubble/smoke 到四類英文 label
#   5. EPS_P / EPS_R / EPS_S 提供三個語意不同但數值相同的 epsilon
#   6. RELIABILITY_FEATURE_CUES 宣告 numerical path 的 q_T/q_V 特徵名稱
# 維護提醒:
#   - 不得在其他模組就地重定義 CLASS_ORDER 或 TOF_SCHEMA，也不得以字面字串取代。
#   - 不得用 TOF_DISPLAY_ORDER 取 array index；兩者順序不同，誤用會讓 signal 與
#     ambient 靜默對調，且因兩者皆為 rate、量級相近而不會觸發任何 range 檢查。
#   - 任何常數變更等同 formal scientific identity 變更，必須先立 NOTE 並重新 freeze
#     所有下游 lock。
#   - v0.1.0 新增：首版常數，決策見 NOTE-001（canonical 順序）與 NOTE-002（顯示順序分離）。
# 驗證方式:
#   - py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -k "canonical_order or display_order or legacy_label or class_order"
# ------------------------------------------------------------

from types import MappingProxyType

# ---------------------------------------------------------------------------
# 類別空間
# ---------------------------------------------------------------------------

# 四類液態狀態的凍結順序。這個 tuple 同時是 agent JSON-to-vector bridge 的
# 取值順序（SRC-SAI §20 support_to_vector）與所有 probability vector 的 index 意義。
CLASS_ORDER: tuple[str, ...] = ("Empty", "Water-filled", "Bubbly", "Misty")

N_CLASSES: int = len(CLASS_ORDER)

# 前研究原始標籤 -> 本研究四類 label space（SRC-SAI FR-002）。
# 固定映射，不得由模型自行推定。
LEGACY_LABEL_MAP = MappingProxyType(
    {
        "nowater": "Empty",
        "water": "Water-filled",
        "bubble": "Bubbly",
        "smoke": "Misty",
    }
)


def class_index(class_label: str) -> int:
    """回傳 class 在 CLASS_ORDER 中的 index；未知 label 直接 fail-fast。"""
    try:
        return CLASS_ORDER.index(class_label)
    except ValueError:
        raise ValueError(
            f"unknown class_label {class_label!r}; expected one of {CLASS_ORDER}"
        ) from None


# ---------------------------------------------------------------------------
# ToF 四特徵 schema
# ---------------------------------------------------------------------------

# NOTE(NOTE-001): canonical array order 固定為 Distance -> Ambient -> Signal -> Sigma-like。
# 這是 Notion 四參數推論程式的實際 feature order，不是 SRC-PLAN 正文的敘述順序。
# 任何 flatten / reshape / 取欄都必須經由本常數或 tof_index()，不得依文字順序推測。
TOF_SCHEMA: tuple[str, ...] = (
    "distance_mm",
    "ambient_rate_mcps",
    "signal_rate_mcps",
    "sigma_like",
)

N_TOF_FEATURES: int = len(TOF_SCHEMA)

# 一筆 TofRecording 的 measurement-time 取樣點數（SRC-PLAN §1、SRC-SAI §10）。
# 500 個點描述 recording 內部時序，不是 500 個獨立實驗樣本。
TOF_RECORDING_POINTS: int = 500

TOF_RECORDING_SHAPE: tuple[int, int] = (TOF_RECORDING_POINTS, N_TOF_FEATURES)

# NOTE(NOTE-002): display order 與 array index 是兩個獨立概念。
# 本 tuple 只准用於報表/圖表排版，禁止任何數值路徑用它 enumerate 出 index。
TOF_DISPLAY_ORDER: tuple[str, ...] = (
    "distance_mm",
    "signal_rate_mcps",
    "ambient_rate_mcps",
    "sigma_like",
)

# legacy CSV 第四欄的來源別名。canonical 名稱是 sigma_like；
# sigma_mm_or_surrogate 僅作為 SRC-SAI §8 LegacyCSVAdapter 的來源欄位別名保留。
# 這一欄不得被稱為「真實 VL53L0X internal Sigma」（SRC-SAI §10 禁止做法）。
LEGACY_SIGMA_COLUMN_ALIAS: str = "sigma_mm_or_surrogate"


def tof_index(feature_name: str) -> int:
    """回傳 ToF 特徵在 canonical array 中的 column index；未知欄位 fail-fast。"""
    try:
        return TOF_SCHEMA.index(feature_name)
    except ValueError:
        raise ValueError(
            f"unknown ToF feature {feature_name!r}; expected one of {TOF_SCHEMA}"
        ) from None


# ---------------------------------------------------------------------------
# 數值常數
# ---------------------------------------------------------------------------

# SRC-SAI §16 / §20：三個 epsilon 數值相同但語意不同，分開命名以免被誤用互換。
# EPS_P 用於 probability vector clipping，EPS_R 用於 reliability 權重分母穩定，
# EPS_S 用於 agent support 正規化。三者皆納入 formal freeze hash。
EPS_P: float = 1e-6
EPS_R: float = 1e-6
EPS_S: float = 1e-6

# agent structured output 的 class support 合法範圍（SRC-SAI Executable Schema 2）。
AGENT_SUPPORT_MIN: float = 0.0
AGENT_SUPPORT_MAX: float = 100.0


# ---------------------------------------------------------------------------
# Reliability estimator 的 quality cue 特徵名稱
# ---------------------------------------------------------------------------

# NOTE(NOTE-003): 這份 allowlist 屬於 numerical path 的 q_T / q_V 特徵向量
# （SRC-SAI §15），與 Multi-Agent InferencePayload 的 observable quality cues
# 是兩份不同的清單。此處包含 predictive entropy，agent payload 那份不得包含。
RELIABILITY_FEATURE_CUES = MappingProxyType(
    {
        "tof": (
            "tof_predictive_entropy",
            "tof_sigma_like_mean",
            "tof_sigma_like_std",
            "tof_signal_rate_mean",
            "tof_signal_rate_std",
            "tof_temporal_variability",
        ),
        "vision": (
            "vision_predictive_entropy",
            "vision_blur",
            "vision_brightness",
            "vision_contrast",
        ),
    }
)


# ---------------------------------------------------------------------------
# 七類 split 角色
# ---------------------------------------------------------------------------

# SRC-PLAN §3.1 / SRC-SAI FR-007。順序不代表流程先後，僅為宣告用。
SPLIT_ROLES: tuple[str, ...] = (
    "calibration",
    "perception_train",
    "model_gate_validation",
    "e2_pilot",
    "heldout_real",
    "formal_e2",
    "extension",
)

# E2 四個 benchmark condition（SRC-PLAN §3.3、SRC-SAI §13）。
# Conflict-Stress 是 generation condition 名稱，不等於實際發生跨模態衝突。
E2_CONDITIONS: tuple[str, ...] = (
    "Clean",
    "Vision-degraded",
    "ToF-degraded",
    "Conflict-Stress",
)

# E2 五個比較組（SRC-PLAN §3.3、SRC-SAI §22）。
E2_METHOD_GROUPS: tuple[str, ...] = ("G1", "G2", "G3", "G4", "G5")
