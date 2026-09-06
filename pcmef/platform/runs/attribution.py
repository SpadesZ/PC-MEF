# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes 啟動 run 時寫入一次，之後由 Run / Results
#         頁讀取。**write-once：寫下之後不得再改。**
# 檔案路徑: pcmef/platform/runs/attribution.py
# 產生時間: 2026-09-08 09:20 +08:00
# 版本: v0.1.0
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
# 維護提醒:
#   - **不得提供更新歸屬的函式。** 可被改寫的歸屬等於沒有歸屬；
#     一次 run 的 Project/Profile 是它的身分，不是它的設定。
#   - 不得在讀不到歸屬時猜一個。legacy run 沒有歸屬是事實，
#     畫面要說「這筆執行沒有留下歸屬」，不是替它編一個。
#   - 不得只存 id 而不存 digest。id 指向的東西會變 —— design 被改、
#     pipeline 被換之後，只存 id 的 run 會被新的定義重新解讀。
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
    "AttributionExistsError",
    "RunAttribution",
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


class AttributionExistsError(RuntimeError):
    """歸屬已存在。**不覆寫。**"""


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


def owned_by(
    attribution: RunAttribution | None, project_id: str, *, legacy_project_id: str
) -> bool:
    """這個 project 是否擁有這次 run。

    沒有歸屬的舊 run 屬於 legacy 專案：全域 runs 根目錄在平台化之前
    就是它在用的。這不是猜測，是那段歷史的事實。
    """
    if attribution is None:
        return project_id == legacy_project_id
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
