# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.audit.e1_gates、pcmef.audit.firewall、
#         pcmef.audit.split_policy 匯入；由 pcmef.cli 的 audit 子指令
#         取用其 to_artifact() 落盤與 exit_code() 決定回傳碼。不自行讀寫檔案。
# 檔案路徑: pcmef/audit/result.py
# 產生時間: 2026-08-27 08:10 +08:00
# 版本: v0.1.0
# 功能說明: 定義稽核結果的四種狀態與彙整方式。關鍵在於把「檢查不通過」與
#           「證據還沒產出」分成兩種狀態 —— 後者在目前階段是正確結果，
#           不該被算成失敗。
# 模組定位: 稽核輸出的共同型別。它「不是」檢查邏輯，也不決定哪些檢查該跑。
# 主要責任:
#   1. CheckStatus 區分 PASS / FAIL / NOT_PRODUCED / BLOCKED 四種結果
#   2. CheckResult 保存單一檢查的判定、證據路徑與具體 findings
#   3. AuditReport.counts() 統計各狀態數量
#   4. AuditReport.exit_code() 依 required 集合決定回傳碼
#   5. AuditReport.to_artifact() 產生可落盤、可比對的 JSON
#   6. AuditReport.lines() 產生對齊的人類可讀輸出
# 維護提醒:
#   - 不得把 NOT_PRODUCED 併進 FAIL。稽核器先於被稽核的產物存在是刻意的，
#     把「還沒做」報成「做錯了」會讓整份報告失去訊息量，
#     而讀報告的人會學會忽略它。
#   - 不得讓 exit_code() 在沒有 required 集合時因 NOT_PRODUCED 回非零；
#     不指定要求就只是盤點。但 FAIL 任何情況下都必須回非零。
#   - 不得為了讓報告好看而省略 findings；判定為 FAIL 時必須說出哪一項不符。
#   - v0.1.0 新增：首版四狀態模型，決策見 NOTE-022。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_audit_result.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

__all__ = ["CheckStatus", "CheckResult", "AuditReport"]


class CheckStatus(str, Enum):
    """單一檢查的四種結果。

    NOT_PRODUCED 與 FAIL 分開是本模組存在的理由：稽核系統刻意先於被稽核的
    產物建立，因此「證據還沒產出」在目前階段是**正確**輸出。
    """

    PASS = "PASS"
    FAIL = "FAIL"
    #: 證據 artifact 尚未產出。屬預期狀態，不計入失敗。
    NOT_PRODUCED = "NOT_PRODUCED"
    #: 前置條件未滿足，因此無法判定。與 NOT_PRODUCED 的差別在於
    #: 這一項不是「還沒做」，而是「做了也讀不出結論」。
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class CheckResult:
    """單一檢查的判定結果。"""

    identifier: str
    requirement: str
    status: CheckStatus
    detail: str = ""
    evidence: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is CheckStatus.FAIL and not self.findings:
            raise ValueError(
                f"{self.identifier} is FAIL but lists no findings; a failed check "
                "must say which condition was not met"
            )

    def to_artifact(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "requirement": self.requirement,
            "status": self.status.value,
            "detail": self.detail,
            "evidence": list(self.evidence),
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class AuditReport:
    """一次稽核的完整結果。"""

    name: str
    results: tuple[CheckResult, ...]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # -- 統計 -------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in CheckStatus}
        for result in self.results:
            counts[result.status.value] += 1
        return counts

    def by_status(self, status: CheckStatus) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is status)

    def get(self, identifier: str) -> CheckResult:
        for result in self.results:
            if result.identifier == identifier:
                return result
        raise KeyError(
            f"{identifier!r} is not part of the {self.name} audit; "
            f"known checks: {[r.identifier for r in self.results]}"
        )

    # -- 判定 -------------------------------------------------------------

    def exit_code(self, required: Iterable[str] | None = None) -> int:
        """回傳 CLI exit code。

        沒有 required 集合時，本次執行只是盤點，NOT_PRODUCED 不計入失敗 ——
        稽核器先於產物存在，把「還沒做」報成失敗會讓這個指令從第一天起
        就一直是紅的，然後沒有人會再看它。

        但 FAIL 一律回非零：那代表**已產出**的證據自相矛盾，
        與「還沒產出」是兩回事。
        """
        if required is None:
            return 1 if self.by_status(CheckStatus.FAIL) else 0
        return 1 if self.unmet(required) else 0

    def unmet(self, required: Iterable[str]) -> tuple[CheckResult, ...]:
        """列出被要求、但目前不是 PASS 的檢查。"""
        return tuple(
            self.get(identifier)
            for identifier in required
            if self.get(identifier).status is not CheckStatus.PASS
        )

    # -- 輸出 -------------------------------------------------------------

    def to_artifact(self) -> dict[str, Any]:
        return {
            "audit": self.name,
            "created_at": self.created_at,
            "counts": self.counts(),
            "checks": [result.to_artifact() for result in self.results],
        }

    def lines(self) -> list[str]:
        width = max((len(r.identifier) for r in self.results), default=0)
        out: list[str] = []
        for result in self.results:
            out.append(
                f"[{result.status.value:>12}] {result.identifier:<{width}}  "
                f"{result.detail}"
            )
            for finding in result.findings:
                out.append(f"{'':>15} - {finding}")
        return out
