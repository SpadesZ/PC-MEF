# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.perception.dataset 產生的 dataset_manifest 與
#         preprocessing；由 cli 的 `perception train` 呼叫；寫出
#         outputs/perception/<run>/perception_report.json。
#         **不含 LLM、Reliability Agent、D/U/Q gate 或 formal E2。**
# 檔案路徑: pcmef/perception/baselines.py
# 產生時間: 2026-08-31 11:45 +08:00
# 版本: v0.1.0
# 功能說明: 兩個最小可用單模態模型（Vision CNN / ToF 1D-CNN）與一個
#           傳統融合 baseline（固定權重的機率加權平均），並產出可判讀的
#           accuracy / macro-F1 / confusion matrix / class probability。
# 模組定位: perception 的 baseline 層。目標是證明「可學、輸出正常、
#           融合流程可跑」，**不是**追求 SOTA，也不是 PC-MEF gate。
# 主要責任:
#   1. VisionCNN / ToFCNN 兩個最小架構
#   2. train_model() 以固定 seed 訓練並回傳逐 epoch 曲線
#   3. evaluate() 產出 accuracy / macro-F1 / confusion matrix / 機率
#   4. fit_fusion_weight() **只在 validation 上**挑融合權重
#   5. run_baselines() 串起全部並寫出報告
# 維護提醒:
#   - 不得用 test split 挑任何東西 —— 架構、epoch 數、融合權重一律只能
#     由 train/val 決定。test 只准算一次分數。
#   - 不得因為模型表現回頭改模擬器、解析度或校準值；那會讓 E1 的結論
#     變成被下游調出來的。
#   - 不得把「四類分得開」說成泛化。當前資料集每類只有一個物理場景，
#     dataset_manifest.leakage_audit 已具名記下這個上限。
#   - 不得在本模組引入 LLM、gate 或 E2 的任何部分；那是後續階段。
#   - v0.1.0 新增：首版 perception baseline 與傳統融合。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_baselines.py -v
#   - py -3.10 -m pcmef.cli perception train --run <run_name>
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.constants import CLASS_ORDER, N_CLASSES, N_TOF_FEATURES

__all__ = [
    "TRAIN_SEED",
    "VisionCNN",
    "ToFCNN",
    "train_model",
    "evaluate",
    "fit_fusion_weight",
    "run_baselines",
]

#: 訓練種子。固定值，且與模擬用過的任何種子無關（那些是場景實現的種子，
#: 這是權重初始化與批次順序的種子，兩者互不相干）。
TRAIN_SEED = 20260831

DEFAULT_EPOCHS = 40
DEFAULT_BATCH = 16
DEFAULT_LR = 1e-3


def _torch():
    import torch

    return torch


# ---------------------------------------------------------------------------
# 架構
# ---------------------------------------------------------------------------


def VisionCNN():  # noqa: N802 — 工廠函式，回傳 nn.Module
    """輕量 CNN：3x64x64 -> 四類 logit。

    刻意小（三個 conv block + GAP）：本階段要證明的是「學得起來」，
    而在每類只有一個物理場景的資料上堆容量只會更快記住那四個場景。
    GAP 而不是 flatten+FC：後者的參數量會被 64x64 撐爆，且對位置過度敏感。
    """
    torch = _torch()
    nn = torch.nn

    def block(cin: int, cout: int) -> Any:
        return nn.Sequential(
            nn.Conv2d(cin, cout, 3, padding=1, bias=False),
            nn.BatchNorm2d(cout),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
        )

    return nn.Sequential(
        block(3, 16),      # 64 -> 32
        block(16, 32),     # 32 -> 16
        block(32, 64),     # 16 -> 8
        nn.AdaptiveAvgPool2d(1),
        nn.Flatten(),
        nn.Dropout(0.3),
        nn.Linear(64, N_CLASSES),
    )


def ToFCNN():  # noqa: N802
    """1D CNN：4x500 -> 四類 logit。

    channel = 四個特徵、length = 500 個 measurement point。這個擺法是
    語意的：卷積沿**時間**走，四個特徵在每一個時間點被一起看 ——
    反過來擺會讓卷積沿著特徵軸滑動，而那個軸沒有鄰接關係（NOTE-001 的
    canonical order 是約定，不是物理上的相鄰）。
    """
    torch = _torch()
    nn = torch.nn

    def block(cin: int, cout: int) -> Any:
        return nn.Sequential(
            nn.Conv1d(cin, cout, 7, padding=3, bias=False),
            nn.BatchNorm1d(cout),
            nn.ReLU(inplace=True),
            nn.MaxPool1d(2),
        )

    return nn.Sequential(
        block(N_TOF_FEATURES, 16),   # 500 -> 250
        block(16, 32),               # 250 -> 125
        block(32, 64),               # 125 -> 62
        nn.AdaptiveAvgPool1d(1),
        nn.Flatten(),
        nn.Dropout(0.3),
        nn.Linear(64, N_CLASSES),
    )


# ---------------------------------------------------------------------------
# 訓練
# ---------------------------------------------------------------------------


def train_model(
    factory: Callable[[], Any],
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    epochs: int = DEFAULT_EPOCHS,
    batch_size: int = DEFAULT_BATCH,
    lr: float = DEFAULT_LR,
    seed: int = TRAIN_SEED,
    progress: Callable[[str], None] | None = None,
) -> tuple[Any, list[dict[str, float]]]:
    """以固定 seed 訓練，回傳 (最佳 val 的模型, 逐 epoch 曲線)。

    選 checkpoint 的判準是 **validation accuracy**，不是 test —— test 在
    整個訓練過程中不得被看到一次。
    """
    torch = _torch()
    say = progress or (lambda _m: None)

    torch.manual_seed(seed)
    np.random.seed(seed)
    model = factory()
    optimiser = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = torch.nn.CrossEntropyLoss()

    xt = torch.from_numpy(x_train)
    yt = torch.from_numpy(y_train)
    xv = torch.from_numpy(x_val)
    yv = torch.from_numpy(y_val)

    generator = torch.Generator().manual_seed(seed)
    history: list[dict[str, float]] = []
    best_state, best_val = None, -1.0

    for epoch in range(1, epochs + 1):
        model.train()
        order = torch.randperm(len(xt), generator=generator)
        total_loss = 0.0
        for start in range(0, len(order), batch_size):
            index = order[start : start + batch_size]
            optimiser.zero_grad()
            loss = loss_fn(model(xt[index]), yt[index])
            loss.backward()
            optimiser.step()
            total_loss += float(loss.detach()) * len(index)

        model.eval()
        with torch.no_grad():
            train_acc = float((model(xt).argmax(1) == yt).float().mean())
            val_logits = model(xv)
            val_loss = float(loss_fn(val_logits, yv))
            val_acc = float((val_logits.argmax(1) == yv).float().mean())

        history.append(
            {
                "epoch": epoch,
                "train_loss": total_loss / len(xt),
                "train_accuracy": train_acc,
                "val_loss": val_loss,
                "val_accuracy": val_acc,
            }
        )
        if val_acc > best_val:
            best_val = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        if epoch % 10 == 0 or epoch == 1:
            say(
                f"    epoch {epoch:3d}  train_acc {train_acc:.3f}  "
                f"val_acc {val_acc:.3f}  val_loss {val_loss:.4f}"
            )

    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    return model, history


def predict_proba(model: Any, x: np.ndarray) -> np.ndarray:
    torch = _torch()
    with torch.no_grad():
        return torch.softmax(model(torch.from_numpy(x)), dim=1).numpy()


# ---------------------------------------------------------------------------
# 度量
# ---------------------------------------------------------------------------


def confusion_matrix(y_true: np.ndarray, y_pred: np.ndarray) -> np.ndarray:
    matrix = np.zeros((N_CLASSES, N_CLASSES), dtype=int)
    for true, pred in zip(y_true, y_pred):
        matrix[int(true), int(pred)] += 1
    return matrix


def macro_f1(matrix: np.ndarray) -> tuple[float, list[float]]:
    """逐類 F1 再取平均。分母為 0 時該類 F1 記 0.0，不記 nan ——
    一個從未被預測也從未出現的類別，其 F1 是 0 而不是「未定義」。
    """
    per_class: list[float] = []
    for index in range(N_CLASSES):
        tp = matrix[index, index]
        fp = matrix[:, index].sum() - tp
        fn = matrix[index, :].sum() - tp
        denominator = 2 * tp + fp + fn
        per_class.append(float(2 * tp / denominator) if denominator else 0.0)
    return float(np.mean(per_class)), per_class


def evaluate(name: str, proba: np.ndarray, labels: np.ndarray) -> dict[str, Any]:
    predictions = proba.argmax(1)
    matrix = confusion_matrix(labels, predictions)
    macro, per_class_f1 = macro_f1(matrix)
    correct = predictions == labels
    return {
        "name": name,
        "n": int(len(labels)),
        "accuracy": float(correct.mean()),
        "macro_f1": macro,
        "per_class_f1": {c: per_class_f1[i] for i, c in enumerate(CLASS_ORDER)},
        "confusion_matrix": matrix.tolist(),
        "confusion_matrix_rows": "true class (CLASS_ORDER)",
        "confusion_matrix_cols": "predicted class (CLASS_ORDER)",
        "mean_probability_of_true_class": float(
            proba[np.arange(len(labels)), labels].mean()
        ),
        "mean_max_probability": float(proba.max(1).mean()),
        "mean_probability_by_true_class": {
            c: [
                float(v)
                for v in proba[labels == i].mean(0)
            ]
            if int((labels == i).sum())
            else None
            for i, c in enumerate(CLASS_ORDER)
        },
    }


# ---------------------------------------------------------------------------
# 傳統融合
# ---------------------------------------------------------------------------


def fit_fusion_weight(
    vision_val: np.ndarray,
    tof_val: np.ndarray,
    y_val: np.ndarray,
    grid: np.ndarray | None = None,
) -> dict[str, Any]:
    """在 **validation** 上挑固定權重 w：p = w*vision + (1-w)*tof。

    刻意選一個只有一個自由度、且結果可以直接讀出來的融合方式：
    w 靠近 1 代表 vision 主導，靠近 0 代表 ToF 主導。平手時取**較大的 w**
    沒有道理，因此平手取**離 0.5 最近**的那個 —— 在兩個模態分不出高下時
    等權是唯一不需要額外辯護的選擇。
    """
    grid = np.linspace(0.0, 1.0, 21) if grid is None else grid
    rows = []
    for w in grid:
        blended = w * vision_val + (1.0 - w) * tof_val
        accuracy = float((blended.argmax(1) == y_val).mean())
        rows.append({"w": float(w), "val_accuracy": accuracy})
    best = max(r["val_accuracy"] for r in rows)
    tied = [r for r in rows if r["val_accuracy"] == best]
    chosen = min(tied, key=lambda r: abs(r["w"] - 0.5))
    return {
        "method": "fixed weighted average of class probabilities",
        "form": "p_fused = w * p_vision + (1 - w) * p_tof",
        "selected_w": chosen["w"],
        "selected_on": "validation split only",
        "val_accuracy_at_selected_w": chosen["val_accuracy"],
        "tie_rule": "closest to 0.5 among equally best weights",
        "n_tied": len(tied),
        "grid": rows,
    }


def fuse(vision: np.ndarray, tof: np.ndarray, w: float) -> np.ndarray:
    return w * vision + (1.0 - w) * tof


# ---------------------------------------------------------------------------
# 串起來
# ---------------------------------------------------------------------------


def _overfitting(history: list[dict[str, float]], report: dict[str, Any]) -> dict[str, Any]:
    """過擬合的判讀。用差距而不是單一數字：訓練分數本身說明不了任何事。"""
    final = history[-1]
    gap_val = final["train_accuracy"] - report["val"]["accuracy"]
    gap_test = final["train_accuracy"] - report["test"]["accuracy"]
    best_val = max(h["val_accuracy"] for h in history)
    return {
        "final_train_accuracy": final["train_accuracy"],
        "val_accuracy": report["val"]["accuracy"],
        "test_accuracy": report["test"]["accuracy"],
        "train_minus_val": gap_val,
        "train_minus_test": gap_test,
        "best_val_accuracy": best_val,
        "val_loss_first": history[0]["val_loss"],
        "val_loss_final": final["val_loss"],
        "val_loss_rose_after_best": bool(
            final["val_loss"] > min(h["val_loss"] for h in history) * 1.5
        ),
        "verdict": (
            "clear overfitting"
            if gap_val > 0.15
            else "mild train-val gap"
            if gap_val > 0.05
            else "no material train-val gap"
        ),
    }


def run_baselines(
    run_dir: str | Path,
    epochs: int = DEFAULT_EPOCHS,
    batch_size: int = DEFAULT_BATCH,
    lr: float = DEFAULT_LR,
    seed: int = TRAIN_SEED,
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """訓練兩個單模態 baseline、挑融合權重、產出報告。"""
    from pcmef.perception.dataset import fit_preprocessing, load_split

    say = progress or (lambda _m: None)
    directory = Path(run_dir)
    manifest = json.loads(
        (directory / "dataset_manifest.json").read_text(encoding="utf-8")
    )

    say("fitting preprocessing on the train split only")
    preprocessing = fit_preprocessing(manifest)
    (directory / "preprocessing.json").write_text(
        json.dumps(preprocessing, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    say("loading splits")
    splits = {name: load_split(manifest, preprocessing, name) for name in ("train", "val", "test")}

    results: dict[str, Any] = {}
    probabilities: dict[str, dict[str, np.ndarray]] = {}

    for name, factory, key in (
        ("vision", VisionCNN, "rgb"),
        ("tof", ToFCNN, "tof"),
    ):
        say(f"  training {name} baseline")
        model, history = train_model(
            factory,
            getattr(splits["train"], key), splits["train"].labels,
            getattr(splits["val"], key), splits["val"].labels,
            epochs=epochs, batch_size=batch_size, lr=lr, seed=seed, progress=say,
        )
        probabilities[name] = {
            split: predict_proba(model, getattr(splits[split], key))
            for split in ("train", "val", "test")
        }
        report = {
            split: evaluate(f"{name}/{split}", probabilities[name][split], splits[split].labels)
            for split in ("train", "val", "test")
        }
        parameters = sum(p.numel() for p in model.parameters())
        results[name] = {
            "architecture": str(model),
            "parameter_count": int(parameters),
            "input_shape": list(getattr(splits["train"], key).shape[1:]),
            "epochs": epochs,
            "batch_size": batch_size,
            "learning_rate": lr,
            "seed": seed,
            "history": history,
            **report,
            "overfitting": _overfitting(history, report),
        }
        say(
            f"    {name}: val {report['val']['accuracy']:.3f} / "
            f"test {report['test']['accuracy']:.3f} "
            f"(macro-F1 {report['test']['macro_f1']:.3f})"
        )

    say("  fitting the fusion weight on validation")
    fusion = fit_fusion_weight(
        probabilities["vision"]["val"], probabilities["tof"]["val"], splits["val"].labels
    )
    w = fusion["selected_w"]
    fused = {
        split: fuse(probabilities["vision"][split], probabilities["tof"][split], w)
        for split in ("train", "val", "test")
    }
    fusion_report = {
        split: evaluate(f"fusion/{split}", fused[split], splits[split].labels)
        for split in ("train", "val", "test")
    }
    results["fusion"] = {**fusion, **fusion_report}
    say(
        f"    fusion (w={w:.2f}): val {fusion_report['val']['accuracy']:.3f} / "
        f"test {fusion_report['test']['accuracy']:.3f}"
    )

    document = {
        "report_id": "perception_baselines",
        "scientific_result": False,
        "purpose": (
            "Minimal single-modality baselines and a traditional fusion baseline. "
            "Shows that the paired synthetic data is learnable and that the fusion "
            "path runs end to end. Not a PC-MEF gate, not formal E2, and not tuned "
            "for accuracy."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "run_dir": directory.as_posix(),
        "class_order": list(CLASS_ORDER),
        "dataset": {
            "counts": manifest["counts"],
            "totals": manifest["totals"],
            "per_class": manifest["per_class"],
            "split_ratio": manifest["split_ratio"],
            "leakage_audit": manifest["leakage_audit"],
            "simulator_identity_hash": manifest["simulator"]["identity_hash"],
            "calibrated_simulation_lock_hash": manifest["simulator"][
                "calibrated_simulation_lock_hash"
            ],
        },
        "preprocessing": preprocessing,
        "models": {k: v for k, v in results.items() if k != "fusion"},
        "fusion": results["fusion"],
        "claim_boundary": (
            "Accuracy here measures separability of four fixed physical scenes under "
            "Monte-Carlo render noise. The dataset contains exactly one physical "
            "scene per class, so it cannot support any claim about generalisation to "
            "unseen bottle configurations, and it says nothing about real-sensor "
            "performance."
        ),
    }
    (directory / "perception_report.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
