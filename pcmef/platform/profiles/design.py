# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.profiles.models 與 thesis 匯入；console 的 Research
#         Design 頁渲染本檔的結構。**純資料結構，不讀 freeze、不算科學值。**
# 檔案路徑: pcmef/platform/profiles/design.py
# 產生時間: 2026-09-06 20:10 +08:00
# 版本: v0.1.0
# 功能說明: Research Design（研究設計）的結構化描述，以及每一個欄位的
#           出處與「可執行真相在哪」兩種 provenance。
# 模組定位: 平台化 Phase 3。研究設計是**敘述**，不是執行來源 ——
#           門檻、class order 與 severity 的可執行真相在 freeze/ 的 lock，
#           本檔只負責說明「這個 Profile 的研究設計長什麼樣、出自計畫書
#           哪一節、以及真正被執行的那份在哪」。
# 主要責任:
#   1. DesignField 綁定值、計畫書出處與 authoritative lock
#   2. DesignSection 把欄位分組成畫面上的一段
#   3. ResearchDesign 承載整份設計並可序列化
#   4. SECTION_ORDER 固定畫面上的段落順序
# 維護提醒:
#   - **不得把本檔當成科學參數的來源。** 任何 executor 若讀這裡取門檻，
#     就會出現與 freeze/ 不同的第二份真相，而兩份必然漂移。
#     欄位上的 `authoritative` 標的就是為了讓這件事在畫面上是明說的。
#   - 不得在本檔重新解釋實驗計畫。欄位內容一律逐條對應 v1.2.1，
#     計畫書沒寫的東西不得由平台補上。
#   - 不得為了畫面好看而省略 `source`。沒有出處的設計敘述無法被稽核，
#     而無法稽核的敘述遲早會與計畫書分歧。
#   - v0.1.0 新增：首版，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_research_design.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DESIGN_SCHEMA_VERSION",
    "SECTION_ORDER",
    "DesignField",
    "DesignSection",
    "ResearchDesign",
    "build_section",
]

DESIGN_SCHEMA_VERSION = "research_design_v1"

#: 畫面上的段落順序。照研究敘事推進：先問題，再感測，再方法，最後判準。
SECTION_ORDER: tuple[str, ...] = (
    "objective",
    "research_questions",
    "modalities",
    "class_space",
    "conditions",
    "data_roles",
    "experiments",
    "calibration",
    "perception",
    "reliability",
    "routing",
    "agents",
    "primary_endpoint",
    "statistics",
    "claim_boundary",
)


@dataclass(frozen=True)
class DesignField:
    """研究設計的一個欄位。

    `source` 是計畫書出處，`authoritative` 是「真正被執行的那份在哪」。
    兩者刻意分開：計畫書說門檻是 0.5，而執行時讀的是 gate.lock ——
    當兩者分歧時，必須看得出來該信哪一個，以及該去修哪一份。
    """

    label: str
    value: Any
    #: 實驗計畫 v1.2.1 的章節，例如 "§3.4"。
    source: str
    #: 可執行真相所在的 lock 名稱；None 表示這一項沒有對應的 lock。
    authoritative: str | None = None
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "authoritative": self.authoritative,
            "note": self.note,
        }


@dataclass(frozen=True)
class DesignSection:
    """畫面上的一段研究設計。"""

    key: str
    title: str
    summary: str
    fields: tuple[DesignField, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "fields": [f.to_json() for f in self.fields],
        }


@dataclass(frozen=True)
class ResearchDesign:
    """一個 Profile 的完整研究設計。

    第一版**唯讀**：內容由 v1.2.1 映射而來，平台不提供編輯。
    可編輯之前要先有「編輯之後那份還算不算計畫書所述的設計」的答案。
    """

    source_document: str
    title: str
    sections: tuple[DesignSection, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def section(self, key: str) -> DesignSection | None:
        for item in self.sections:
            if item.key == key:
                return item
        return None

    def ordered_sections(self) -> list[DesignSection]:
        """依 SECTION_ORDER 排序；不在清單裡的排在最後，不被丟掉。

        丟掉未知段落會讓新增一段設計後畫面上什麼都沒變 ——
        那種錯誤看起來像「還沒寫」，而不是「排序漏了」。
        """
        rank = {key: index for index, key in enumerate(SECTION_ORDER)}
        return sorted(
            self.sections, key=lambda s: (rank.get(s.key, len(SECTION_ORDER)), s.key)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": DESIGN_SCHEMA_VERSION,
            "source_document": self.source_document,
            "title": self.title,
            "sections": [s.to_json() for s in self.sections],
            "extra": dict(self.extra),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ResearchDesign:
        schema = str(data.get("schema_version", ""))
        if schema != DESIGN_SCHEMA_VERSION:
            raise ValueError(
                f"research design declares schema_version={schema!r}; this build "
                f"understands {DESIGN_SCHEMA_VERSION!r} only"
            )
        return cls(
            source_document=str(data.get("source_document", "")),
            title=str(data.get("title", "")),
            sections=tuple(_section(item) for item in data.get("sections", ())),
            extra=dict(data.get("extra", {})),
        )


def _section(data: Mapping[str, Any]) -> DesignSection:
    return DesignSection(
        key=str(data["key"]),
        title=str(data.get("title", data["key"])),
        summary=str(data.get("summary", "")),
        fields=tuple(_field(item) for item in data.get("fields", ())),
    )


def _field(data: Mapping[str, Any]) -> DesignField:
    return DesignField(
        label=str(data["label"]),
        value=data.get("value"),
        source=str(data.get("source", "")),
        authoritative=data.get("authoritative"),
        note=str(data.get("note", "")),
    )


def build_section(
    key: str, title: str, summary: str, fields: Iterable[Sequence[Any]]
) -> DesignSection:
    """以 tuple 序列建段，讓 thesis.py 的內容排版接近計畫書表格。

    每個 field 是 (label, value, source[, authoritative[, note]])。
    """
    built = []
    for row in fields:
        label, value, source, *rest = row
        built.append(
            DesignField(
                label=label,
                value=value,
                source=source,
                authoritative=rest[0] if len(rest) > 0 else None,
                note=rest[1] if len(rest) > 1 else "",
            )
        )
    return DesignSection(key=key, title=title, summary=summary, fields=tuple(built))
