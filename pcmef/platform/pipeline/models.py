# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.pipeline.registry 與各 provider 建構；console 的
#         Pipeline 頁與 Run 頁渲染本檔的結構。**純資料，不讀檔。**
# 檔案路徑: pcmef/platform/pipeline/models.py
# 產生時間: 2026-09-07 14:10 +08:00
# 版本: v0.1.0
# 功能說明: PipelineDefinition 與 PipelineStage —— 一個 Profile 的流程
#           由幾個 stage 組成、每個 stage 收什麼、做什麼、產出什麼。
# 模組定位: 平台化 Phase 6。**節點數量不是固定的。**
#           PC-MEF 的七節點只是「PC-MEF Profile 的 PipelineDefinition」；
#           UI 與執行模型都不得假設有七個。
# 主要責任:
#   1. PipelineStage 定義單一 stage 的身分、順序、依賴與 I/O 描述
#   2. PipelineDefinition 承載一份完整流程並驗證其依賴
#   3. validate() 擋下未知依賴與重複 stage_id
#   4. to_json() 供畫面與 run manifest 使用
# 維護提醒:
#   - **不得在本檔或任何 UI 假設 stage 數量。** 七節點是 PC-MEF 的形狀，
#     不是平台的形狀；寫死七個會讓三節點的專案在畫面上壞掉或說謊。
#   - 不得讓 depends_on 指向不存在的 stage。那會讓執行順序在某些
#     排列下靜默錯亂，而畫面看起來完全正常。
#   - 不得把 runtime 進度存進 PipelineDefinition。定義是靜態的，
#     進度屬於某一次 run；混在一起會讓兩次 run 互相覆蓋。
#   - v0.1.0 新增：首版，對應平台化 Phase 6。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_pipeline_definition.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "PIPELINE_SCHEMA_VERSION",
    "PipelineDefinition",
    "PipelineStage",
    "PipelineDefinitionError",
]

PIPELINE_SCHEMA_VERSION = "pipeline_definition_v1"


class PipelineDefinitionError(ValueError):
    """流程定義本身不成立。**建構時就拒絕，不要留到執行才發現。**"""


@dataclass(frozen=True)
class PipelineStage:
    """流程中的一個 stage。

    `carries` 寫的是它**交給下一個 stage** 的東西 —— 黑箱感來自看不見
    中間傳遞了什麼，而不是看不見每一步叫什麼名字。
    """

    stage_id: str
    display_name: str
    english: str = ""
    summary: str = ""
    #: 交給下一個 stage 的東西。
    carries: str = ""
    input_desc: str = ""
    process_desc: str = ""
    output_desc: str = ""
    #: 這個 stage 會產生哪幾類 artifact（角色名稱，不是路徑）。
    artifact_roles: tuple[str, ...] = ()
    #: 必須先完成的 stage。空的代表它是起點。
    depends_on: tuple[str, ...] = ()
    #: 非必要的 stage：某些 run 不會經過它（例如只有困難案例才仲裁）。
    optional: bool = False
    #: 額外的、由 provider 填入的顯示細節（例如 lock 還原出的實際值）。
    detail: Any = None

    def to_json(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "display_name": self.display_name,
            "english": self.english,
            "summary": self.summary,
            "carries": self.carries,
            "input": self.input_desc,
            "process": self.process_desc,
            "output": self.output_desc,
            "artifact_roles": list(self.artifact_roles),
            "depends_on": list(self.depends_on),
            "optional": self.optional,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PipelineStage:
        """由 to_json() 的輸出還原。**`detail` 不還原。**

        `detail` 是 provider 由當下的 lock 填進去的顯示值，不在 to_json()
        裡，也不該由快照重建 —— 那會讓歷史 run 顯示今天的門檻。
        """
        return cls(
            stage_id=str(data.get("stage_id", "")),
            display_name=str(data.get("display_name", "")),
            english=str(data.get("english", "")),
            summary=str(data.get("summary", "")),
            carries=str(data.get("carries", "")),
            input_desc=str(data.get("input", "")),
            process_desc=str(data.get("process", "")),
            output_desc=str(data.get("output", "")),
            artifact_roles=tuple(str(r) for r in data.get("artifact_roles", ())),
            depends_on=tuple(str(d) for d in data.get("depends_on", ())),
            optional=bool(data.get("optional", False)),
        )


@dataclass(frozen=True)
class PipelineDefinition:
    """一份 Profile 的流程定義。**stage 數量由定義決定。**"""

    pipeline_id: str
    display_name: str
    stages: tuple[PipelineStage, ...] = ()
    note: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """重複 id 與未知依賴一律在建構時擋下。"""
        seen: set[str] = set()
        for stage in self.stages:
            if stage.stage_id in seen:
                raise PipelineDefinitionError(
                    f"duplicate stage_id {stage.stage_id!r} in pipeline "
                    f"{self.pipeline_id!r}"
                )
            seen.add(stage.stage_id)
        for stage in self.stages:
            unknown = [d for d in stage.depends_on if d not in seen]
            if unknown:
                raise PipelineDefinitionError(
                    f"stage {stage.stage_id!r} depends on unknown stage(s) "
                    f"{unknown}; a dependency that names nothing silently "
                    "reorders execution"
                )

    def __len__(self) -> int:
        return len(self.stages)

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return tuple(s.stage_id for s in self.stages)

    def stage(self, stage_id: str) -> PipelineStage | None:
        for item in self.stages:
            if item.stage_id == stage_id:
                return item
        return None

    def next_of(self, stage_id: str) -> PipelineStage | None:
        ids = self.stage_ids
        if stage_id not in ids:
            return None
        index = ids.index(stage_id)
        return self.stages[index + 1] if index + 1 < len(self.stages) else None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "pipeline_id": self.pipeline_id,
            "display_name": self.display_name,
            "note": self.note,
            "stage_count": len(self.stages),
            "stages": [s.to_json() for s in self.stages],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PipelineDefinition:
        """由快照還原一份流程定義。**不呼叫任何 provider。**

        歷史 run 的形狀與文案必須完全從它自己的快照重建：向 provider
        要一次「同一個 id 現在長什麼樣」，等於讓今天的定義去解釋昨天的
        執行 —— 而畫面不會說它被改寫過（P1-1 / P1-6）。

        schema 不認得就拒絕。用讀不懂的資料硬拼出一份定義，等於在
        「還原」的名義下重新發明歷史。
        """
        schema = str(data.get("schema_version", ""))
        if schema != PIPELINE_SCHEMA_VERSION:
            raise PipelineDefinitionError(
                f"pipeline snapshot declares schema_version={schema!r}; this "
                f"build understands {PIPELINE_SCHEMA_VERSION!r} only"
            )
        return cls(
            pipeline_id=str(data.get("pipeline_id", "")),
            display_name=str(data.get("display_name", "")),
            note=str(data.get("note", "")),
            stages=tuple(
                PipelineStage.from_json(s) for s in data.get("stages", ())
            ),
        )
