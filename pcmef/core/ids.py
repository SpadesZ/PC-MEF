# PC-MEF Research System source maintenance contract
# 上下游: 由 adapters、simulation.scenario_generator、models.train_split、experiments
#         與 registry.artifacts 呼叫；不讀寫檔案，產出的 ID 字串成為 manifest、
#         split registry、lock payload 與 artifact 目錄名稱的 join key。
# 檔案路徑: pcmef/core/ids.py
# 產生時間: 2026-08-25 21:05 +08:00
# 版本: v0.1.0
# 功能說明: 依固定命名規則組出六種識別碼 —— 前研究 recording 與影像、synthetic
#           scenario、模型 checkpoint、執行批次與 artifact 路徑，並提供格式驗證，
#           讓每筆結果都能沿 ID 追回它的來源。
# 模組定位: canonical（具語意）ID 的唯一產生處。它產生的 ID 屬 evaluator-only；
#           送進 inference 的非語意 ID 由 core.opaque_ids 另外負責。
# 主要責任:
#   1. CLASS_ID_TOKEN / CONDITION_ID_TOKEN / SEVERITY_ID_TOKEN 定義語意 token 表
#   2. real_recording_id() / real_image_id() 產生前研究資料的 immutable ID
#   3. synthetic_scenario_id() 產生 synthetic scenario ID 並擋下 Clean+severity 組合
#   4. model_id() / run_id() 產生 checkpoint 與執行批次 ID
#   5. artifact_path() 組出 case-centric 的 artifact 相對路徑
#   6. is_canonical_id() 供 leakage 測試判斷字串是否具語意
# 維護提醒:
#   - 不得把本模組產生的 ID 直接送入 InferencePayload；它們帶有 class 與 condition
#     語意，等同把 ground truth 交給 provider。
#   - 不得用原始檔名當 ID；檔名可能帶臨時語意或錯字，一旦修正就會讓既有 lock 與
#     result row 失去 join key。ID 與檔名映射必須分開保存。
#   - 不得把易變的軟體版本編進 ID；版本變動會讓同一 scenario 在 split 之間變成兩個
#     身分，破壞 formal E2「G1-G5 使用完全相同 scenario IDs」的要求。
#   - ID 格式一旦用於 formal run 即不可變更；新增 condition 要同步更新 token 表與 regex。
#   - v0.1.0 新增：首版命名規則，condition token 取捨見 NOTE-009。
# 驗證方式:
#   - py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -k "id_formats or severity or canonical_ids or invalid_identifier"
# ------------------------------------------------------------

from __future__ import annotations

import re
from types import MappingProxyType

from pcmef.core.constants import CLASS_ORDER, E2_CONDITIONS

__all__ = [
    "InvalidIdentifier",
    "CLASS_ID_TOKEN",
    "CONDITION_ID_TOKEN",
    "SEVERITY_ID_TOKEN",
    "real_recording_id",
    "real_image_id",
    "synthetic_scenario_id",
    "model_id",
    "run_id",
    "artifact_path",
    "is_canonical_id",
]


class InvalidIdentifier(ValueError):
    """ID 不符合 SRC-SAI §34 的命名契約。"""


# ---------------------------------------------------------------------------
# 語意 token 表
# ---------------------------------------------------------------------------

CLASS_ID_TOKEN = MappingProxyType(
    {
        "Empty": "empty",
        "Water-filled": "water",
        "Bubbly": "bubbly",
        "Misty": "misty",
    }
)

# NOTE(NOTE-009): condition token 以 SRC-SAI Appendix A1 的 CanonicalCase 範例為準
# （syn_conflictstress_bubbly_0042），而非 §34 表格的縮寫範例（syn_conflict_...）。
# A1 是完整 case 契約的實例，§34 的那格是格式示意；採完整 token 才能讓
# "Conflict-Stress" 與任何未來以 conflict 開頭的 condition 不會撞名。
CONDITION_ID_TOKEN = MappingProxyType(
    {
        "Clean": "clean",
        "Vision-degraded": "visiondegraded",
        "ToF-degraded": "tofdegraded",
        "Conflict-Stress": "conflictstress",
    }
)

SEVERITY_ID_TOKEN = MappingProxyType({"Low": "low", "Mid": "mid", "High": "high"})

_SERIAL_DIGITS = 4
_MAX_SERIAL = 10**_SERIAL_DIGITS - 1

_REAL_RECORDING_RE = re.compile(r"^real_tof_(?:empty|water|bubbly|misty)_\d{4}$")
_REAL_IMAGE_RE = re.compile(r"^real_rgb_(?:empty|water|bubbly|misty)_\d{4}$")
_SYNTHETIC_RE = re.compile(
    r"^syn_(?:clean|visiondegraded|tofdegraded|conflictstress)_"
    r"(?:empty|water|bubbly|misty)(?:_(?:low|mid|high))?_\d{4}$"
)
_MODEL_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*_s\d{2}_r\d{2}$")
_RUN_RE = re.compile(r"^[a-z0-9_]+_\d{8}_r\d{2}$")

_CANONICAL_PATTERNS = (
    _REAL_RECORDING_RE,
    _REAL_IMAGE_RE,
    _SYNTHETIC_RE,
    _MODEL_RE,
    _RUN_RE,
)


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------


def _class_token(class_label: str) -> str:
    if class_label not in CLASS_ID_TOKEN:
        raise InvalidIdentifier(
            f"unknown class_label {class_label!r}; expected one of {CLASS_ORDER}"
        )
    return CLASS_ID_TOKEN[class_label]


def _condition_token(condition: str) -> str:
    if condition not in CONDITION_ID_TOKEN:
        raise InvalidIdentifier(
            f"unknown condition {condition!r}; expected one of {E2_CONDITIONS}"
        )
    return CONDITION_ID_TOKEN[condition]


def _serial(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidIdentifier(f"serial must be an int, got {type(value).__name__}")
    if not 0 <= value <= _MAX_SERIAL:
        raise InvalidIdentifier(f"serial must lie in [0, {_MAX_SERIAL}], got {value}")
    return f"{value:0{_SERIAL_DIGITS}d}"


# ---------------------------------------------------------------------------
# ID 產生
# ---------------------------------------------------------------------------


def real_recording_id(class_label: str, serial: int) -> str:
    """前研究 ToF recording 的 immutable ID，例如 real_tof_bubbly_0007。

    這個 ID 與原始 filename 的對照關係必須分開保存
    （SRC-SAI §34「stable，與原 filename mapping 分開保存」）：
    原檔名可能帶有實驗當下的臨時語意或錯字，一旦被當成 ID 使用，
    之後任何檔名修正都會讓既有 lock 與 result row 失去 join key。
    """
    return f"real_tof_{_class_token(class_label)}_{_serial(serial)}"


def real_image_id(class_label: str, serial: int) -> str:
    """前研究 RGB 影像的 immutable ID，例如 real_rgb_misty_0211。"""
    return f"real_rgb_{_class_token(class_label)}_{_serial(serial)}"


def synthetic_scenario_id(
    condition: str, class_label: str, serial: int, severity: str | None = None
) -> str:
    """Synthetic scenario 的 immutable ID，例如 syn_conflictstress_bubbly_0042。

    severity 只在 degradation 類 condition 有意義；Clean 依 SRC-SAI §13 使用
    seed strata 而非 severity，因此傳入 severity 會被拒絕，避免產生
    「Clean 也有嚴重度」這種與 scenario rule 矛盾的 ID。

    ID 本身不含軟體版本（SRC-SAI §34「ID 不含易變軟體版本；版本放 manifest」）：
    版本變動時若 ID 跟著變，同一個 scenario 會在 split 之間變成兩個不同身分，
    直接破壞 formal E2 要求的「G1-G5 使用完全相同 scenario IDs」。
    """
    condition_token = _condition_token(condition)
    class_token = _class_token(class_label)
    if severity is None:
        return f"syn_{condition_token}_{class_token}_{_serial(serial)}"
    if condition == "Clean":
        raise InvalidIdentifier(
            "Clean scenarios use seed strata, not severity; passing a severity "
            "would contradict the frozen scenario rule"
        )
    if severity not in SEVERITY_ID_TOKEN:
        raise InvalidIdentifier(
            f"unknown severity {severity!r}; expected one of {tuple(SEVERITY_ID_TOKEN)}"
        )
    severity_token = SEVERITY_ID_TOKEN[severity]
    return f"syn_{condition_token}_{class_token}_{severity_token}_{_serial(serial)}"


def model_id(branch: str, seed: int, revision: int) -> str:
    """模型 checkpoint ID，例如 tof_1dcnn_s02_r03（branch + seed + revision）。"""
    branch_token = str(branch or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", branch_token):
        raise InvalidIdentifier(
            f"model branch {branch!r} must be lowercase alphanumeric with underscores"
        )
    if not 0 <= seed <= 99 or not 0 <= revision <= 99:
        raise InvalidIdentifier(
            f"model seed/revision must lie in [0, 99], got {seed}/{revision}"
        )
    return f"{branch_token}_s{seed:02d}_r{revision:02d}"


def run_id(experiment: str, date_yyyymmdd: str, revision: int) -> str:
    """執行 ID，例如 e2_formal_20260825_r01。formal 與 test run 必須顯式可辨。"""
    experiment_token = str(experiment or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", experiment_token):
        raise InvalidIdentifier(
            f"experiment {experiment!r} must be lowercase alphanumeric with underscores"
        )
    if not re.fullmatch(r"\d{8}", str(date_yyyymmdd)):
        raise InvalidIdentifier(
            f"date must be YYYYMMDD, got {date_yyyymmdd!r}"
        )
    if not 0 <= revision <= 99:
        raise InvalidIdentifier(f"run revision must lie in [0, 99], got {revision}")
    return f"{experiment_token}_{date_yyyymmdd}_r{revision:02d}"


def artifact_path(case_id: str, artifact_type: str, extension: str) -> str:
    """case-centric artifact 相對路徑：<case_id>/<artifact_type>.<ext>。"""
    if not case_id:
        raise InvalidIdentifier("case_id is required for artifact paths")
    type_token = str(artifact_type or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", type_token):
        raise InvalidIdentifier(
            f"artifact_type {artifact_type!r} must be lowercase alphanumeric"
        )
    ext = str(extension or "").strip().lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]+", ext):
        raise InvalidIdentifier(f"extension {extension!r} is not valid")
    return f"{case_id}/{type_token}.{ext}"


def is_canonical_id(value: str) -> bool:
    """判斷字串是否符合任一 canonical ID 格式。

    供 leakage 測試使用：canonical ID 具語意，若出現在 provider payload 中
    就是 truth firewall 破口（SRC-SAI Appendix I1）。
    """
    if not isinstance(value, str):
        return False
    return any(pattern.match(value) for pattern in _CANONICAL_PATTERNS)
