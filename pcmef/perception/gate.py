# PC-MEF Research System source maintenance contract
# 上下游: 載入 outputs/perception/ds_v2 凍結的 Vision/ToF 權重與 preprocessing，
#         讀 pcmef.perception.stress 產生的 stress manifest；由 cli 的
#         `perception gate` 與 `perception e2` 呼叫；寫出
#         gate_rule.json 與 formal_e2_report.json。
#         **不讀真實資料、不碰 FORMAL_E1_FINAL、不重訓任何模型。**
# 檔案路徑: pcmef/perception/gate.py
# 產生時間: 2026-08-31 14:10 +08:00
# 版本: v0.2.0
# 功能說明: 溫度校準、可觀測的品質訊號、D/U/Q 門檻與路由規則，以及
#           baseline 與 PC-MEF 的一次性 Formal E2 比較。
# 模組定位: gate 的決策層。門檻與規則**只能**由 gate-validation 決定；
#           Formal E2 只准算一次分數，不得回頭調門檻再跑。
# 主要責任:
#   1. fit_temperature() 只在 gate-validation 上做溫度校準
#   2. calibration_metrics() 回報 NLL / ECE / Brier
#   3. quality_signals() 由**輸入本身**算出品質，不看標籤
#   4. fit_gate_rule() 在 gate-validation 上選 D/U/Q 門檻
#   5. run_formal_e2() 一次性比較 baseline 與 PC-MEF
# 維護提醒:
#   - 不得在 Formal E2 上選任何東西。門檻、溫度、severity、融合權重
#     一律只能由 gate-validation 決定；E2 跑完就是結論，不得改門檻重跑。
#   - 不得讓品質訊號看到標籤或 condition 標記。它必須只由輸入算得出來，
#     否則 gate 是在作弊而不是在偵測劣化。
#   - 不得把溫度校準寫成會改變 argmax 的形式；T > 0 的縮放不改變預測，
#     報告必須逐筆驗證這一點。
#   - 不得在 LLM 未設定時假裝 PC-MEF 的 LLM 仲裁跑過了。仲裁器是可替換的，
#     實際用了哪一個必須寫在報告最上面。
#   - v0.2.0 新增：RELIABILITY_MODEL_VERSION / RELIABILITY_ROUTING_VERSION
#     兩個版本識別常數，供 reliability_final.lock 與 gate.lock 指名（NOTE-050）。
#     行為未變動，兩者只是把既有實作命名成可被 lock 引用的識別。
#   - v0.1.0 新增：首版 D/U/Q gate 與 Formal E2。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_gate.py -v
#   - py -3.10 -m pcmef.cli perception gate --help
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.constants import CLASS_ORDER, N_CLASSES, TOF_SCHEMA

__all__ = [
    "fit_temperature",
    "calibration_metrics",
    "quality_signals",
    "GateRule",
    "fit_gate_rule",
    "apply_gate",
    "run_formal_e2",
    "RELIABILITY_MODEL_VERSION",
    "RELIABILITY_ROUTING_VERSION",
]

ECE_BINS = 15
BOOTSTRAP_REPLICATES = 10000
BOOTSTRAP_SEED = 20260831


# ---------------------------------------------------------------------------
# 溫度校準
# ---------------------------------------------------------------------------


def fit_temperature(logits: np.ndarray, labels: np.ndarray) -> float:
    """在 gate-validation 上以 NLL 最小化求單一溫度 T。

    單一純量而不是逐類向量：向量版可以改變 argmax，也就是可以在
    「校準」的名義下改變預測，而校準的定義是只動信心不動決策。
    """
    from scipy.optimize import minimize_scalar

    def nll(log_t: float) -> float:
        scaled = logits / np.exp(log_t)
        shifted = scaled - scaled.max(axis=1, keepdims=True)
        log_prob = shifted - np.log(np.exp(shifted).sum(axis=1, keepdims=True))
        return float(-log_prob[np.arange(len(labels)), labels].mean())

    result = minimize_scalar(nll, bounds=(-3.0, 3.0), method="bounded")
    return float(np.exp(result.x))


def softmax(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    scaled = np.asarray(logits, dtype=np.float64) / float(temperature)
    shifted = scaled - scaled.max(axis=1, keepdims=True)
    exponentiated = np.exp(shifted)
    return exponentiated / exponentiated.sum(axis=1, keepdims=True)


def calibration_metrics(proba: np.ndarray, labels: np.ndarray) -> dict[str, float]:
    """NLL / ECE / Brier。三個都報，因為它們對不同的失準方式敏感。"""
    n = len(labels)
    clipped = np.clip(proba, 1e-12, 1.0)
    nll = float(-np.log(clipped[np.arange(n), labels]).mean())

    onehot = np.zeros_like(proba)
    onehot[np.arange(n), labels] = 1.0
    brier = float(((proba - onehot) ** 2).sum(axis=1).mean())

    confidence = proba.max(axis=1)
    correct = (proba.argmax(axis=1) == labels).astype(float)
    edges = np.linspace(0.0, 1.0, ECE_BINS + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (confidence > lo) & (confidence <= hi)
        if mask.any():
            ece += mask.mean() * abs(correct[mask].mean() - confidence[mask].mean())
    return {"nll": nll, "ece": float(ece), "brier": brier,
            "accuracy": float(correct.mean()), "mean_confidence": float(confidence.mean())}


# ---------------------------------------------------------------------------
# 品質訊號（只看輸入，不看標籤）
# ---------------------------------------------------------------------------


def quality_signals(rgb: np.ndarray, tof: np.ndarray) -> dict[str, float]:
    """由**輸入本身**算出兩個模態的品質代理量。

    這兩個量必須在推論時算得出來，因此不得引用標籤或 condition 標記 ——
    gate 若知道自己正在看哪一個 condition，它就不是在偵測劣化。

    vision_sharpness  影像的高頻能量（Laplacian 變異數的對數）。
                      失焦會把高頻抹掉，因此它直接偵測 defocus。
    tof_snr           median(signal) / median(ambient) 的對數。
                      ambient flood 會抬高分母、signal attenuation 會壓低
                      分子，兩種 ToF 失效都往同一個方向推。
    """
    image = np.asarray(rgb, dtype=np.float64)
    grey = image.mean(axis=2) if image.ndim == 3 else image
    # 3x3 Laplacian，不引入額外相依。
    kernel = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]])
    padded = np.pad(grey, 1, mode="edge")
    response = sum(
        kernel[i, j] * padded[i : i + grey.shape[0], j : j + grey.shape[1]]
        for i in range(3)
        for j in range(3)
        if kernel[i, j] != 0.0
    )
    scale = max(float(np.abs(grey).mean()), 1e-12)
    sharpness = float(np.log1p(np.var(response / scale)))

    recording = np.asarray(tof, dtype=np.float64)
    signal = float(np.median(recording[:, TOF_SCHEMA.index("signal_rate_mcps")]))
    ambient = float(np.median(recording[:, TOF_SCHEMA.index("ambient_rate_mcps")]))
    snr = float(np.log1p(max(signal, 0.0) / max(ambient, 1e-12)))
    return {"vision_sharpness": sharpness, "tof_snr": snr}


def _entropy(proba: np.ndarray) -> np.ndarray:
    clipped = np.clip(proba, 1e-12, 1.0)
    return -(clipped * np.log(clipped)).sum(axis=1) / np.log(N_CLASSES)


def duq_signals(
    vision_proba: np.ndarray, tof_proba: np.ndarray, quality: dict[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """D / U / Q 三個訊號，全部只由模型輸出與輸入品質算得出。"""
    return {
        # D：兩個機率向量的 total variation 距離。argmax 相同但分佈很不同時
        # 它仍然看得見，而純粹比 argmax 會漏掉那種情況。
        "D": 0.5 * np.abs(vision_proba - tof_proba).sum(axis=1),
        "U_vision": _entropy(vision_proba),
        "U_tof": _entropy(tof_proba),
        "Q_vision": quality["vision_sharpness"],
        "Q_tof": quality["tof_snr"],
    }


# ---------------------------------------------------------------------------
# gate 規則
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateRule:
    """在 gate-validation 上決定的路由規則。"""

    q_vision_threshold: float
    q_tof_threshold: float
    disagreement_threshold: float
    fusion_weight: float
    temperature_vision: float
    temperature_tof: float
    fitted_on: str = "gate_validation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "q_vision_threshold": self.q_vision_threshold,
            "q_tof_threshold": self.q_tof_threshold,
            "disagreement_threshold": self.disagreement_threshold,
            "fusion_weight": self.fusion_weight,
            "temperature_vision": self.temperature_vision,
            "temperature_tof": self.temperature_tof,
            "fitted_on": self.fitted_on,
            "routing": (
                "1) if exactly one modality's quality is below its threshold, trust "
                "the other one (the degradation is observable without labels); "
                "2) else if disagreement D exceeds its threshold, escalate to the "
                "arbiter; 3) otherwise use the fixed-weight probability fusion."
            ),
        }


def apply_gate(
    rule: GateRule,
    vision_proba: np.ndarray,
    tof_proba: np.ndarray,
    signals: dict[str, np.ndarray],
    arbiter: Callable[[int, np.ndarray, np.ndarray, dict[str, float]], np.ndarray] | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """套用路由規則，回傳 (機率, 每筆的路由決策)。"""
    n = len(vision_proba)
    fused = rule.fusion_weight * vision_proba + (1.0 - rule.fusion_weight) * tof_proba
    out = fused.copy()
    route = np.array(["fusion"] * n, dtype=object)

    vision_bad = signals["Q_vision"] < rule.q_vision_threshold
    tof_bad = signals["Q_tof"] < rule.q_tof_threshold

    trust_tof = vision_bad & ~tof_bad
    trust_vision = tof_bad & ~vision_bad
    out[trust_tof] = tof_proba[trust_tof]
    route[trust_tof] = "trust_tof"
    out[trust_vision] = vision_proba[trust_vision]
    route[trust_vision] = "trust_vision"

    # 兩邊品質都過關卻仍然分歧 -> 傳統證據不足以裁決，交給仲裁器。
    contested = (~trust_tof) & (~trust_vision) & (signals["D"] > rule.disagreement_threshold)
    route[contested] = "escalated"
    if arbiter is not None:
        for index in np.flatnonzero(contested):
            out[index] = arbiter(
                int(index),
                vision_proba[index],
                tof_proba[index],
                {k: float(v[index]) for k, v in signals.items()},
            )
    return out, {"route": route, "escalated": contested}


def fit_gate_rule(
    vision_proba: np.ndarray,
    tof_proba: np.ndarray,
    signals: dict[str, np.ndarray],
    labels: np.ndarray,
    temperature_vision: float,
    temperature_tof: float,
    fusion_weight: float,
    progress: Callable[[str], None] | None = None,
) -> tuple[GateRule, dict[str, Any]]:
    """在 gate-validation 上網格搜尋三個門檻。

    目標是**路由後的準確率**，且完全不呼叫仲裁器 —— 門檻必須在沒有仲裁的
    情況下就站得住，否則我們是在用仲裁去補一個壞的路由規則。
    """
    say = progress or (lambda _m: None)
    q_v_grid = np.quantile(signals["Q_vision"], np.linspace(0.05, 0.95, 19))
    q_t_grid = np.quantile(signals["Q_tof"], np.linspace(0.05, 0.95, 19))
    d_grid = np.quantile(signals["D"], np.linspace(0.5, 0.99, 12))

    best, best_accuracy = None, -1.0
    for q_v in q_v_grid:
        for q_t in q_t_grid:
            for d in d_grid:
                candidate = GateRule(
                    q_vision_threshold=float(q_v),
                    q_tof_threshold=float(q_t),
                    disagreement_threshold=float(d),
                    fusion_weight=fusion_weight,
                    temperature_vision=temperature_vision,
                    temperature_tof=temperature_tof,
                )
                proba, _ = apply_gate(candidate, vision_proba, tof_proba, signals)
                accuracy = float((proba.argmax(1) == labels).mean())
                if accuracy > best_accuracy:
                    best, best_accuracy = candidate, accuracy
    say(f"    gate-validation routed accuracy {best_accuracy:.4f}")
    _, decisions = apply_gate(best, vision_proba, tof_proba, signals)
    routes, counts = np.unique(decisions["route"], return_counts=True)
    return best, {
        "search_space": {
            "q_vision_grid": [float(v) for v in q_v_grid],
            "q_tof_grid": [float(v) for v in q_t_grid],
            "disagreement_grid": [float(v) for v in d_grid],
        },
        "objective": "routed accuracy on gate-validation, arbiter disabled",
        "gate_validation_accuracy": best_accuracy,
        "route_counts": {str(r): int(c) for r, c in zip(routes, counts)},
        "escalation_rate": float(decisions["escalated"].mean()),
    }


# ---------------------------------------------------------------------------
# 凍結模型的載入與推論
# ---------------------------------------------------------------------------


def load_frozen_models(ds_dir: str | Path) -> tuple[Any, Any, dict[str, Any]]:
    """載入 ds_v2 訓練出的**凍結**權重與前處理常數。不重訓、不調參。"""
    import torch

    from pcmef.perception.baselines import ToFCNN, VisionCNN

    directory = Path(ds_dir)
    preprocessing = json.loads(
        (directory / "preprocessing.json").read_text(encoding="utf-8")
    )
    vision, tof = VisionCNN(), ToFCNN()
    vision.load_state_dict(torch.load(directory / "vision_weights.pt"))
    tof.load_state_dict(torch.load(directory / "tof_weights.pt"))
    vision.eval()
    tof.eval()
    return vision, tof, preprocessing


def _logits(model: Any, x: np.ndarray) -> np.ndarray:
    import torch

    with torch.no_grad():
        return model(torch.from_numpy(x)).numpy().astype(np.float64)


def _prepare(rows: list[dict[str, Any]], preprocessing: dict[str, Any]):
    """把 stress row 的 rgb/tof 檔案轉成模型輸入。與訓練用同一組常數。"""
    from pcmef.perception.dataset import _rgb_tonemap, _tof_transform

    rgb_spec, tof_spec = preprocessing["rgb"], preprocessing["tof"]
    rgb_batch, tof_batch = [], []
    for row in rows:
        image = _rgb_tonemap(np.load(row["rgb_path"]))
        image = (image - np.asarray(rgb_spec["mean"])) / np.asarray(rgb_spec["std"])
        rgb_batch.append(np.ascontiguousarray(image.transpose(2, 0, 1), dtype=np.float32))

        recording = _tof_transform(np.load(row["tof_path"]))
        recording = (recording - np.asarray(tof_spec["mean"])) / np.asarray(tof_spec["std"])
        tof_batch.append(np.ascontiguousarray(recording.transpose(1, 0), dtype=np.float32))
    return np.stack(rgb_batch), np.stack(tof_batch)


def _quality_batch(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    """品質訊號一律由**原始輸入**算，不用正規化後的張量。"""
    sharpness, snr = [], []
    for row in rows:
        signals = quality_signals(np.load(row["rgb_path"]), np.load(row["tof_path"]))
        sharpness.append(signals["vision_sharpness"])
        snr.append(signals["tof_snr"])
    return {
        "vision_sharpness": np.asarray(sharpness),
        "tof_snr": np.asarray(snr),
    }


# ---------------------------------------------------------------------------
# 仲裁器
# ---------------------------------------------------------------------------


def confidence_weighted_arbiter(
    _index: int, vision_proba: np.ndarray, tof_proba: np.ndarray, signals: dict[str, float]
) -> np.ndarray:
    """決定性的信心加權仲裁。

    **這不是 LLM 仲裁。** LLM 路徑未設定（無 connection、無 llm_runtime.lock，
    且 agents.representation_mode 與 agents.retry.max_attempts 仍待核定，
    NOTE-005 禁止補預設值），因此 PC-MEF 的 LLM 臂**沒有被評估**。
    這個仲裁器是它的位置上一個具名、可重現的替代品：兩邊品質都過關卻仍分歧時，
    以各自的確定度（1 - 正規化熵）加權。
    """
    w_v = max(1.0 - signals["U_vision"], 1e-6)
    w_t = max(1.0 - signals["U_tof"], 1e-6)
    total = w_v + w_t
    return (w_v * vision_proba + w_t * tof_proba) / total


ARBITER_NOTE = (
    "deterministic confidence-weighted arbiter. The LLM/agent arbiter of PC-MEF was "
    "NOT run: no LLM connection is configured, freeze/llm_runtime.lock.json does not "
    "exist, and agents.representation_mode / agents.retry.max_attempts still await "
    "advisor approval (NOTE-005 forbids defaulting them). The LLM arm of PC-MEF is "
    "therefore unevaluated; only the D/U/Q routing is."
)


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------


def paired_bootstrap_delta(
    correct_a: np.ndarray,
    correct_b: np.ndarray,
    units: np.ndarray,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float]:
    """以 base scenario 為單位的成對重抽，估 accuracy 差的 CI。

    重抽單位是 scenario 而不是 row：同一個 scenario 的四個 condition 是
    同一個場景的四種感測情況，把它們當成獨立樣本會低估變異。
    """
    unique = np.unique(units)
    index_by_unit = {u: np.flatnonzero(units == u) for u in unique}
    rng = np.random.default_rng(seed)
    observed = float(correct_a.mean() - correct_b.mean())
    deltas = np.empty(replicates)
    for r in range(replicates):
        drawn = rng.choice(unique, size=len(unique), replace=True)
        rows = np.concatenate([index_by_unit[u] for u in drawn])
        deltas[r] = correct_a[rows].mean() - correct_b[rows].mean()
    lower, upper = np.percentile(deltas, [2.5, 97.5])
    # Cohen's h：兩個比例的效果量，對接近 0/1 的準確率比原始差值穩定。
    pa, pb = float(correct_a.mean()), float(correct_b.mean())
    h = float(2 * np.arcsin(np.sqrt(min(pa, 1.0))) - 2 * np.arcsin(np.sqrt(min(pb, 1.0))))
    return {
        "delta_accuracy": observed,
        "ci_lower": float(lower),
        "ci_upper": float(upper),
        "replicates": replicates,
        "seed": seed,
        "n_units": int(len(unique)),
        "cohens_h": h,
        "significant_at_95": bool(lower > 0.0 or upper < 0.0),
    }


# ---------------------------------------------------------------------------
# gate-validation：溫度、severity、門檻全部在這裡決定
# ---------------------------------------------------------------------------


def _accuracy_at_severity(
    base_manifest: dict[str, Any],
    models: tuple[Any, Any],
    preprocessing: dict[str, Any],
    temperatures: tuple[float, float],
    severity: float,
    degrade: str,
) -> dict[str, float]:
    """在記憶體中施加劣化並量兩個模態各自的準確率。不寫檔。"""
    from pcmef.perception.dataset import _load_exr, _rgb_tonemap, _tof_transform
    from pcmef.perception.stress import STRESS_SEED_BASE, degrade_tof, degrade_vision

    vision_model, tof_model = models
    rgb_spec, tof_spec = preprocessing["rgb"], preprocessing["tof"]
    rgb_batch, tof_batch, labels = [], [], []
    for index, sample in enumerate(base_manifest["samples"]):
        image = _load_exr(sample["rgb"]["exr_path"])
        recording = np.load(sample["tof"]["path"])
        seed = STRESS_SEED_BASE + index
        if degrade == "vision":
            image, _ = degrade_vision(image, severity, seed)
        else:
            recording, _ = degrade_tof(recording, severity, seed + 1)

        toned = _rgb_tonemap(image)
        toned = (toned - np.asarray(rgb_spec["mean"])) / np.asarray(rgb_spec["std"])
        rgb_batch.append(np.ascontiguousarray(toned.transpose(2, 0, 1), dtype=np.float32))
        transformed = _tof_transform(recording)
        transformed = (transformed - np.asarray(tof_spec["mean"])) / np.asarray(tof_spec["std"])
        tof_batch.append(np.ascontiguousarray(transformed.transpose(1, 0), dtype=np.float32))
        labels.append(sample["class_index"])

    labels = np.asarray(labels)
    v = softmax(_logits(vision_model, np.stack(rgb_batch)), temperatures[0])
    t = softmax(_logits(tof_model, np.stack(tof_batch)), temperatures[1])
    return {
        "vision": float((v.argmax(1) == labels).mean()),
        "tof": float((t.argmax(1) == labels).mean()),
    }


#: 被剔除的 family 在 split mapping 裡的標記。保留而不是刪除 ——
#: 它歷史上存在過，而且 gate threshold 曾經在它身上擬合過。
EXCLUDED_DUPLICATE_IDENTITY = "EXCLUDED_DUPLICATE_IDENTITY"


def exclude_duplicate_identity_families(
    manifest: dict[str, Any], ds_dir: str | Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """剔除 physical_scene_family 與 ds_v2 重複的 gate-validation family。

    規則（NOTE-048，**在看到任何新分數之前**寫定）：以 hash 相等為準，
    不以索引、不以類別、不以個案判斷。

    為什麼非剔不可：ds_v2 的 12/4/4 family 分法讓重複的那一個落在
    perception development/validation。gate-validation 再看到同一個物理場景
    （只換 realization），我們宣稱的 development-stage family independence
    就不成立 —— 那不是分數問題，是 gate threshold 在一個它不該看過的場景上
    擬合這件事本身。

    刻意**不**做的事：不因對稱而順手剔掉其他類別的同號 family、
    不補新 family 湊數、不重訓任何模型。
    """
    ds_manifest = json.loads(
        (Path(ds_dir) / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    ds_identities = {
        s["physical_scene_family"]
        for s in (ds_manifest.get("samples") or ds_manifest.get("rows") or [])
        if "physical_scene_family" in s
    }

    kept, dropped = [], []
    for sample in manifest["samples"]:
        if sample.get("physical_scene_family") in ds_identities:
            dropped.append(sample)
        else:
            kept.append(sample)

    excluded_families = sorted(
        {(s["class_label"], s["family_index"]) for s in dropped}
    )
    lines = [
        f"corrective rule (NOTE-048): dropped {len(dropped)} sample(s) from "
        f"{len(excluded_families)} family(ies) duplicating a ds_v2 physical scene"
    ]
    for class_label, family_index in excluded_families:
        lines.append(f"  excluded {class_label} f{family_index:02d}")
    if not dropped:
        lines = ["corrective rule (NOTE-048): no duplicate identity found"]

    return {**manifest, "samples": kept}, {
        "rule": (
            "exclude any gate-validation family whose physical_scene_family hash "
            "also appears in ds_v2"
        ),
        "rule_recorded_in": "docs/NOTES.md NOTE-048",
        "excluded_families": [
            {"class_label": c, "family_index": f, "status": EXCLUDED_DUPLICATE_IDENTITY}
            for c, f in excluded_families
        ],
        "excluded_sample_count": len(dropped),
        "remaining_sample_count": len(kept),
        "report_lines": lines,
    }


def run_gate_validation(
    ds_dir: str | Path,
    gate_val_dir: str | Path,
    out_dir: str | Path,
    fusion_weight: float,
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """溫度校準 -> severity 選定 -> gate 門檻。全部只看 gate-validation。"""
    from pcmef.perception.stress import SEVERITY_LADDER, build_stress_dataset, select_severity

    say = progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    vision_model, tof_model, preprocessing = load_frozen_models(ds_dir)
    base = json.loads(
        (Path(gate_val_dir) / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    base, exclusion = exclude_duplicate_identity_families(base, ds_dir)
    for line in exclusion["report_lines"]:
        say(line)

    # -- 1. 溫度校準（clean gate-validation） -------------------------------
    say("temperature scaling on clean gate-validation")
    clean_rows = [
        {"rgb_path": s["rgb"]["exr_path"], "tof_path": s["tof"]["path"]}
        for s in base["samples"]
    ]
    from pcmef.perception.dataset import _load_exr

    rgb_batch, tof_batch = [], []
    from pcmef.perception.dataset import _rgb_tonemap, _tof_transform

    for s in base["samples"]:
        image = _rgb_tonemap(_load_exr(s["rgb"]["exr_path"]))
        image = (image - np.asarray(preprocessing["rgb"]["mean"])) / np.asarray(
            preprocessing["rgb"]["std"]
        )
        rgb_batch.append(np.ascontiguousarray(image.transpose(2, 0, 1), dtype=np.float32))
        rec = _tof_transform(np.load(s["tof"]["path"]))
        rec = (rec - np.asarray(preprocessing["tof"]["mean"])) / np.asarray(
            preprocessing["tof"]["std"]
        )
        tof_batch.append(np.ascontiguousarray(rec.transpose(1, 0), dtype=np.float32))
    labels = np.asarray([s["class_index"] for s in base["samples"]])

    v_logits = _logits(vision_model, np.stack(rgb_batch))
    t_logits = _logits(tof_model, np.stack(tof_batch))
    t_vision = fit_temperature(v_logits, labels)
    t_tof = fit_temperature(t_logits, labels)

    calibration = {}
    for name, logits, temperature in (
        ("vision", v_logits, t_vision), ("tof", t_logits, t_tof)
    ):
        before = softmax(logits, 1.0)
        after = softmax(logits, temperature)
        calibration[name] = {
            "temperature": temperature,
            "before": calibration_metrics(before, labels),
            "after": calibration_metrics(after, labels),
            "predictions_unchanged": bool((before.argmax(1) == after.argmax(1)).all()),
        }
        say(
            f"  {name}: T={temperature:.4f}  NLL {calibration[name]['before']['nll']:.4f}"
            f" -> {calibration[name]['after']['nll']:.4f}  "
            f"ECE {calibration[name]['before']['ece']:.4f}"
            f" -> {calibration[name]['after']['ece']:.4f}"
        )

    # -- 2. severity 選定（事前判準，不看 gate/融合） -----------------------
    say("severity ladder")
    ladders = {}
    for modality in ("vision", "tof"):
        by_severity = {}
        for severity in SEVERITY_LADDER:
            by_severity[severity] = _accuracy_at_severity(
                base, (vision_model, tof_model), preprocessing,
                (t_vision, t_tof), severity, modality,
            )
            say(
                f"  {modality} severity {severity}: "
                f"vision {by_severity[severity]['vision']:.3f} "
                f"tof {by_severity[severity]['tof']:.3f}"
            )
        ladders[modality] = select_severity(by_severity, modality)
        say(f"  -> {modality} severity {ladders[modality]['selected_severity']}")

    vision_severity = ladders["vision"]["selected_severity"]
    tof_severity = ladders["tof"]["selected_severity"]

    # -- 3. 產生 gate-validation 的 stress set 並選門檻 ---------------------
    say("building the gate-validation stress set")
    stress = build_stress_dataset(
        base, vision_severity, tof_severity, out / "stress", code_version, say
    )
    rows = stress["rows"]
    rgb_x, tof_x = _prepare(rows, preprocessing)
    y = np.asarray([r["class_index"] for r in rows])
    v_proba = softmax(_logits(vision_model, rgb_x), t_vision)
    t_proba = softmax(_logits(tof_model, tof_x), t_tof)
    signals = duq_signals(v_proba, t_proba, _quality_batch(rows))

    say("fitting the D/U/Q thresholds")
    rule, search = fit_gate_rule(
        v_proba, t_proba, signals, y, t_vision, t_tof, fusion_weight, say
    )

    from pcmef.perception.stress import condition_summary

    conditions = condition_summary(
        rows, v_proba.argmax(1) == y, t_proba.argmax(1) == y
    )

    document = {
        "report_id": "gate_validation",
        "scientific_result": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "frozen_models": {
            "source_run": Path(ds_dir).as_posix(),
            "retrained": False,
            "note": "weights loaded from the ds_v2 run; no re-training, no re-tuning",
        },
        # 剔除紀錄與擬合結果放在一起：任何讀這份 gate rule 的人，都必須
        # 同時看得到它是在哪一個子集上擬合的。
        "family_exclusion": exclusion,
        "calibration": calibration,
        "severity_selection": ladders,
        "stress": {
            "manifest": (out / "stress" / "stress_manifest.json").as_posix(),
            "counts": stress["counts"],
            "severity": stress["severity"],
            "condition_summary": conditions,
        },
        "gate_rule": rule.to_dict(),
        "gate_search": search,
        "arbiter": ARBITER_NOTE,
    }
    (out / "gate_rule.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document


# ---------------------------------------------------------------------------
# Formal E2：一次性，門檻全部來自 gate-validation
# ---------------------------------------------------------------------------


def run_formal_e2(
    ds_dir: str | Path,
    gate_rule_path: str | Path,
    e2_dir: str | Path,
    out_dir: str | Path,
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """比較 baseline 與 PC-MEF。**只准跑一次，不得改門檻重跑。**"""
    from pcmef.perception.baselines import evaluate
    from pcmef.perception.stress import build_stress_dataset, condition_summary

    say = progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    gate_doc = json.loads(Path(gate_rule_path).read_text(encoding="utf-8"))
    spec = gate_doc["gate_rule"]
    rule = GateRule(
        q_vision_threshold=spec["q_vision_threshold"],
        q_tof_threshold=spec["q_tof_threshold"],
        disagreement_threshold=spec["disagreement_threshold"],
        fusion_weight=spec["fusion_weight"],
        temperature_vision=spec["temperature_vision"],
        temperature_tof=spec["temperature_tof"],
    )
    severity = gate_doc["stress"]["severity"]
    say(
        f"gate rule from gate-validation: Qv<{rule.q_vision_threshold:.3f} "
        f"Qt<{rule.q_tof_threshold:.3f} D>{rule.disagreement_threshold:.3f} "
        f"w={rule.fusion_weight}"
    )

    vision_model, tof_model, preprocessing = load_frozen_models(ds_dir)
    base = json.loads(
        (Path(e2_dir) / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    say("building the Formal E2 stress set at the gate-validation severities")
    stress = build_stress_dataset(
        base, severity["vision"], severity["tof"], out / "stress", code_version, say
    )
    rows = stress["rows"]
    rgb_x, tof_x = _prepare(rows, preprocessing)
    y = np.asarray([r["class_index"] for r in rows])
    units = np.asarray([r["base_scenario_id"] for r in rows])

    v_proba = softmax(_logits(vision_model, rgb_x), rule.temperature_vision)
    t_proba = softmax(_logits(tof_model, tof_x), rule.temperature_tof)
    signals = duq_signals(v_proba, t_proba, _quality_batch(rows))

    fused = rule.fusion_weight * v_proba + (1.0 - rule.fusion_weight) * t_proba
    pcmef_proba, decisions = apply_gate(
        rule, v_proba, t_proba, signals, arbiter=confidence_weighted_arbiter
    )

    arms = {
        "vision_only": v_proba,
        "tof_only": t_proba,
        "fixed_fusion": fused,
        "pcmef_gate": pcmef_proba,
    }
    results = {name: evaluate(name, proba, y) for name, proba in arms.items()}
    correct = {name: (proba.argmax(1) == y) for name, proba in arms.items()}

    conditions = np.array([r["condition"] for r in rows])
    per_condition = {
        name: {
            condition: {
                "n": int((conditions == condition).sum()),
                "accuracy": float(correct[name][conditions == condition].mean()),
                "errors": int((~correct[name][conditions == condition]).sum()),
            }
            for condition in sorted(set(conditions))
        }
        for name in arms
    }

    comparisons = {
        f"pcmef_gate_vs_{baseline}": paired_bootstrap_delta(
            correct["pcmef_gate"], correct[baseline], units
        )
        for baseline in ("vision_only", "tof_only", "fixed_fusion")
    }

    routes, counts = np.unique(decisions["route"], return_counts=True)
    escalation_rate = float(decisions["escalated"].mean())

    document = {
        "report_id": "formal_e2",
        "scientific_result": True,
        "one_shot": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "arbiter": ARBITER_NOTE,
        "llm_arm_evaluated": False,
        "gate_rule": rule.to_dict(),
        "gate_rule_source": Path(gate_rule_path).as_posix(),
        "severity_source": "gate-validation; not re-selected on E2",
        "dataset": {
            "base": Path(e2_dir).as_posix(),
            "family_indices": base["family_indices"],
            "total_rows": len(rows),
            "counts": stress["counts"],
            "condition_summary": condition_summary(
                rows, correct["vision_only"], correct["tof_only"]
            ),
        },
        "results": results,
        "per_condition": per_condition,
        "comparisons": comparisons,
        "routing": {
            "counts": {str(r): int(c) for r, c in zip(routes, counts)},
            "escalation_rate": escalation_rate,
            "llm_call_rate": escalation_rate,
            "llm_calls_actually_made": 0,
            "note": (
                "escalation_rate is what the LLM call rate WOULD be. No LLM call was "
                "made: the arbiter is the deterministic stand-in described above."
            ),
        },
        "claim_boundary": (
            "Synthetic data only. Accuracy here is over four classes under two "
            "preregistered nuisance axes and sensor-level degradations; it says "
            "nothing about real-sensor performance, and the LLM arm of PC-MEF is "
            "unevaluated."
        ),
    }
    (out / "formal_e2_report.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document


# ---------------------------------------------------------------------------
# 可靠度 q_m —— 與 p_m(y|x) 完全分離
# ---------------------------------------------------------------------------

#: q_m 的來源白名單。**max softmax 不在其中，也永遠不得加入。**
#:
#: pilot（families 28-35）確立的 negative result：溫度校準把 ToF 調到
#: T = 0.0498，於是 ToF 一旦劣化就「高信心地錯」，而信心加權仲裁正好被
#: 拉向錯的那一邊（conflict 0.302，單看 Vision 反而有 0.677）。
#: 那不是仲裁器寫壞了，是**把預測信心當成感測可靠度**這個假設本身錯了。
RELIABILITY_EVIDENCE: tuple[str, ...] = (
    "sensor_quality",       # Q：由輸入本身算出的劣化證據（銳利度 / SNR）
    "degradation_margin",   # Q 相對於 gate-validation 門檻的裕度
    "cross_modal_support",  # D：另一個模態是否支持（同意時互相加分）
)

#: 可靠度映射的版本識別。名稱定義在**程式碼**而不是 lock 檔裡 ——
#: reliability_final.lock 會記下這個字串，若它只存在於 lock，那個版本號
#: 就沒有任何東西可以對照（NOTE-050，與 SELECTIVE_ESCALATION_BRIDGE_VERSION 同一做法）。
RELIABILITY_MODEL_VERSION = "reliability_margin_support_v1"

#: 可靠度路由的版本識別。與上面同一個理由；它指的是 reliability_route()，
#: **不是** apply_gate() 的品質門檻路由（後者是 pilot 用的那一條）。
RELIABILITY_ROUTING_VERSION = "reliability_routing_v1"


@dataclass(frozen=True)
class ReliabilityModel:
    """由 gate-validation 的 clean 分佈定出的可靠度標定。

    只記錄「乾淨時 Q 長什麼樣」與門檻，因此 q_m 是一個**相對於已知乾淨
    基線的感測品質分數**，而不是模型對自己的信心。
    """

    q_vision_clean_median: float
    q_tof_clean_median: float
    q_vision_degraded_anchor: float
    q_tof_degraded_anchor: float
    q_vision_scale: float
    q_tof_scale: float
    fitted_on: str = "gate_validation_clean"

    def to_dict(self) -> dict[str, Any]:
        return {
            "q_vision_clean_median": self.q_vision_clean_median,
            "q_tof_clean_median": self.q_tof_clean_median,
            "q_vision_degraded_anchor": self.q_vision_degraded_anchor,
            "q_tof_degraded_anchor": self.q_tof_degraded_anchor,
            "q_vision_scale": self.q_vision_scale,
            "q_tof_scale": self.q_tof_scale,
            "fitted_on": self.fitted_on,
            "anchor_rule": (
                "the sigmoid is centred on the clean 5th percentile and scaled by "
                "the clean IQR, both from the gate-validation CLEAN subset only. It "
                "deliberately does NOT reuse the gate's q thresholds: those were "
                "fitted as routing cuts on the full stress set, and the ToF one "
                "(17.008) sits ABOVE the clean median (15.341), so using it as a "
                "reliability centre would score even clean ToF as unreliable."
            ),
            "evidence_sources": list(RELIABILITY_EVIDENCE),
            "forbidden_sources": [
                "max softmax probability",
                "predictive entropy of the modality being scored",
                "anything derived from p_m(y|x)",
            ],
            "why": (
                "The pilot established that classifier confidence is not modality "
                "reliability: temperature scaling made ToF sharp (T=0.0498), so a "
                "degraded ToF is confidently wrong and a confidence-weighted arbiter "
                "is pulled towards the wrong modality. q_m therefore reads only "
                "sensor-side degradation evidence and cross-modal support."
            ),
        }


def reliability_scores(
    signals: dict[str, np.ndarray], model: ReliabilityModel
) -> dict[str, np.ndarray]:
    """q_vision / q_tof in [0, 1]。**完全不看 p_m(y|x)。**

    每個模態的 q 由兩部分相乘：
      * degradation margin：Q 相對於門檻與乾淨中位數的位置，
        壓成 [0, 1]。低於門檻 -> 迅速趨近 0。
      * cross-modal support：兩個模態一致時（D 小）互相加分。
        一致不能證明兩個都對，但它是**獨立於各自信心**的證據，
        因此可以進 q；不一致時這一項退為中性 0.5 而不是 0，
        否則 conflict 會把兩邊的 q 一起壓垮而讓 q 失去區分力。
    """
    def margin(q: np.ndarray, anchor: float, scale: float) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(q - anchor) / max(scale, 1e-9)))

    m_vision = margin(signals["Q_vision"], model.q_vision_degraded_anchor,
                      model.q_vision_scale)
    m_tof = margin(signals["Q_tof"], model.q_tof_degraded_anchor, model.q_tof_scale)
    support = 0.5 + 0.5 * (1.0 - np.clip(signals["D"], 0.0, 1.0))
    return {
        "q_vision": np.clip(m_vision * support, 0.0, 1.0),
        "q_tof": np.clip(m_tof * support, 0.0, 1.0),
    }


def fit_reliability_model(
    signals: dict[str, np.ndarray], clean_mask: np.ndarray, rule: GateRule
) -> ReliabilityModel:
    """由 gate-validation 的 **clean** 子集定出可靠度標定。

    刻意**不**沿用 gate 的 q 門檻。那兩個值是在完整 stress set 上以
    routing accuracy 搜出來的**切點**，不是「劣化邊界」——實測 ToF 那個
    切點 17.008 落在 clean 中位數 15.341 **之上**，拿它當可靠度中心會讓
    乾淨的 ToF 也被評為不可靠（實測 q_tof = 0.180）。
    改以 clean 分佈自己的第 5 百分位當中心、IQR 當尺度：
    「這個輸入看起來還像不像乾淨時的樣子」。
    """
    def calibrate(values: np.ndarray) -> tuple[float, float]:
        clean = values[clean_mask]
        p5, p25, p75 = np.percentile(clean, [5.0, 25.0, 75.0])
        return float(p5), float(max(p75 - p25, 1e-9))

    v_anchor, v_scale = calibrate(signals["Q_vision"])
    t_anchor, t_scale = calibrate(signals["Q_tof"])
    return ReliabilityModel(
        q_vision_clean_median=float(np.median(signals["Q_vision"][clean_mask])),
        q_tof_clean_median=float(np.median(signals["Q_tof"][clean_mask])),
        q_vision_degraded_anchor=v_anchor,
        q_tof_degraded_anchor=t_anchor,
        q_vision_scale=v_scale,
        q_tof_scale=t_scale,
    )


#: 可靠度路由的三條分支（事前宣告，與 pilot 的規則同一套門檻）。
RELIABILITY_ROUTING = (
    "1) exactly one modality reliable -> that modality dominates; "
    "2) both reliable and they agree -> traditional fixed fusion; "
    "3) both weak, or both reliable but disagreeing -> escalate to the arbiter."
)

RELIABLE_MARGIN = 0.5


def reliability_route(
    q: dict[str, np.ndarray], signals: dict[str, np.ndarray], rule: GateRule
) -> np.ndarray:
    """回傳每筆的路由標籤。純函式，只看 q 與 D。"""
    reliable_v = q["q_vision"] >= RELIABLE_MARGIN
    reliable_t = q["q_tof"] >= RELIABLE_MARGIN
    agree = signals["D"] <= rule.disagreement_threshold

    route = np.empty(len(signals["D"]), dtype=object)
    route[:] = "escalated"
    route[reliable_v & ~reliable_t] = "trust_vision"
    route[reliable_t & ~reliable_v] = "trust_tof"
    route[reliable_v & reliable_t & agree] = "fusion"
    return route
