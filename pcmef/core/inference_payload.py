# PC-MEF Research System source maintenance contract
# 上下游: 由 experiments.e2_formal 以 CanonicalCase 加 opaque id 建構，
#         供 agents.observation / physics / visual_semantic / arbitration 與
#         agents.provider 使用；本檔不讀寫檔案，evidence 由 orchestrator 先載入成
#         ndarray 後傳入，輸出直接成為送往外部 LLM 的內容。
# 檔案路徑: pcmef/core/inference_payload.py
# 產生時間: 2026-08-25 21:35 +08:00
# 版本: v0.1.0
# 功能說明: 決定「哪些資訊可以讓 LLM 看到」。它只放行不具名的代號、已載入的影像與
#           ToF 陣列，以及少數可從這些資料本身算出來的品質指標；任何會透露答案的
#           欄位或字串（類別、condition、嚴重度、場景家族、檔名路徑）一律擋下。
# 模組定位: 整個系統最關鍵的洩漏邊界。它「不是」資料轉換工具 —— 不做特徵計算，
#           quality cues 必須由呼叫端事先從 evidence 算好再傳入。
# 主要責任:
#   1. OBSERVABLE_QUALITY_CUES 定義 agent 可見的品質指標 allowlist
#   2. FORBIDDEN_PAYLOAD_FIELDS 定義硬性拒絕的欄位名
#   3. assert_no_forbidden_tokens() 遞迴掃描字串值，擋下用合法欄位夾帶語意的情況
#   4. InferencePayload._check_opaque_id() 拒絕具語意的 canonical ID
#   5. InferencePayload._check_evidence() 拒絕未載入的檔案路徑與非有限值
#   6. to_provider_dict() 在回傳前再掃描一次，供 provider payload snapshot 測試
# 維護提醒:
#   - 不得加入任何 perception 模型導出的量（例如 predictive entropy）；那會讓 s_A
#     與 p_rel 統計相關，破壞 decision-space interpolation 的語意（NOTE-003）。
#   - 不得把檔案路徑字串傳進來代替已載入的張量；provider 永遠不該看到來源路徑。
#   - 不得只檢查 dict key 就認為安全；把語意字串塞進合法欄位的值一樣是洩漏，
#     因此禁止 token 掃描同時涵蓋 key 與 value。
#   - 新增任何 payload 欄位前先問「evaluator 能不能用它反推 ground truth」，能就不准加。
#   - v0.1.0 新增：首版 firewall，quality cue 邊界見 NOTE-003、身分層見 NOTE-004。
# 驗證方式:
#   - py -3.10 -m pytest tests/leakage/test_inference_firewall.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pcmef.core.constants import CLASS_ORDER, E2_CONDITIONS, LEGACY_LABEL_MAP, SPLIT_ROLES

__all__ = [
    "InferenceFirewallViolation",
    "OBSERVABLE_QUALITY_CUES",
    "FORBIDDEN_PAYLOAD_FIELDS",
    "InferencePayload",
    "assert_no_forbidden_tokens",
]


class InferenceFirewallViolation(ValueError):
    """Payload 含有 evaluator-only 資訊，或 quality cue 不在 allowlist 內。

    這個例外一律代表研究完整性事故，不是可以退回預設值的一般錯誤。
    SRC-SAI NFR-04：違規直接 fail run，不允許 silent continue。
    """


# ---------------------------------------------------------------------------
# Observable quality cue allowlist
# ---------------------------------------------------------------------------

# NOTE(NOTE-003): 這份 allowlist 只收「由當前 RGB/ToF evidence 直接可觀測導出」的量。
# predictive entropy 刻意不在此列 —— 它是 perception 模型的輸出函數，不是 evidence
# 可觀測量。把它放進來會讓 s_A 與 p_rel 統計相關，
# 破壞 F=(1-g)p_rel+g*s_A 的 decision-space interpolation 語意，
# 進而讓 G5 相對 G4 的改善失去因果解釋。
# numerical path 的 q_T/q_V 特徵另見 constants.RELIABILITY_FEATURE_CUES。
OBSERVABLE_QUALITY_CUES: frozenset[str] = frozenset(
    {
        # ToF evidence 可觀測統計
        "tof_sigma_like_mean",
        "tof_sigma_like_std",
        "tof_signal_rate_mean",
        "tof_signal_rate_std",
        "tof_ambient_rate_mean",
        "tof_ambient_rate_std",
        "tof_distance_mean",
        "tof_distance_std",
        "tof_temporal_variability",
        "tof_valid_sample_ratio",
        # RGB evidence 可觀測統計
        "vision_blur",
        "vision_brightness",
        "vision_contrast",
        "vision_reflection_ratio",
        "vision_saturated_pixel_ratio",
    }
)


# ---------------------------------------------------------------------------
# 禁止欄位與禁止 token
# ---------------------------------------------------------------------------

# SRC-SAI Appendix G2 的 "intentionally absent" 清單 + Appendix I1 的 hard schema reject。
FORBIDDEN_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "case_id",
        "canonical_case_id",
        "class_label",
        "class",
        "truth",
        "label",
        "condition",
        "severity",
        "corruption_target",
        "corruption_family",
        "parent_scene_family",
        "scenario_seed",
        "scenario_params",
        "split_role",
        "source_role",
        "filename",
        "file_name",
        "path",
        "rgb_path",
        "tof_sequence_path",
        "generator",
        "generator_params",
        "provenance",
        "truth_visibility",
    }
)


def _forbidden_token_patterns() -> tuple[re.Pattern[str], ...]:
    """組出禁止出現在 payload 任何字串值中的 token regex。

    涵蓋四類 class 名稱、前研究原始標籤、E2 condition 名稱、七類 split 角色、
    severity 級別、以及 synthetic/real 檔名前綴。全部以 word-ish boundary 比對，
    避免把 "Empty" 這種常見英文字誤判在自由文字裡 —— 但 payload 的字串值
    本來就只該是 opaque id 與數值單位，出現這些字就是有問題。
    """
    tokens: set[str] = set()
    tokens.update(CLASS_ORDER)
    tokens.update(LEGACY_LABEL_MAP.keys())
    tokens.update(E2_CONDITIONS)
    tokens.update(SPLIT_ROLES)
    tokens.update({"Low", "Mid", "High"})
    tokens.update({"syn_", "real_", "nominal_", "clean_"})
    patterns = []
    for token in sorted(tokens):
        patterns.append(re.compile(re.escape(token), re.IGNORECASE))
    return tuple(patterns)


def _forbidden_field_patterns() -> tuple[re.Pattern[str], ...]:
    """把禁止欄位名做成「值內也不准出現」的 regex。

    只擋 dict key 是不夠的：把 "generated from parent_scene_family f19" 塞進某個
    合法欄位的字串值，一樣是把 benchmark metadata 送到 provider 面前。

    比對採 underscore-aware 邊界 (?<![a-z0-9_]) / (?![a-z0-9_])，
    因為 payload 本身有一個合法欄位叫 opaque_case_id —— 純子字串比對會把它裡面的
    "case_id" 判成違規，讓整個 firewall 對每一筆 payload 都誤報。
    """
    patterns = []
    for token in sorted(FORBIDDEN_PAYLOAD_FIELDS):
        patterns.append(
            re.compile(
                rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])",
                re.IGNORECASE,
            )
        )
    return tuple(patterns)


_FORBIDDEN_TOKEN_PATTERNS = _forbidden_token_patterns() + _forbidden_field_patterns()

# opaque id 必須是非語意的十六進位字串或 UUID 形式（SRC-SAI Appendix I1）。
_OPAQUE_ID_PATTERN = re.compile(
    r"^(?:[0-9a-f]{16,64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def assert_no_forbidden_tokens(value: Any, location: str) -> None:
    """遞迴掃描物件，任何字串命中禁止 token 即拋例外。

    掃描的是「即將送給 provider 的東西」。這是最後一道防線：
    前面的 schema 檢查擋掉已知欄位名，這裡擋掉「用合法欄位夾帶語意字串」的情況，
    例如把 "Conflict-Stress Mid" 塞進某個自由文字欄位。
    """
    if isinstance(value, str):
        for pattern in _FORBIDDEN_TOKEN_PATTERNS:
            if pattern.search(value):
                raise InferenceFirewallViolation(
                    f"forbidden semantic token {pattern.pattern!r} found at {location}; "
                    "inference payloads must not carry class, condition, severity, "
                    "split, or filename semantics"
                )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_PAYLOAD_FIELDS:
                raise InferenceFirewallViolation(
                    f"forbidden field {key!r} found at {location}"
                )
            assert_no_forbidden_tokens(key, f"{location}.<key>")
            assert_no_forbidden_tokens(item, f"{location}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_forbidden_tokens(item, f"{location}[{index}]")


# ---------------------------------------------------------------------------
# InferencePayload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferencePayload:
    """送進 Multi-Agent 的唯一資料結構（SRC-SAI Appendix G2）。

    刻意缺席的欄位：canonical case_id、class_label、condition、severity、
    corruption_target、parent_scene_family、semantic file/path、generator labels。

    Orchestrator 必須「先載入」RGB bytes/tensor 與 ToF array 才呼叫 provider；
    絕不得把 source path 字串送進 LLM（SRC-PLAN §2.2.3）。
    因此本結構持有的是 ndarray，不是路徑。
    """

    opaque_case_id: str
    rgb_tensor: np.ndarray | None = None
    tof_sequence: np.ndarray | None = None
    tof_summary: dict[str, float] | None = None
    quality_cues: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._check_opaque_id()
        self._check_evidence()
        self._check_quality_cues()
        self._check_tof_summary()

    # -- 檢查 -------------------------------------------------------------

    def _check_opaque_id(self) -> None:
        if not isinstance(self.opaque_case_id, str) or not self.opaque_case_id:
            raise InferenceFirewallViolation("opaque_case_id is required")
        if not _OPAQUE_ID_PATTERN.match(self.opaque_case_id):
            raise InferenceFirewallViolation(
                f"opaque_case_id {self.opaque_case_id!r} is not a non-semantic "
                "identifier; it must be a hex digest or UUID produced by the "
                "evaluator-only opaque map"
            )

    def _check_evidence(self) -> None:
        if self.rgb_tensor is None and self.tof_sequence is None:
            raise InferenceFirewallViolation(
                "payload carries no evidence; at least one of rgb_tensor or "
                "tof_sequence must be loaded before calling the provider"
            )
        for name, array in (
            ("rgb_tensor", self.rgb_tensor),
            ("tof_sequence", self.tof_sequence),
        ):
            if array is None:
                continue
            if not isinstance(array, np.ndarray):
                raise InferenceFirewallViolation(
                    f"{name} must be a loaded ndarray, not {type(array).__name__}; "
                    "the provider must never receive a filesystem path"
                )
            if not np.all(np.isfinite(array)):
                raise InferenceFirewallViolation(f"{name} contains NaN or Inf")

    def _check_quality_cues(self) -> None:
        if not isinstance(self.quality_cues, dict):
            raise InferenceFirewallViolation("quality_cues must be a dict")
        unknown = sorted(set(self.quality_cues) - OBSERVABLE_QUALITY_CUES)
        if unknown:
            raise InferenceFirewallViolation(
                f"quality_cues contains non-allowlisted keys {unknown}; cues must be "
                "derived from the current evidence only. Allowed: "
                f"{sorted(OBSERVABLE_QUALITY_CUES)}"
            )
        for key, value in self.quality_cues.items():
            if isinstance(value, bool):
                continue
            if not isinstance(value, (int, float, np.integer, np.floating)):
                raise InferenceFirewallViolation(
                    f"quality_cues[{key!r}] must be numeric or boolean, "
                    f"got {type(value).__name__}"
                )
            if not np.isfinite(float(value)):
                raise InferenceFirewallViolation(
                    f"quality_cues[{key!r}] must be finite, got {value!r}"
                )

    def _check_tof_summary(self) -> None:
        if self.tof_summary is None:
            return
        if not isinstance(self.tof_summary, dict):
            raise InferenceFirewallViolation("tof_summary must be a dict")
        # tof_summary 與 tof_sequence 分欄保存，不使用 union type（SRC-SAI §6）。
        # 兩者可以同時存在（FULL_500x4 模式下 summary 仍可作為輔助），
        # 但 representation_mode 由 Model/Gate Validation Split 決定並凍結，
        # 不由單一 case 臨時選擇。
        assert_no_forbidden_tokens(self.tof_summary, "tof_summary")

    # -- 匯出 -------------------------------------------------------------

    def to_provider_dict(self, include_arrays: bool = False) -> dict[str, Any]:
        """產生即將送給 provider 的 dict，並在回傳前做完整洩漏掃描。

        include_arrays=False 時只回傳形狀摘要，供 provider-payload snapshot test
        使用（SRC-SAI §29 Leakage 測試：provider payload snapshot test）。
        真正送出時由各 agent module 依 provider 格式組裝，但一律先經過本方法的掃描。
        """
        payload: dict[str, Any] = {"opaque_case_id": self.opaque_case_id}
        if self.tof_summary is not None:
            payload["tof_summary"] = dict(self.tof_summary)
        if self.quality_cues:
            payload["quality_cues"] = {
                key: float(value) for key, value in self.quality_cues.items()
            }
        if include_arrays:
            if self.rgb_tensor is not None:
                payload["rgb_tensor"] = self.rgb_tensor
            if self.tof_sequence is not None:
                payload["tof_sequence"] = self.tof_sequence
        else:
            if self.rgb_tensor is not None:
                payload["rgb_shape"] = list(self.rgb_tensor.shape)
            if self.tof_sequence is not None:
                payload["tof_shape"] = list(self.tof_sequence.shape)

        scannable = {
            key: value
            for key, value in payload.items()
            if not isinstance(value, np.ndarray)
        }
        assert_no_forbidden_tokens(scannable, "provider_payload")
        return payload
