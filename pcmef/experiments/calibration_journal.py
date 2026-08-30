# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.experiments.calibration_formal 在每一次評估與每一個階段
#         邊界呼叫；寫出 outputs/calibration/stage_<id>/evaluations.jsonl 與
#         outputs/calibration/checkpoint.json；重啟時由同一模組讀回。
# 檔案路徑: pcmef/experiments/calibration_journal.py
# 產生時間: 2026-08-30 19:35 +08:00
# 版本: v0.1.0
# 功能說明: 讓一次可能跑很久的校準在中斷之後能夠**逐位元**接續 —— 逐次評估
#           以 append-only 日誌落地，重啟時照原順序重播已完成的評估，
#           而不是把它們當成新結果重跑一遍。
# 模組定位: calibration 的耐久層。它不決定任何科學內容，只負責「已經算過的
#           不要再算一次，也不要假裝沒算過」。重播的正確性由參數向量的
#           逐位元比對保證，不是由時間戳或次數。
# 主要責任:
#   1. vector_fingerprint() 以 float.hex() 產生逐位元穩定的參數指紋
#   2. EvaluationJournal.append() 以 fsync 落地單筆評估
#   3. EvaluationJournal.replay_cursor() 依序交還已完成的評估
#   4. Checkpoint.save() / load() 原子性地保存階段層級狀態
#   5. Checkpoint.assert_compatible() 在身分漂移時拒絕接續
# 維護提醒:
#   - 不得以 repr(float) 或 round() 產生指紋。十進位字面值會把兩個不同的
#     浮點數印成同一個字串，於是重播會在錯的地方對上，而且不會有人發現。
#   - 不得在重播時「差不多就算對上」。指紋不符代表候選順序已經變了，
#     那是 CHECKPOINT_IDENTITY_MISMATCH，必須停，不得繼續。
#   - 不得在重播時重新計數預算或重新抽種子；兩者都必須從日誌還原，
#     否則接續後的執行與未中斷的執行是兩個不同的實驗。
#   - 不得直接覆寫 checkpoint 檔。先寫暫存再 os.replace，否則中途失敗會
#     留下一個半寫入的檔案，而它看起來完全正常。
#   - v0.1.0 新增：首版 formal calibration 檢查點與重播（CAL-PREREG-003）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_resume.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "ResumeError",
    "vector_fingerprint",
    "EvaluationRecord",
    "EvaluationJournal",
    "Checkpoint",
]

#: 日誌格式版本。改欄位語意時必須遞增，否則舊日誌會被以新語意重播。
JOURNAL_SCHEMA_VERSION = 1


class ResumeError(RuntimeError):
    """檢查點與現況不一致；不得接續。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


def vector_fingerprint(vector: Sequence[float]) -> str:
    """參數向量的逐位元指紋。

    用 `float.hex()` 而不是十進位字面值：`repr()` 對兩個相差一個 ULP 的數
    可能印出同一個字串，於是重播會在「看起來一樣、其實不同」的候選上對上。
    hex 是浮點數的無損表示，比對它等於比對位元。
    """
    return "|".join(float(v).hex() for v in vector)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 逐次評估日誌
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationRecord:
    """一次目標函數呼叫的完整記錄。

    欄位名對齊 CAL-PREREG-003 artifacts.per_evaluation.fields；接續所需的
    額外欄位（restart_index / parameter_fingerprint / seed_mode）另加，
    因為沒有它們就無法證明重播走的是同一條軌跡。
    """

    evaluation_index: int
    restart_index: int
    parameters: dict[str, float]
    parameter_fingerprint: str
    objective_total: float
    stage_objective: float
    objective_terms: dict[str, float]
    w1_raw_terms: dict[str, float]
    simulation_run_identity_hash: str
    seeds_used: dict[str, int]
    seed_mode: str
    status: str
    runtime_s: float
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "evaluation_index": self.evaluation_index,
            "restart_index": self.restart_index,
            "parameters": self.parameters,
            "parameter_fingerprint": self.parameter_fingerprint,
            "objective_total": self.objective_total,
            "stage_objective": self.stage_objective,
            "objective_terms": self.objective_terms,
            "w1_raw_terms": self.w1_raw_terms,
            "simulation_run_identity_hash": self.simulation_run_identity_hash,
            "seeds_used": self.seeds_used,
            "seed_mode": self.seed_mode,
            "status": self.status,
            "runtime_s": self.runtime_s,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> "EvaluationRecord":
        return cls(
            evaluation_index=int(row["evaluation_index"]),
            restart_index=int(row["restart_index"]),
            parameters=dict(row["parameters"]),
            parameter_fingerprint=str(row["parameter_fingerprint"]),
            objective_total=float(row["objective_total"]),
            stage_objective=float(row["stage_objective"]),
            objective_terms=dict(row["objective_terms"]),
            w1_raw_terms=dict(row["w1_raw_terms"]),
            simulation_run_identity_hash=str(row["simulation_run_identity_hash"]),
            seeds_used={str(k): int(v) for k, v in row["seeds_used"].items()},
            seed_mode=str(row["seed_mode"]),
            status=str(row["status"]),
            runtime_s=float(row["runtime_s"]),
            error=row.get("error"),
        )


@dataclass
class EvaluationJournal:
    """單一階段的 append-only 評估日誌。

    日誌**就是**檢查點的評估層：重啟時不需要還原 optimizer 的內部狀態，
    只要用同一顆種子重建它，再把已完成的評估照原順序交還，它就會走出
    完全相同的軌跡 —— 因為 DE 的下一步只取決於它拿到的目標值。
    """

    path: Path
    records: list[EvaluationRecord] = field(default_factory=list)
    _stream: Any = field(default=None, repr=False)
    _cursor: dict[int, int] = field(default_factory=dict, repr=False)
    #: 開檔當下從磁碟讀到的記錄，**開檔後就不再變動**。重播只認它。
    #: 若讓重播看 `records`，本次執行剛附加的記錄會立刻被當成「待重播的」，
    #: 於是第二次評估就會拿自己的第一次去比對而必然不符。
    _replayable: list[EvaluationRecord] = field(default_factory=list, repr=False)

    @classmethod
    def open(cls, path: str | Path, resume: bool) -> "EvaluationJournal":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        records: list[EvaluationRecord] = []
        if resume and target.exists():
            for line in target.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(EvaluationRecord.from_json(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    # 崩潰時最後一行可能只寫了一半。截斷到最後一筆完整記錄是
                    # 唯一安全的處置：半筆記錄無法證明那次評估真的完成了。
                    break
        elif target.exists():
            target.unlink()
        journal = cls(path=target, records=records, _replayable=list(records))
        journal._stream = target.open("a", encoding="utf-8")
        # 截斷任何殘缺的尾巴，讓檔案內容與已載入的記錄逐字一致。
        journal._rewrite_prefix()
        return journal

    def _rewrite_prefix(self) -> None:
        if self._stream is not None:
            self._stream.close()
        text = "".join(
            json.dumps(r.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
            for r in self.records
        )
        self.path.write_text(text, encoding="utf-8")
        self._stream = self.path.open("a", encoding="utf-8")

    def append(self, record: EvaluationRecord) -> None:
        self.records.append(record)
        self._stream.write(
            json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
        )
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    # -- 重播 --------------------------------------------------------------

    def completed_for(self, restart_index: int) -> list[EvaluationRecord]:
        """本次執行**開始之前**就已完成的評估。重播的唯一來源。"""
        return [r for r in self._replayable if r.restart_index == restart_index]

    def start_replay(self, restart_index: int) -> None:
        self._cursor[restart_index] = 0

    def replay(
        self, restart_index: int, fingerprint: str
    ) -> EvaluationRecord | None:
        """交還下一筆已完成的評估，或 None 代表日誌已用盡。

        指紋不符即 fail closed：那代表重建的 optimizer 提出了與原執行不同的
        候選，也就是接續後的執行與未中斷的執行不是同一個實驗。
        """
        done = self.completed_for(restart_index)
        index = self._cursor.get(restart_index, 0)
        if index >= len(done):
            return None
        record = done[index]
        if record.parameter_fingerprint != fingerprint:
            raise ResumeError(
                "CHECKPOINT_IDENTITY_MISMATCH",
                f"replaying restart {restart_index} evaluation {index}: the "
                f"rebuilt optimizer proposed fingerprint {fingerprint} but the "
                f"journal recorded {record.parameter_fingerprint}. The candidate "
                "ordering has changed, so this is not a continuation of the same run.",
            )
        self._cursor[restart_index] = index + 1
        return record

    def replay_exhausted(self, restart_index: int) -> bool:
        return self._cursor.get(restart_index, 0) >= len(
            self.completed_for(restart_index)
        )


# ---------------------------------------------------------------------------
# 階段層級檢查點
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """階段層級的執行狀態。評估層級由日誌負責。"""

    path: Path
    payload: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "Checkpoint | None":
        target = Path(path)
        if not target.exists():
            return None
        return cls(path=target, payload=json.loads(target.read_text(encoding="utf-8")))

    @classmethod
    def create(
        cls,
        path: str | Path,
        identity_block: dict[str, Any],
        code_version: str,
        runner_version: str,
        run_config: dict[str, Any],
    ) -> "Checkpoint":
        payload = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "runner_version": runner_version,
            "code_version": code_version,
            "identity": identity_block,
            "run_config": run_config,
            "completed_stages": {},
            "stage_order_completed": [],
            "frozen_parameters": {},
            "current": None,
            "runtime_s": 0.0,
            "access_ledger_entries": [],
        }
        checkpoint = cls(path=Path(path), payload=payload)
        checkpoint.save()
        return checkpoint

    def save(self) -> None:
        _atomic_write_json(self.path, self.payload)

    # -- 相容性 ------------------------------------------------------------

    def assert_compatible(
        self,
        identity_block: dict[str, Any],
        code_version: str,
        runner_version: str,
        run_config: dict[str, Any],
    ) -> None:
        """接續之前逐項比對身分。任何一項不同都拒絕接續。

        允許「換一台機器」但不允許「換一份協定、換一組資料、換一版程式碼、
        換一組模擬設定」。前者只影響時間，後者會讓前後兩半屬於不同的實驗。
        """
        if int(self.payload.get("schema_version", -1)) != JOURNAL_SCHEMA_VERSION:
            raise ResumeError(
                "CHECKPOINT_DRIFT",
                f"checkpoint schema {self.payload.get('schema_version')!r} != "
                f"{JOURNAL_SCHEMA_VERSION}",
            )
        for label, expected, actual in (
            ("identity", identity_block, self.payload.get("identity")),
            ("run_config", run_config, self.payload.get("run_config")),
            ("code_version", code_version, self.payload.get("code_version")),
            ("runner_version", runner_version, self.payload.get("runner_version")),
        ):
            if expected != actual:
                raise ResumeError(
                    "CHECKPOINT_DRIFT",
                    f"{label} drifted between the checkpointed run and this one.\n"
                    f"  checkpoint: {json.dumps(actual, ensure_ascii=False, sort_keys=True)}\n"
                    f"  now:        {json.dumps(expected, ensure_ascii=False, sort_keys=True)}",
                )

    # -- 存取 --------------------------------------------------------------

    @property
    def frozen_parameters(self) -> dict[str, float]:
        return dict(self.payload["frozen_parameters"])

    def completed(self, stage_id: str) -> dict[str, Any] | None:
        return self.payload["completed_stages"].get(stage_id)

    def complete_stage(self, stage_id: str, summary: dict[str, Any], selected: dict[str, float]) -> None:
        self.payload["completed_stages"][stage_id] = summary
        if stage_id not in self.payload["stage_order_completed"]:
            self.payload["stage_order_completed"].append(stage_id)
        self.payload["frozen_parameters"].update(
            {k: float(v) for k, v in selected.items()}
        )
        self.payload["current"] = None
        self.save()

    def set_current(self, stage_id: str, restart_index: int, detail: dict[str, Any]) -> None:
        self.payload["current"] = {
            "stage_id": stage_id,
            "restart_index": restart_index,
            **detail,
        }
        self.save()

    def add_runtime(self, seconds: float) -> None:
        self.payload["runtime_s"] = float(self.payload.get("runtime_s", 0.0)) + float(
            seconds
        )

    def record_ledger_entry(self, entry: dict[str, Any]) -> None:
        self.payload.setdefault("access_ledger_entries", []).append(entry)
        self.save()
