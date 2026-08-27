# PC-MEF Research System source maintenance contract
# 上下游: 被 core.schema、core.numeric、core.inference_payload、adapters、surrogate、
#         models、gate、agents、fusion、stats 匯入。本檔不讀寫任何檔案，
#         常數直接流向各模組的 array index、驗證條件與 formal freeze payload。
# 檔案路徑: pcmef/core/constants.py
# 產生時間: 2026-08-25 20:10 +08:00
# 版本: v0.2.0
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
#   - v0.2.0 新增 provenance facet 詞彙、SRC-HANDOFF §8 證據位階與 E1-G08
#     契約版本常數（NOTE-028 / AMD-001）。
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

# legacy 合併程式輸出的 CSV 欄位標題（SRC-NOTION「合併csv、資料後處理」column_order）。
# 順序與 TOF_SCHEMA 一致，供 LegacyCSVAdapter 比對來源欄名。
LEGACY_CSV_COLUMN_TITLES: tuple[str, ...] = (
    "Distance (mm)",
    "Ambient Rate (MCPS)",
    "Signal Rate (MCPS)",
    "Sigma (mm)",
)

# legacy 原始資料的目錄層級：<condition>/<metric_folder>/*.csv。
# 一筆邏輯 recording 被拆成四個 metric 檔分開存放，這是 physical_source_files
# 可以大於 nominal_logical_recordings 的原因（SRC-NOTION 合併程式）。
LEGACY_METRIC_FOLDERS: tuple[str, ...] = ("distance", "ambient", "signal", "sigma")

# NOTE(NOTE-010): Sigma register 在 SRC-NOTION 三份推論程式中不一致。
# 四參數與兩參數用 0x1E，單一參數用 0x18；三者 scaling 皆為 /65536.0。
# 此處僅記錄「已觀察到的候選值」，不代表 dataset 實際採用哪一個 ——
# 那必須由 M0 的 acquisition code + raw CSV range 交叉比對後決定。
SIGMA_REGISTER_CANDIDATES: tuple[int, ...] = (0x18, 0x1E)
SIGMA_RAW_SCALE_DIVISOR: float = 65536.0
RATE_RAW_SCALE_DIVISOR: float = 128.0

# 四特徵 E1 primary 的前置條件（SRC-SAI E1-G08）。
SIGMA_STATUS_RESOLVED: str = "RESOLVED"
SIGMA_STATUS_UNRESOLVED: str = "UNRESOLVED"
SIGMA_STATUS_VALUES: tuple[str, ...] = (
    SIGMA_STATUS_RESOLVED,
    SIGMA_STATUS_UNRESOLVED,
)

# NOTE(NOTE-028): provenance facet 的證據等級詞彙。
# 每個 facet 各自持有一個等級，不再讓數個彼此獨立的事實共用一個
# RESOLVED/UNRESOLVED 旗標 —— 那會讓「可由資料驗證的事」被
# 「原理上無法由資料觀測的事」綁死。
PROVENANCE_CONFIRMED: str = "CONFIRMED"
PROVENANCE_CONFLICT: str = "CONFLICT"
PROVENANCE_RECONSTRUCTED: str = "RECONSTRUCTED"
PROVENANCE_UNKNOWN: str = "UNKNOWN"
PROVENANCE_STATUS_VALUES: tuple[str, ...] = (
    PROVENANCE_CONFIRMED,
    PROVENANCE_CONFLICT,
    PROVENANCE_RECONSTRUCTED,
    PROVENANCE_UNKNOWN,
)

#: 只有 CONFIRMED 能滿足 gate 要求。RECONSTRUCTED 是「推得出來但沒有證據」，
#: 讓它通過等於允許推論冒充證據；要放寬必須是另一次明示的 protocol amendment。
PROVENANCE_GATE_SATISFYING: tuple[str, ...] = (PROVENANCE_CONFIRMED,)

#: SRC-HANDOFF §8 的證據位階。數字越小越強，低位階不得覆寫高位階。
EVIDENCE_RANK_RAW_DATASET: int = 1
EVIDENCE_RANK_ACQUISITION_CODE: int = 2
EVIDENCE_RANK_THESIS_DOC: int = 3
EVIDENCE_RANK_LEGACY_POSTPROCESS: int = 4
EVIDENCE_RANK_LEGACY_INFERENCE_4F: int = 5
EVIDENCE_RANK_LEGACY_INFERENCE_1F2F: int = 6
EVIDENCE_RANK_FILENAME_HINT: int = 7
EVIDENCE_RANK_GUESS: int = 8

#: E1-G08 契約版本。v1 要求 register 位址一併 RESOLVED；
#: v2（AMD-001）拆成 channel semantics + numeric scale，位址允許 CONFLICT/UNKNOWN。
E1_G08_CONTRACT_VERSION: str = "v2"
PROTOCOL_AMENDMENT_ID: str = "AMD-001"


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
