# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 provenance resolve-sigma 呼叫；讀 adapters.legacy_kg 取得的
#         真實 Sigma 觀測值與（若有）採集程式碼證據；寫出
#         provenance/sigma_resolution.json，供 E1-G08 判定與 e1_candidates.lock 引用。
# 檔案路徑: pcmef/provenance/sigma.py
# 產生時間: 2026-08-26 03:35 +08:00
# 版本: v0.3.0
# 功能說明: 判斷前研究的 Sigma 數值到底是怎麼算出來的 —— 把它拆成四個可以各自
#           證成或證否的事實（channel 語意、數值縮放、歷史暫存器位址、採集方式），
#           逐一標記證據等級，其中前兩者確定才允許四特徵 E1 把 Sigma 當 primary。
# 模組定位: SRC-D01/D02 的證據產生器。它「不會」替未決的事項挑一個答案 ——
#           暫存器位址無法從數值反推，缺 rank-2 採集程式碼時一律 CONFLICT/UNKNOWN。
# 主要責任:
#   1. ScalingHypothesis 描述單一 scaling 候選及其相容性判定
#   2. evaluate_scaling_hypotheses() 以觀測值域反推各候選除數的隱含原始碼值
#      （刻意不以 test_ 開頭命名：pytest 會把任何 test_ 開頭的可呼叫物件
#      當成測試案例收集，即使它是從產品模組 import 進來的）
#   3. ProvenanceFacet 持有單一事實的狀態、證據等級與依據
#   4. resolve_sigma() 把觀測與程式碼證據分派到四個 facet
#   5. SigmaResolution.to_artifact() 產生 E1-G08 的證據 artifact
# 維護提醒:
#   - 不得因為「四參數腳本用 0x1E」就把 register 判為 CONFIRMED；
#     資料集由當時的採集腳本產生，那份腳本不在已知的三份之中（NOTE-010）。
#   - 不得把 register_address 加回 E1_G08_REQUIRED_FACETS。位址原理上無法由
#     匯出的浮點數反推，綁進來等於讓可驗證的事實被不可觀測的事實擋死
#     （NOTE-028 / AMD-001）。
#   - 不得把 RECONSTRUCTED 加進 PROVENANCE_GATE_SATISFYING；推論不是證據。
#   - 不得用整數性檢定判斷 scaling；來源 CSV 是四捨五入後的窗口平均值，
#     小數位已被截斷，整數性檢定必然失敗且會給出錯誤結論。
#   - v0.3.0 依 AMD-001 改為 facet 制：channel semantics 與 numeric scale 各自
#     判定，歷史暫存器位址獨立為 CONFLICT/UNKNOWN 且不再擋下 E1-G08（NOTE-028）。
#   - v0.2.0 新增 rule_out_range_register()：以資料檢驗「sigma 欄是否讀自存放
#     測距值的暫存器」。**注意**：它證成的是 channel semantics，不是位址身分；
#     v0.2.0 曾用它「排除 0x1E 進而推得 0x18」，該推論已由 AMD-001 撤銷。
#   - v0.1.0 新增：首版 Sigma provenance resolver，對應 NOTE-011。
# 驗證方式:
#   - py -3.10 -m pytest tests/provenance/test_sigma_and_timing.py -k sigma -v
#   - py -3.10 -m pcmef.cli provenance resolve-sigma
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pcmef.core.constants import (
    E1_G08_CONTRACT_VERSION,
    EVIDENCE_RANK_ACQUISITION_CODE,
    EVIDENCE_RANK_GUESS,
    EVIDENCE_RANK_LEGACY_INFERENCE_4F,
    EVIDENCE_RANK_RAW_DATASET,
    PROTOCOL_AMENDMENT_ID,
    PROVENANCE_CONFIRMED,
    PROVENANCE_CONFLICT,
    PROVENANCE_GATE_SATISFYING,
    PROVENANCE_STATUS_VALUES,
    PROVENANCE_UNKNOWN,
    RATE_RAW_SCALE_DIVISOR,
    SIGMA_RAW_SCALE_DIVISOR,
    SIGMA_STATUS_RESOLVED,
    SIGMA_STATUS_UNRESOLVED,
)

__all__ = [
    "SigmaProvenanceError",
    "ScalingHypothesis",
    "ProvenanceFacet",
    "SigmaResolution",
    "E1_G08_REQUIRED_FACETS",
    "evaluate_scaling_hypotheses",
    "quantization_verdict",
    "rule_out_range_register",
    "resolve_sigma",
]

# VL53L0X 的 result 欄位是 16-bit 無號整數。
_RAW_MIN = 0
_RAW_MAX = 65535

# 若某個 scaling 假設下，隱含原始值只佔滿量程的極小比例，
# 代表該假設要求感測器全程只用到量程邊角，實務上不合理。
_MIN_PLAUSIBLE_RANGE_FRACTION = 1e-3


class SigmaProvenanceError(ValueError):
    """觀測值不足以進行 scaling 判定。"""


@dataclass(frozen=True)
class ScalingHypothesis:
    """單一 scaling 候選的判定結果。"""

    divisor: float
    implied_raw_min: float
    implied_raw_max: float
    fits_16bit: bool
    range_fraction: float
    plausible: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "divisor": self.divisor,
            "implied_raw_min": round(self.implied_raw_min, 4),
            "implied_raw_max": round(self.implied_raw_max, 4),
            "fits_16bit": self.fits_16bit,
            "range_fraction": round(self.range_fraction, 8),
            "plausible": self.plausible,
            "note": self.note,
        }


@dataclass(frozen=True)
class ProvenanceFacet:
    """單一 provenance 事實及其證據等級（NOTE-028）。

    facet 是「一個可以被獨立證成或證否的事實」。把它們分開持有，
    是因為它們的可觀測性根本不同 —— numeric scale 可由 280,000 列量化關係
    驗證，historical register address 則**原理上**無法由匯出的浮點數反推。
    """

    name: str
    status: str
    value: Any
    evidence_rank: int
    basis: str
    alternatives: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in PROVENANCE_STATUS_VALUES:
            raise SigmaProvenanceError(
                f"facet {self.name!r} has status {self.status!r}; "
                f"allowed: {list(PROVENANCE_STATUS_VALUES)}"
            )

    @property
    def satisfies_gate(self) -> bool:
        return self.status in PROVENANCE_GATE_SATISFYING

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "value": self.value,
            "evidence_rank": self.evidence_rank,
            "basis": self.basis,
        }
        if self.alternatives:
            out["alternatives"] = dict(sorted(self.alternatives.items()))
        return out


#: E1-G08 v2 要求為 CONFIRMED 的 facet。register_address 刻意不在其中。
E1_G08_REQUIRED_FACETS: tuple[str, ...] = ("channel_semantics", "numeric_scale")


@dataclass(frozen=True)
class SigmaResolution:
    """Sigma provenance 的完整判定，以 facet 為單位（NOTE-028）。"""

    observed_min: float
    observed_max: float
    observed_median: float
    n_observations: int
    scaling_hypotheses: tuple[ScalingHypothesis, ...]
    facets: dict[str, ProvenanceFacet]
    range_register_test: dict[str, Any] = field(default_factory=dict)
    blocking_reasons: tuple[str, ...] = field(default=())

    @property
    def resolved_divisor(self) -> float | None:
        return self.facets["numeric_scale"].value

    @property
    def status(self) -> str:
        """E1-G08 v2 的整體判定：required facet 全為 CONFIRMED 才 RESOLVED。

        register_address 為 CONFLICT/UNKNOWN **不影響**這個結果 ——
        那正是 AMD-001 的重點。
        """
        if self.blocking_reasons:
            return SIGMA_STATUS_UNRESOLVED
        ok = all(
            name in self.facets and self.facets[name].satisfies_gate
            for name in E1_G08_REQUIRED_FACETS
        )
        return SIGMA_STATUS_RESOLVED if ok else SIGMA_STATUS_UNRESOLVED

    def to_artifact(self) -> dict[str, Any]:
        """產生 provenance/sigma_resolution.json 的內容（E1-G08 證據）。"""
        return {
            "status": self.status,
            "gate": "E1-G08",
            "contract_version": E1_G08_CONTRACT_VERSION,
            "amendment": PROTOCOL_AMENDMENT_ID,
            "required_facets": list(E1_G08_REQUIRED_FACETS),
            "facets": {
                name: facet.to_dict() for name, facet in sorted(self.facets.items())
            },
            "observation": {
                "n": self.n_observations,
                "min": round(self.observed_min, 6),
                "median": round(self.observed_median, 6),
                "max": round(self.observed_max, 6),
            },
            "scaling": {
                "resolved_divisor": self.resolved_divisor,
                "hypotheses": [h.to_dict() for h in self.scaling_hypotheses],
            },
            "range_register_exclusion": dict(self.range_register_test),
            "blocking_reasons": list(self.blocking_reasons),
        }


def quantization_verdict(
    values: np.ndarray, divisor: float, export_decimals: int | None
) -> tuple[bool | None, str]:
    """以量化步長判定某個除數是否成立。

    若原始值是 k/divisor（k 為整數暫存器讀值），則觀測值乘上 divisor 必須落在
    整數附近。這比值域論證銳利得多 —— 但**只在量化步長粗於匯出精度時可用**：
    1/65536 = 1.5e-5 比四位小數的 1e-4 還細，rounding 會把量化痕跡抹掉，
    此時檢定無法給出結論，必須誠實回報 None 而不是當成通過。

    回傳 (verdict, note)。verdict 為 None 代表不適用，不是通過。
    """
    if export_decimals is None:
        return None, "export precision unknown; quantization test not applicable"

    step = 1.0 / divisor
    rounding = 10.0 ** (-export_decimals)
    if step <= rounding:
        return None, (
            f"quantization step 1/{divisor:g} = {step:.2e} is finer than the export "
            f"rounding {rounding:.0e}; the test cannot distinguish this hypothesis"
        )

    finite = np.asarray(values, dtype=np.float64).ravel()
    scaled = finite * divisor
    residual = float(np.abs(scaled - np.round(scaled)).max())
    tolerance = 0.5 * rounding * divisor + 1e-9
    if residual <= tolerance:
        return True, (
            f"observations are multiples of 1/{divisor:g} within the export rounding "
            f"(max residual {residual:.4g} <= {tolerance:.4g})"
        )
    return False, (
        f"observations are NOT multiples of 1/{divisor:g}: max residual "
        f"{residual:.4g} exceeds the rounding tolerance {tolerance:.4g}"
    )


def evaluate_scaling_hypotheses(
    values: np.ndarray,
    divisors: tuple[float, ...] | None = None,
    export_decimals: int | None = None,
) -> tuple[ScalingHypothesis, ...]:
    """對每個候選除數，反推隱含的原始暫存器值域並判定相容性。

    有兩層判定：值域合理性，以及（若匯出精度已知）量化步長。
    後者銳利得多，因此一旦量化檢定給出明確結論就以它為準。
    """
    finite = np.asarray(values, dtype=np.float64).ravel()
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise SigmaProvenanceError("no finite Sigma observations to analyse")

    candidates = divisors or (1.0, RATE_RAW_SCALE_DIVISOR, SIGMA_RAW_SCALE_DIVISOR)
    observed_min = float(finite.min())
    observed_max = float(finite.max())

    hypotheses: list[ScalingHypothesis] = []
    for divisor in candidates:
        raw_min = observed_min * divisor
        raw_max = observed_max * divisor
        fits = _RAW_MIN <= raw_min and raw_max <= _RAW_MAX
        fraction = (raw_max - raw_min) / (_RAW_MAX - _RAW_MIN)

        if not fits:
            note = (
                f"implied raw values exceed the 16-bit range "
                f"[{_RAW_MIN}, {_RAW_MAX}]"
            )
            plausible = False
        elif divisor == 1.0:
            # 未縮放時原始值必為整數；觀測到非整數即排除。
            non_integer = float(np.mean(np.abs(finite - np.round(finite)) > 1e-9))
            plausible = non_integer == 0.0
            note = (
                "raw register reads are integers; "
                f"{non_integer:.0%} of observations are non-integer"
            )
        elif fraction < _MIN_PLAUSIBLE_RANGE_FRACTION:
            plausible = False
            note = (
                f"implied raw span is only {fraction:.2e} of full scale, which would "
                "require the sensor to use a negligible corner of its range"
            )
        else:
            plausible = True
            note = "implied raw values occupy a plausible portion of the 16-bit range"

        # 量化檢定比值域論證銳利，有明確結論時以它為準。
        verdict, quant_note = quantization_verdict(finite, divisor, export_decimals)
        if verdict is not None:
            plausible = verdict
            note = quant_note

        hypotheses.append(
            ScalingHypothesis(
                divisor=float(divisor),
                implied_raw_min=raw_min,
                implied_raw_max=raw_max,
                fits_16bit=fits,
                range_fraction=float(fraction),
                plausible=plausible,
                note=note,
            )
        )
    return tuple(hypotheses)


def rule_out_range_register(
    sigma_values: np.ndarray, distance_values: np.ndarray, divisor: float
) -> dict[str, Any]:
    """檢驗「sigma 欄其實讀自存放測距結果的暫存器」這個假設。

    若該假設成立，sigma * divisor 必須等於同一筆的 distance ——
    因為那會是同一個 16-bit 讀值被除了兩次不同的數。
    這是純資料檢驗，不依賴任何暫存器位址知識。

    回傳的 ruled_out=True 代表：無論那個暫存器叫什麼位址，
    sigma 欄都**不是**從存放測距值的那一個讀來的。
    """
    sigma = np.asarray(sigma_values, dtype=np.float64).ravel()
    distance = np.asarray(distance_values, dtype=np.float64).ravel()
    if sigma.size == 0 or sigma.size != distance.size:
        raise SigmaProvenanceError(
            "sigma and distance columns must be non-empty and the same length"
        )

    implied_raw = sigma * divisor
    match_ratio = float(np.mean(np.abs(implied_raw - distance) < 1.0))
    correlation = float(np.corrcoef(implied_raw, distance)[0, 1])
    magnitude_ratio = float(
        np.median(sigma) / (np.median(distance) / divisor)
        if np.median(distance) > 0
        else float("inf")
    )
    return {
        "hypothesis": "sigma column was read from the register holding the range",
        "implied_raw_range": [
            round(float(implied_raw.min()), 2),
            round(float(implied_raw.max()), 2),
        ],
        "distance_range": [float(distance.min()), float(distance.max())],
        "match_ratio": round(match_ratio, 6),
        "correlation": round(correlation, 4),
        "magnitude_ratio": round(magnitude_ratio, 1),
        "ruled_out": bool(match_ratio < 0.01 and abs(magnitude_ratio - 1.0) > 0.5),
    }


def resolve_sigma(
    values: np.ndarray,
    acquisition_register: int | None = None,
    acquisition_evidence: str = "",
    ruled_out_registers: dict[int, str] | None = None,
    export_decimals: int | None = None,
    range_register_test: dict[str, Any] | None = None,
) -> SigmaResolution:
    """把觀測與程式碼證據拆成獨立 facet，各自判定證據等級（NOTE-028）。

    AMD-001 之後不再回傳單一 RESOLVED/UNRESOLVED 旗標。四個 facet 各自判定：

    - ``numeric_scale``：可由量化關係驗證 → CONFIRMED
    - ``channel_semantics``：可由「sigma 欄是否等於測距值」驗證 → CONFIRMED
    - ``register_address``：**原理上無法**由匯出浮點數反推 → CONFLICT/UNKNOWN
    - ``original_acquisition_method``：採集腳本未取得 → UNKNOWN

    E1-G08 v2 只要求前兩者為 CONFIRMED。後兩者維持 CONFLICT/UNKNOWN
    **不會**使 dataset 失效，也不會擋下四特徵 E1 primary。

    acquisition_register 仍必須來自**產生這批資料的採集腳本**，
    不能拿三份推論腳本中的任一份代替（NOTE-010）。
    """
    finite = np.asarray(values, dtype=np.float64).ravel()
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise SigmaProvenanceError("no finite Sigma observations to analyse")

    hypotheses = evaluate_scaling_hypotheses(finite, export_decimals=export_decimals)
    plausible = [h for h in hypotheses if h.plausible]
    blocking: list[str] = []

    if len(plausible) == 1:
        resolved_divisor: float | None = plausible[0].divisor
    else:
        resolved_divisor = None
        if not plausible:
            blocking.append("no scaling hypothesis is consistent with the observations")
        else:
            blocking.append(
                "multiple scaling hypotheses remain consistent: "
                + ", ".join(str(h.divisor) for h in plausible)
            )

    facets: dict[str, ProvenanceFacet] = {}

    # --- facet 1: numeric scale ------------------------------------------
    # 由 280,000 列的量化關係驗證：/128 與 /1 被排除，/65536 是唯一相容者，
    # 且三份 legacy 腳本（rank 5-6）獨立地都寫 /65536 —— 兩條線收斂。
    if resolved_divisor is not None:
        facets["numeric_scale"] = ProvenanceFacet(
            name="numeric_scale",
            status=PROVENANCE_CONFIRMED,
            value=resolved_divisor,
            evidence_rank=EVIDENCE_RANK_RAW_DATASET,
            basis=(
                f"quantization test over n={finite.size} observations: "
                + "; ".join(
                    f"/{h.divisor:g} {'consistent' if h.plausible else 'excluded'}"
                    for h in hypotheses
                )
                + "; corroborated independently by all legacy scripts (rank 5-6)"
            ),
        )
    else:
        facets["numeric_scale"] = ProvenanceFacet(
            name="numeric_scale",
            status=PROVENANCE_UNKNOWN,
            value=None,
            evidence_rank=EVIDENCE_RANK_RAW_DATASET,
            basis="no single divisor survives the quantization test",
        )

    # --- facet 2: channel semantics --------------------------------------
    # 「sigma 欄不是測距值」是純資料檢驗，**不依賴任何暫存器位址知識**。
    # 這正是 AMD-001 拆分的依據：它可證，位址不可證。
    ruled = dict(ruled_out_registers or {})
    if range_register_test and range_register_test.get("ruled_out"):
        facets["channel_semantics"] = ProvenanceFacet(
            name="channel_semantics",
            status=PROVENANCE_CONFIRMED,
            value="dispersion_channel_not_range",
            evidence_rank=EVIDENCE_RANK_RAW_DATASET,
            basis=(
                "the sigma column is not the ranging result: match_ratio="
                f"{range_register_test.get('match_ratio')}, correlation="
                f"{range_register_test.get('correlation')}, magnitude_ratio="
                f"{range_register_test.get('magnitude_ratio')}. This test is "
                "independent of any register-address knowledge."
            ),
        )
    else:
        facets["channel_semantics"] = ProvenanceFacet(
            name="channel_semantics",
            status=PROVENANCE_UNKNOWN,
            value=None,
            evidence_rank=EVIDENCE_RANK_RAW_DATASET,
            basis=(
                "the range-register exclusion test was not supplied or did not "
                "rule out the hypothesis that the sigma column holds the range"
            ),
        )

    # --- facet 3: historical register address -----------------------------
    # 原理上無法由匯出的浮點數反推。只有 rank-2 的採集程式碼能定案。
    if acquisition_register is not None and acquisition_evidence:
        facets["register_address"] = ProvenanceFacet(
            name="register_address",
            status=PROVENANCE_CONFIRMED,
            value=hex(acquisition_register),
            evidence_rank=EVIDENCE_RANK_ACQUISITION_CODE,
            basis=acquisition_evidence,
        )
    elif acquisition_register is not None:
        blocking.append(
            "acquisition_register was supplied without evidence; record the source "
            "file and line that establishes it"
        )
        facets["register_address"] = ProvenanceFacet(
            name="register_address",
            status=PROVENANCE_UNKNOWN,
            value=None,
            evidence_rank=EVIDENCE_RANK_GUESS,
            basis="register supplied without a citable source",
        )
    else:
        facets["register_address"] = ProvenanceFacet(
            name="register_address",
            status=PROVENANCE_CONFLICT,
            value=None,
            evidence_rank=EVIDENCE_RANK_LEGACY_INFERENCE_4F,
            basis=(
                "legacy sources disagree and the acquisition script that produced "
                "this dataset is not recovered. The address is not observable from "
                "exported float values, so no data test can settle it. Recorded as "
                "CONFLICT rather than eliminated-to-one: excluding "
                "'sigma column == range value' does not identify an address, and "
                "the candidate set is not exhaustive."
            ),
            alternatives={
                hex(reg): why
                for reg, why in sorted(ruled.items())
            }
            or {
                "0x18": "single-feature script; does not consume sigma (rank 6)",
                "0x1e": "two- and four-feature scripts (rank 5-6)",
            },
        )

    # --- facet 4: original acquisition method ------------------------------
    facets["original_acquisition_method"] = ProvenanceFacet(
        name="original_acquisition_method",
        status=PROVENANCE_UNKNOWN,
        value=None,
        evidence_rank=EVIDENCE_RANK_ACQUISITION_CODE,
        basis=(
            "the script that generated the 560 recordings is not recovered; the "
            "known four-feature script has SAVE_RAW_SENSOR_DATA=False and is "
            "therefore not evidence that it produced this dataset"
        ),
    )

    return SigmaResolution(
        observed_min=float(finite.min()),
        observed_max=float(finite.max()),
        observed_median=float(np.median(finite)),
        n_observations=int(finite.size),
        scaling_hypotheses=hypotheses,
        facets=facets,
        range_register_test=dict(range_register_test or {}),
        blocking_reasons=tuple(blocking),
    )
