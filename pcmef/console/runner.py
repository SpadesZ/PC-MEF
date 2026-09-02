# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.console.routes 呼叫；以子行程執行 pcmef.cli 的指令，
#         把 stdout/stderr 逐行寫入 outputs/console/runs/<run_id>/log.txt，
#         狀態寫入同目錄的 run.json；產物落在該 run 的 artifacts 目錄。
# 檔案路徑: pcmef/console/runner.py
# 產生時間: 2026-08-27 16:10 +08:00
# 版本: v0.2.0
# 功能說明: 讓網頁按一下就能跑模擬，而且跑的過程像在看終端機一樣逐行出現。
#           每次執行都存成一筆有編號的紀錄，含當時用的完整參數與完整輸出。
# 模組定位: Console 的執行層。formal run 自 AMD-008 起可由此觸發，但
#           **只能觸發，不能設定** —— 白名單只收 mode 與 confirm，
#           准駁全在 `pcmef formal run-e2` 子行程的 pre-flight 內。
# 主要責任:
#   1. RunSpec 定義一次執行的種類、參數與產生的指令
#   2. PRESETS 提供三個懶人包，進階參數才需要展開
#   3. ConsoleRunner.start() 以子行程啟動並立刻回傳 run_id
#   4. _pump() 在背景執行緒把輸出逐行落盤，讓網頁可以邊跑邊看
#   5. ConsoleRunner.tail() 依位元組位移取回新增的行，供 SSE 串流
#   6. ConsoleRunner.list_runs() / get() 提供歷史紀錄
#   7. _assert_not_formal() 擋下任何試圖從 UI 啟動 formal 的參數
# 維護提醒:
#   - 不得擴大 FORMAL_PARAM_WHITELIST。UI 一旦能傳 severity、門檻或
#     freeze_dir，它就成了繞過 freeze 的第二條設定通道，而那正是
#     §52 結語與 §208 要擋的事。能按按鈕不等於能決定跑什麼（AMD-008）。
#   - 不得讓 formal 模式在沒有確認片語的情況下啟動。那是一次性的。
#   - 不得把子行程改成同一行程內呼叫。NOTE-012：drjit/mitsuba 在
#     Windows 連續算多場景後會於 DLL detach 崩潰，子行程隔離是既有結論；
#     而且網頁行程被算圖卡住的話，整個 console 會失去回應。
#   - 不得把 log 只留在記憶體。落盤才能在重新整理頁面、甚至重開瀏覽器後
#     仍看得到，也才能在容器重啟後保留證據。
#   - 不得移除 run.json 裡的完整參數與指令；沒有它就無法回答
#     「這張圖是用什麼參數跑出來的」。
#   - v0.1.0 新增：首版執行器，決策見 NOTE-025。
#   - v0.2.0 新增：formal_e2 run kind 與參數白名單（AMD-008、NOTE-059）。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_runner.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from pcmef.core.constants import CLASS_ORDER

__all__ = [
    "RunnerError",
    "FormalRunRefused",
    "PRESETS",
    "RunSpec",
    "RunRecord",
    "ConsoleRunner",
    "DEFAULT_RUN_ROOT",
]

DEFAULT_RUN_ROOT = Path("outputs/console/runs")

#: 懶人包。三個 preset 涵蓋「看一眼」「正常跑」「出圖用」三種需求，
#: 進階參數在 UI 上預設摺疊 —— 模擬軟體常見的做法。
PRESETS: dict[str, dict[str, Any]] = {
    "preview": {
        "label": "快速預覽",
        "hint": "約十幾秒。看得出形狀，不適合放進論文。",
        "resolution": 32, "spp": 8, "temporal_bins": 64,
    },
    "standard": {
        "label": "標準",
        "hint": "與 configs/simulation/smoke.yaml 相同，可重現既有結果。",
        "resolution": 64, "spp": 16, "temporal_bins": 128,
    },
    "quality": {
        "label": "高品質",
        "hint": "數分鐘。雜訊低，適合出圖。",
        "resolution": 128, "spp": 64, "temporal_bins": 256,
    },
}

#: 每一類的預設 seed 與介質參數，取自 configs/simulation/smoke.yaml。
_CLASS_DEFAULTS: dict[str, dict[str, Any]] = {
    "Empty": {"seed": 1001, "medium": {}},
    "Water-filled": {
        "seed": 1002,
        "medium": {"turbidity": {"value": 0.05, "placeholder": True}},
    },
    "Bubbly": {
        "seed": 1042,
        "medium": {"bubble_density": {"value": 0.30, "placeholder": True}},
    },
    "Misty": {
        "seed": 1004,
        "medium": {"mist_density": {"value": 0.15, "placeholder": True}},
    },
}


class RunnerError(RuntimeError):
    """參數非法或執行紀錄不存在。"""


class FormalRunRefused(RunnerError):
    """有人試圖從 UI 啟動 formal run。

    與一般錯誤分開：這不是輸入失誤，是踩到 §208 與 §52 結語劃下的界線 ——
    formal identity 只能由 CLI 產生。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 一次執行的規格
# ---------------------------------------------------------------------------


#: console 允許啟動的指令種類。
#:
#: llm_snapshot 是唯一會寫進 freeze/ 的一種，加入時的界線判斷見
#: _assert_not_formal() 的說明：console 仍然不跑 formal experiment，
#: 但可以觸發「把目前的 draft 設定凍結成 lock」這個動作。
RUN_KINDS: tuple[str, ...] = (
    "sim_smoke", "surrogate_smoke", "audit_gates", "llm_snapshot", "formal_e2",
)

#: `formal_e2` **唯一**接受的參數。任何其他鍵一律拒絕。
#:
#: 這個集合刻意小到不含任何科學設定：severity、門檻、freeze_dir、base、
#: ds_dir 全部不在其中。lineage 由 ACTIVE_LINEAGE 解析，資料位置由伺服器端
#: 設定決定 —— UI 能決定的只有「跑不跑」與「跑哪一種模式」。
FORMAL_PARAM_WHITELIST: frozenset[str] = frozenset({"mode", "confirm"})

#: formal 模式必須附上的確認字串。dry run 不需要。
FORMAL_CONFIRM_PHRASE = "RUN FINAL E2"

#: 預演預設讀的資料集。已開封的 gate-validation，不是 final partition。
DEFAULT_DRY_RUN_BASE = "outputs/perception/gate_validation"


@dataclass(frozen=True)
class RunSpec:
    """一次執行要做什麼、用什麼參數。"""

    kind: str
    params: Mapping[str, Any]
    label: str = ""

    KINDS: tuple[str, ...] = field(default=RUN_KINDS, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in RUN_KINDS:
            raise RunnerError(
                f"unknown run kind {self.kind!r}; the console runs "
                + " / ".join(RUN_KINDS)
            )


@dataclass
class RunRecord:
    """一次執行的完整紀錄。落盤成 run.json。"""

    run_id: str
    kind: str
    label: str
    params: dict[str, Any]
    command: list[str]
    status: str = "running"
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    exit_code: int | None = None
    note: str = ""

    @property
    def finished(self) -> bool:
        return self.status in ("succeeded", "failed")

    def matches(self, query: str) -> bool:
        """搜尋比對。涵蓋編號、種類、標籤、狀態與參數值。

        參數值也納入，是因為實務上要找的往往是「那次 spp 開到 64 的」，
        而那個數字只存在於 params 裡，不在任何一個欄位標題上。
        """
        needle = query.strip().lower()
        if not needle:
            return True
        haystack = " ".join(
            [self.run_id, self.kind, self.label, self.status]
            + [f"{k}={v}" for k, v in self.params.items()]
        ).lower()
        return needle in haystack

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "kind": self.kind, "label": self.label,
            "params": self.params, "command": self.command, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "exit_code": self.exit_code, "note": self.note,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunRecord:
        return cls(
            run_id=data["run_id"], kind=data["kind"], label=data.get("label", ""),
            params=dict(data.get("params", {})), command=list(data.get("command", [])),
            status=data.get("status", "running"), started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at"), exit_code=data.get("exit_code"),
            note=data.get("note", ""),
        )


# ---------------------------------------------------------------------------
# 執行器
# ---------------------------------------------------------------------------


class ConsoleRunner:
    """以子行程執行探索性指令，並把輸出逐行落盤。"""

    def __init__(
        self,
        run_root: str | Path = DEFAULT_RUN_ROOT,
        python_executable: str | None = None,
        dry_run_base: str | Path = DEFAULT_DRY_RUN_BASE,
    ) -> None:
        self.run_root = Path(run_root)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._python = python_executable or sys.executable
        self._threads: dict[str, threading.Thread] = {}
        # 預演用的資料位置由**伺服器端**決定，不從表單來。讓 UI 指定 base
        # 等於讓它挑要用哪一批資料，而 families 36-43 生成即開封。
        self.dry_run_base = Path(dry_run_base)

    # -- 路徑 -------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.run_root / run_id

    def log_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "log.txt"

    def record_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    # -- 參數 -------------------------------------------------------------

    @staticmethod
    def _assert_not_formal(params: Mapping[str, Any], kind: str = "") -> None:
        """擋下任何試圖用 UI 設定 formal experiment 的參數。

        §208 原本規定：所有 formal run 一律無 UI、走 CLI。這條線被**兩次**
        明確劃細，而不是被放寬 —— 兩次用的是同一個判準：**誰做判斷**。

        第一次（2026-08-31）：區分「跑 formal experiment」與「凍結 draft
        設定成 lock」。後者允許由 console 觸發（`llm_snapshot`），因為
        console 只是啟動真正的 `pcmef llm snapshot --freeze` 子行程，
        前提未齊時是 CLI 自己拒絕並回 exit 2。UI 沒有任何一行程式碼能決定
        「這份 lock 該不該寫」。

        第二次（2026-09-02，AMD-008）：`formal_e2` 加入允許清單。同樣的
        判準 —— console 啟動的是 `pcmef formal run-e2` 子行程，
        pre-flight、lineage 解析、sealed-partition 檢查全部在 CLI 內，
        不通過就 exit 2。UI 能決定的只有「跑不跑」與「哪一種模式」。

        關鍵在於 UI **不能設定任何科學參數**：severity、門檻、freeze_dir、
        base、ds_dir 都不在 `FORMAL_PARAM_WHITELIST` 內。少了這一條，
        按鈕觸發就會變成「UI 成為繞過 freeze 的第二條設定通道」，
        那正是 §52 結語要擋的事。

        其餘 run kind 維持原本的全面拒絕：它們沒有理由帶 formal 參數。
        """
        if kind == "formal_e2":
            unknown = sorted(set(params) - FORMAL_PARAM_WHITELIST)
            if unknown:
                raise FormalRunRefused(
                    f"the console refuses to pass {unknown} to a formal run. A "
                    f"formal run accepts only {sorted(FORMAL_PARAM_WHITELIST)}: the "
                    "lineage comes from ACTIVE_LINEAGE and every scientific setting "
                    "comes from the frozen locks. Letting the UI supply them would "
                    "make it a second configuration channel that bypasses freeze."
                )
            mode = str(params.get("mode", "dry-run"))
            if mode not in ("dry-run", "formal"):
                raise FormalRunRefused(f"unknown formal run mode {mode!r}")
            if mode == "formal" and str(params.get("confirm", "")) != FORMAL_CONFIRM_PHRASE:
                raise FormalRunRefused(
                    "a formal run is one-shot and unrepeatable; it requires the "
                    f"confirmation phrase {FORMAL_CONFIRM_PHRASE!r}"
                )
            return

        for key, value in params.items():
            if "formal" in str(key).lower() and value:
                raise FormalRunRefused(
                    f"the console refuses {key}={value!r} for run kind {kind!r}. "
                    "Only the formal_e2 kind may start a formal run, and it takes "
                    f"no scientific parameters (SRC-SAI §208, AMD-008)."
                )

    def build_config(self, run_id: str, params: Mapping[str, Any]) -> Path:
        """把 UI 參數寫成一份完整的 scenario 設定並存進 run 目錄。

        存下實際用的設定而非只存幾個數字：日後看到一張圖時，
        「它是用什麼跑出來的」必須能完整回答，而不是靠拼湊。
        """
        preset = PRESETS[params.get("preset", "standard")]

        def _override(key: str) -> int:
            """取進階參數，沒給才用 preset 的值。

            不用 `params.get(key) or preset[key]`：0 是 falsy，會被 `or`
            悄悄換成 preset 值，於是使用者輸入的 0 變成 16 而畫面上看不出來。
            這與 NOTE-005 讓 Required 成為物件而非 None 是同一個理由。
            """
            value = params.get(key)
            return int(preset[key] if value in (None, "") else value)

        resolution = _override("resolution")
        spp = _override("spp")
        bins = _override("temporal_bins")
        # 區分「沒給 classes」與「給了空清單」：
        # 用 `or CLASS_ORDER` 會讓使用者把四個核取方塊全部取消之後，
        # 靜默變成「跑全部四類」—— 與他按下去的意思正好相反。
        raw_classes = params.get("classes")
        classes = list(CLASS_ORDER) if raw_classes is None else list(raw_classes)
        seed_offset = int(params.get("seed_offset") or 0)

        unknown = [c for c in classes if c not in _CLASS_DEFAULTS]
        if unknown:
            raise RunnerError(f"unknown class label(s) {unknown}")
        if not classes:
            raise RunnerError("select at least one class to simulate")
        for name, value in (("resolution", resolution), ("spp", spp),
                            ("temporal_bins", bins)):
            if value <= 0:
                raise RunnerError(f"{name} must be positive, got {value}")

        scenarios = []
        for label in classes:
            defaults = _CLASS_DEFAULTS[label]
            scenarios.append({
                "class_label": label,
                "seed": int(defaults["seed"]) + seed_offset,
                "medium": defaults["medium"],
            })

        config = {
            "simulation": {
                "variant": params.get("variant") or "llvm_ad_rgb",
                "spp": spp,
                "resolution": [resolution, resolution],
                "temporal_bins": bins,
                "geometry": {
                    "sensor_to_bottle_mm": float(
                        params.get("sensor_to_bottle_mm") or 50.0
                    ),
                    "bottle_diameter_mm": float(
                        params.get("bottle_diameter_mm") or 57.0
                    ),
                    "wall_thickness_mm": float(params.get("wall_thickness_mm") or 2.0),
                    "lateral_offset_mm": float(params.get("lateral_offset_mm") or 0.0),
                },
                "lighting": {
                    "preset": "nominal",
                    "irradiance": float(params.get("irradiance") or 1.0),
                },
                "scenarios": scenarios,
            }
        }
        folder = self.run_dir(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "scenario.yaml"
        path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def _command(self, run_id: str, spec: RunSpec) -> list[str]:
        out_dir = self.run_dir(run_id) / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        base = [self._python, "-u", "-m", "pcmef.cli"]

        if spec.kind == "sim_smoke":
            config = self.build_config(run_id, spec.params)
            return base + [
                "sim", "smoke", "--config", str(config), "--out", str(out_dir),
                "--temporal-bins",
                str(spec.params.get("temporal_bins")
                    or PRESETS[spec.params.get("preset", "standard")]["temporal_bins"]),
            ]
        if spec.kind == "surrogate_smoke":
            source = spec.params.get("simulation_out")
            if not source:
                raise RunnerError(
                    "surrogate_smoke needs simulation_out pointing at a finished "
                    "simulation run"
                )
            return base + [
                "surrogate", "smoke", "--simulation-out", str(source),
                "--out", str(out_dir),
            ]
        if spec.kind == "llm_snapshot":
            # --out 是**目錄**（snapshot.write 自己決定檔名為
            # runtime_snapshot_<hash>.json），不是檔案路徑。給它一個 .json
            # 結尾的路徑會建出一個叫那個名字的資料夾。
            #
            # 候選快照永遠會產生，隨這次 run 存著；--freeze 才會寫
            # freeze/llm_runtime.lock.json，而且前提未齊時是 CLI 自己
            # 拒絕（exit 2），不是這裡判斷的。
            command = base + ["llm", "snapshot", "--out", str(out_dir)]
            if spec.params.get("freeze"):
                command.append("--freeze")
            return command
        if spec.kind == "formal_e2":
            # 只有 --mode 來自使用者。--base 由伺服器端設定決定，
            # lineage 由 CLI 自己解析 ACTIVE_LINEAGE —— 兩者都不經表單。
            mode = str(spec.params.get("mode", "dry-run"))
            command = base + [
                "formal", "run-e2", "--mode", mode, "--out", str(out_dir),
            ]
            if mode == "dry-run":
                command += ["--base", str(self.dry_run_base)]
            return command
        return base + ["audit", "e1-gates", "--out", str(out_dir)]

    # -- 執行 -------------------------------------------------------------

    def start(self, spec: RunSpec) -> RunRecord:
        """啟動一次執行並立刻回傳。輸出由背景執行緒逐行落盤。"""
        self._assert_not_formal(spec.params, spec.kind)
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.run_dir(run_id).mkdir(parents=True, exist_ok=True)

        command = self._command(run_id, spec)
        record = RunRecord(
            run_id=run_id, kind=spec.kind,
            label=spec.label or PRESETS.get(
                spec.params.get("preset", ""), {}
            ).get("label", spec.kind),
            params=dict(spec.params), command=command,
        )
        self._save(record)
        self.log_path(run_id).write_text("", encoding="utf-8")

        thread = threading.Thread(
            target=self._pump, args=(record,), name=f"console-run-{run_id}", daemon=True
        )
        self._threads[run_id] = thread
        thread.start()
        return record

    def _pump(self, record: RunRecord) -> None:
        """在背景把子行程輸出逐行寫進 log 檔。

        逐行 flush 而非等結束才寫：使用者要看到的是「正在跑什麼」，
        而一個算圖跑五分鐘卻沒有任何輸出的畫面，與當掉沒有分別。
        """
        log_path = self.log_path(record.run_id)
        try:
            process = subprocess.Popen(
                record.command,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
        except Exception as error:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"failed to start: {type(error).__name__}: {error}\n")
            record.status, record.exit_code = "failed", -1
            record.finished_at = _now()
            self._save(record)
            return

        with log_path.open("a", encoding="utf-8") as handle:
            assert process.stdout is not None
            for line in process.stdout:
                handle.write(line)
                handle.flush()
        exit_code = process.wait()

        record.exit_code = exit_code
        record.status = "succeeded" if exit_code == 0 else "failed"
        record.finished_at = _now()
        if exit_code != 0 and record.kind == "sim_smoke":
            record.note = (
                "非零 exit code 也可能是 NOTE-012 的 drjit DLL-detach 崩潰；"
                "請以 manifest 的 counts 判定實際成敗。"
            )
        self._save(record)

    # -- 讀取 -------------------------------------------------------------

    def tail(self, run_id: str, offset: int = 0) -> tuple[str, int]:
        """回傳自 offset 之後的新內容與新的位移。

        以位元組位移而非行號：位移讓串流可以在任何時刻接上，
        而且重新整理頁面後不必重讀整份 log。
        """
        path = self.log_path(run_id)
        if not path.exists():
            return "", offset
        data = path.read_bytes()
        chunk = data[offset:]
        return chunk.decode("utf-8", errors="replace"), len(data)

    def _save(self, record: RunRecord) -> None:
        self.record_path(record.run_id).write_text(
            json.dumps(record.to_json(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get(self, run_id: str) -> RunRecord:
        path = self.record_path(run_id)
        if not path.exists():
            raise RunnerError(f"run {run_id!r} not found")
        return RunRecord.from_json(json.loads(path.read_text(encoding="utf-8")))

    def list_runs(self, limit: int = 30, query: str = "") -> list[RunRecord]:
        """最近的執行紀錄，新到舊。query 非空時只留匹配的。

        篩選在讀完之後才做，而不是在 `len(records) >= limit` 那個迴圈裡：
        先截斷再篩選的話，limit 之外的舊紀錄永遠搜不到 —— 而「東西太多所以
        要搜尋」的情境，要找的通常正好就是那些舊的。
        """
        records = []
        for folder in sorted(self.run_root.iterdir(), reverse=True):
            path = folder / "run.json"
            if path.exists():
                records.append(
                    RunRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
                )
        if query:
            records = [r for r in records if r.matches(query)]
        return records[:limit]

    def delete(self, run_id: str) -> None:
        """刪掉一次執行的整個目錄（log、設定與產物）。

        執行中的不給刪：子行程還握著 log 的檔案句柄，而且刪掉之後 SSE
        會對著一個不存在的紀錄一直重試。要刪就先讓它跑完。

        這些是**探索用**紀錄，outputs/ 本來就不進版控，因此刪除沒有科學風險；
        真正有科學意義的東西在 freeze/ 與 artifacts/，不在這裡。
        """
        record = self.get(run_id)
        if not record.finished:
            raise RunnerError(
                f"run {run_id!r} is still running; wait for it to finish before "
                "deleting it"
            )
        shutil.rmtree(self.run_dir(run_id))
        self._threads.pop(run_id, None)

    def is_running(self, run_id: str) -> bool:
        return not self.get(run_id).finished

    def wait(self, run_id: str, timeout: float = 600.0) -> RunRecord:
        """供測試與 CLI 使用；網頁端不呼叫它（那會卡住請求）。"""
        thread = self._threads.get(run_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(run_id)
