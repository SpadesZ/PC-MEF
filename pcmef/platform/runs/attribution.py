# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes 啟動 run 時寫入一次，之後由 Run / Results
#         頁讀取。**write-once：寫下之後不得再改。**
# 檔案路徑: pcmef/platform/runs/attribution.py
# 產生時間: 2026-09-08 09:20 +08:00
# 版本: v0.2.0
# 功能說明: 一次 run 的歸屬快照 —— 它屬於哪個 Project / Profile，
#           當時的 pipeline 是什麼形狀。
# 模組定位: post-platform audit P0-1 的修正。歷史 run **不得**用
#           「現在 UI 選了什麼」來解讀：同一筆 run 在 A 專案下顯示
#           七個 stage、切到 B 之後顯示三個，代表它的歷史被當下的
#           選擇改寫了。歸屬必須在啟動當下就固定下來。
# 主要責任:
#   1. RunAttribution 承載 project / profile / pipeline 的啟動當下身分
#   2. write_attribution() 以 O_EXCL 寫入一次，拒絕覆寫
#   3. read_attribution() 讀回；沒有就是 None（legacy run）
#   4. digest_of() 產生設計與流程的內容指紋
#   5. owned_by() 判定某個 project 是否擁有這次 run
#   6. attribution_boundary() 安裝／讀取歸屬邊界，壞掉即 fail-closed
# 維護提醒:
#   - **不得提供更新歸屬的函式。** 可被改寫的歸屬等於沒有歸屬；
#     一次 run 的 Project/Profile 是它的身分，不是它的設定。
#   - 不得在讀不到歸屬時猜一個。legacy run 沒有歸屬是事實，
#     畫面要說「這筆執行沒有留下歸屬」，不是替它編一個。
#   - 不得只存 id 而不存 digest。id 指向的東西會變 —— design 被改、
#     pipeline 被換之後，只存 id 的 run 會被新的定義重新解讀。
#   - **不得在邊界讀不到時退回「現在時間」。** 那會把所有既有 run 一次
#     推到邊界之前，於是刪掉歸屬檔就能把任何一筆變成碩論的 —— 而且
#     偏偏發生在檔案已經壞掉、最不該放寬的時候。
#   - v0.2.0 變更：attribution_boundary() 改為 fail-closed，對應
#     Execution Layer Closure round 2 的 P0-3。
#   - v0.1.0 新增：首版，對應 post-platform audit P0-1。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_run_attribution_adversarial.py -v
# ------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "ATTRIBUTION_FILENAME",
    "ATTRIBUTION_SCHEMA_VERSION",
    "BOUNDARY_FILENAME",
    "attribution_boundary",
    "AttributionBoundaryError",
    "AttributionExistsError",
    "RunAttribution",
    "is_legacy_run",
    "digest_of",
    "owned_by",
    "read_attribution",
    "write_attribution",
]

ATTRIBUTION_FILENAME = "run_identity.json"
ATTRIBUTION_SCHEMA_VERSION = "run_attribution_v1"

#: 沒有歸屬檔的舊 run 歸給誰。
#:
#: 全域 runs 根目錄在平台化之前就是碩論專案在用的，因此沒有歸屬的
#: run 事實上都屬於它。這**不是猜測**，是那段歷史的真實情況；
#: 但畫面仍必須標明「歸屬由 legacy 佈局推得」，而不是假裝有快照。
LEGACY_ATTRIBUTION_NOTE = "此執行早於歸屬機制，Project 由 legacy 佈局推得。"

#: 記錄「歸屬機制從哪一刻起生效」的標記檔。
#:
#: 用**安裝一次的標記**而不是寫死的日期：寫死的日期無論選哪一天都
#: 會錯一邊 —— 選太早會讓既有的舊 run 全變孤兒，選太晚會讓今天新建
#: 的 run 落在 legacy 側，於是刪掉歸屬檔就能把它變成碩論的（P1-4）。
#: 標記在首次使用時安裝，因此當下已存在的 run 都是 legacy，
#: 之後建立的一律必須自帶歸屬。
BOUNDARY_FILENAME = ".attribution_boundary.json"


class AttributionExistsError(RuntimeError):
    """歸屬已存在。**不覆寫。**"""


class AttributionBoundaryError(RuntimeError):
    """讀不到、也裝不上歸屬邊界。**呼叫端必須據此拒絕，不得自己補一個。**"""


def digest_of(payload: Any) -> str:
    """內容指紋。用來偵測「id 沒變但內容變了」。"""
    if payload is None:
        return ""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RunAttribution:
    """一次 run 在啟動當下的歸屬。"""

    run_id: str
    project_id: str
    project_name: str = ""
    profile_id: str = ""
    profile_name: str = ""
    profile_version: str = ""
    profile_state: str = ""
    design_digest: str = ""
    pipeline_id: str = ""
    pipeline_digest: str = ""
    #: 啟動當下 pipeline 的 stage 順序。**歷史 run 依這個重播事件。**
    stage_ids: tuple[str, ...] = ()
    #: 這次 run 讀寫的科學資料根目錄，供結果歸屬追溯。
    artifact_roots: Mapping[str, str] = field(default_factory=dict)
    #: 啟動當下 PipelineDefinition 的**完整** JSON。
    #:
    #: 只存 stage id 不夠：日後 provider 改了節點文案、Input/Process/
    #: Output 說明或 artifact 角色，歷史 run 會被新的定義重新解釋，
    #: 而畫面不會說它被改寫過（P1-1）。
    pipeline_snapshot: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "profile_state": self.profile_state,
            "design_digest": self.design_digest,
            "pipeline_id": self.pipeline_id,
            "pipeline_digest": self.pipeline_digest,
            "stage_ids": list(self.stage_ids),
            "artifact_roots": dict(self.artifact_roots),
            "pipeline_snapshot": dict(self.pipeline_snapshot),
            "created_at": self.created_at
            or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunAttribution:
        schema = str(data.get("schema_version", ""))
        if schema != ATTRIBUTION_SCHEMA_VERSION:
            raise ValueError(
                f"run attribution declares schema_version={schema!r}; this build "
                f"understands {ATTRIBUTION_SCHEMA_VERSION!r} only"
            )
        return cls(
            run_id=str(data["run_id"]),
            project_id=str(data["project_id"]),
            project_name=str(data.get("project_name", "")),
            profile_id=str(data.get("profile_id", "")),
            profile_name=str(data.get("profile_name", "")),
            profile_version=str(data.get("profile_version", "")),
            profile_state=str(data.get("profile_state", "")),
            design_digest=str(data.get("design_digest", "")),
            pipeline_id=str(data.get("pipeline_id", "")),
            pipeline_digest=str(data.get("pipeline_digest", "")),
            stage_ids=tuple(str(s) for s in data.get("stage_ids", ())),
            artifact_roots=dict(data.get("artifact_roots", {})),
            pipeline_snapshot=dict(data.get("pipeline_snapshot", {})),
            created_at=str(data.get("created_at", "")),
            note=str(data.get("note", "")),
        )


def write_attribution(run_dir: str | Path, attribution: RunAttribution) -> None:
    """寫入歸屬。**只能寫一次。**

    用 O_EXCL 而不是「不存在才寫」：可被改寫的歸屬等於沒有歸屬，
    而覆寫的那一刻正是歷史被改掉的那一刻。
    """
    path = Path(run_dir) / ATTRIBUTION_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(attribution.to_json(), indent=2, ensure_ascii=False)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AttributionExistsError(
            f"run {attribution.run_id!r} already has an attribution; a run's "
            "project and profile are its identity, not its settings"
        ) from None
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload + "\n")


def read_attribution(run_dir: str | Path) -> RunAttribution | None:
    """讀回歸屬。沒有就是 None —— **不替它編一個**。"""
    path = Path(run_dir) / ATTRIBUTION_FILENAME
    if not path.is_file():
        return None
    try:
        return RunAttribution.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, json.JSONDecodeError, KeyError):
        return None


def _read_boundary(path: Path) -> str:
    """讀出已安裝的邊界。壞了就拋，**不回一個看起來合理的值**。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        installed = str(payload["installed_at"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise AttributionBoundaryError(
            f"attribution boundary at {path.as_posix()} is unreadable "
            f"({type(error).__name__}); refusing to guess when attribution "
            "started, because a guessed boundary silently re-dates every run "
            "that has no attribution file"
        ) from None
    if _as_instant(installed) is None:
        raise AttributionBoundaryError(
            f"attribution boundary at {path.as_posix()} records "
            f"installed_at={installed!r}, which is not a time"
        )
    return installed


def attribution_boundary(run_root: str | Path) -> str:
    """歸屬機制在這個 run root 上生效的時刻。**安裝一次，之後只讀。**

    第一次呼叫時以當下時間寫入：那一刻已經存在的 run 都是 legacy，
    之後建立的一律必須自帶歸屬。

    **讀不到或寫不進去時一律拋 AttributionBoundaryError，不得改用
    「現在時間」。** 以當下補出來的邊界會把所有既有 run 一次推到邊界
    之前，於是「刪掉歸屬檔就變成碩論的」這條路又打開了 —— 而且是在
    磁碟壞掉、檔案被改壞的時候打開，正是最不該放寬的時候。
    """
    path = Path(run_root) / BOUNDARY_FILENAME
    if path.is_file():
        return _read_boundary(path)
    # **完整精度，不截到秒。** 截掉小數就是把邊界往前挪最多一秒，
    # 而那一秒內剛建立的 run 會落到邊界之後 —— 明明在安裝前就存在，
    # 卻被判成孤兒。差別只在毫秒，所以它只有在機器忙的時候才出現。
    stamp = datetime.now(timezone.utc).astimezone().isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 競態：另一個行程剛裝好。讀它的即可。
        return _read_boundary(path)
    except OSError as error:
        raise AttributionBoundaryError(
            f"cannot install the attribution boundary at {path.as_posix()}: "
            f"{type(error).__name__}: {error}"
        ) from None
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "installed_at": stamp,
            "note": (
                "Runs started before this moment predate run attribution and "
                "belong to the legacy layout. Runs started after it must carry "
                "their own run_identity.json; a missing one makes them orphaned, "
                "not legacy."
            ),
        }, indent=2) + "\n")
    return stamp


def _as_instant(value: str) -> datetime | None:
    """把 ISO 字串解析成帶時區的時間點。解析不了就回 None。"""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_legacy_run(started_at: str, boundary: str) -> bool:
    """這筆 run 是否落在歸屬機制生效之前。

    邊界之後開始、卻沒有歸屬檔的 run **不是 legacy**，是孤兒 ——
    兩者必須分開，否則刪掉歸屬檔就能把任何 run 變成碩論的。

    **必須比對時間點，不是字串。** run record 的時間是 UTC（+00:00），
    邊界寫的是本地時區（+08:00）；直接比字串會讓同一刻的 "13:04" 排在
    "21:04" 之前，於是剛建立的 run 被判成 legacy —— 正好是這條規則
    要防的方向。
    """
    if not started_at:
        # 連開始時間都沒有的紀錄無法定位在邊界的哪一邊。
        # 保守地當成孤兒，不當成 legacy。
        return False
    started = _as_instant(started_at)
    edge = _as_instant(boundary)
    if started is None or edge is None:
        return False
    return started < edge


def owned_by(
    attribution: RunAttribution | None,
    project_id: str,
    *,
    legacy_project_id: str,
    started_at: str = "",
    boundary: str = "",
) -> bool:
    """這個 project 是否擁有這次 run。

    沒有歸屬檔時只有**邊界之前**的 run 才歸 legacy 專案：全域 runs
    根目錄在平台化之前確實是它在用的。邊界之後沒有歸屬的 run 誰都
    不擁有 —— 它是孤兒，不是碩論的。
    """
    if attribution is None:
        return (
            bool(boundary)
            and is_legacy_run(started_at, boundary)
            and project_id == legacy_project_id
        )
    return attribution.project_id == project_id


def snapshot_stage_ids(
    attribution: RunAttribution | None, fallback: Sequence[str] = ()
) -> tuple[str, ...]:
    """重播事件時該用哪一組 stage。

    有歸屬就用歸屬記下的那一組；**不得改用目前 UI 選中的 pipeline**，
    那會讓同一筆 run 在不同專案下顯示不同的歷史。
    """
    if attribution is not None and attribution.stage_ids:
        return attribution.stage_ids
    return tuple(fallback)
