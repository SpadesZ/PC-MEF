# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.platform.catalog 讀 manifest 與組出內建元件；讀
#         pcmef.core.constants.CLASS_ORDER 與 simulation.scenario.MediumPreset
#         描述碩論的四個情境。**不寫任何東西，也不被任何 stable module 匯入。**
# 檔案路徑: pcmef/platform/scenarios.py
# 產生時間: 2026-09-27 01:38 +08:00（首次提交 d1cf266 的 commit 時間）
# 版本: v0.1.0
# 功能說明: Scenario Plugin 的契約（SAI Appendix B、§7.3）：身分、分類、
#           標籤角色、物理支援（template-backed / plugin-implemented /
#           plugin-required）、參數與宣告的感測家族；以及碩論四個情境的描述。
# 模組定位: Phase 3 第一片。讓「情境」成為平台上的具名元件；碩論的四類只是
#           **被描述** —— CLASS_ORDER 與物理參數一個都沒有搬到這裡。
# 主要責任:
#   1. CATEGORIES：§7.2 Step 1 的情境分類（封閉列舉，含 custom）
#   2. physics_templates()：由 MediumPreset 推導的可用物理模板
#   3. ScenarioPlugin + parse_scenario()：契約驗證，不合即 ContractViolation
#   4. thesis_scenarios()：碩論四類的內建描述，順序即 CLASS_ORDER
# 維護提醒:
#   - **不得讓 registry 的順序變成類別順序。** 碩論的 class order 只有
#     CLASS_ORDER 一個來源（Appendix A 第 4 條）；registry 依 id 排序。
#   - 不得在內建描述裡複製物理參數。參數屬於凍結的 ScenarioConfig 與校準，
#     在這裡再宣告一次就是第二個真相來源（Appendix A 第 2 條）。
#   - 不得讓 manifest 使用碩論的類別名稱當 classification_label。新增的
#     情境不得擴充或改寫碩論的四類（ACC-SCN-04）。
#   - 不得把 plugin_required 的情境當成可模擬。沒有模板、也沒有實作的
#     定義只是定義（§7.3：不得假裝任何新材料只要填幾個值就科學成立）。
#   - v0.1.0 新增：首版，對應 SAI Phase 3 第一片。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_extension_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pcmef.core.constants import CLASS_ORDER
from pcmef.platform.components import (
    KIND_SCENARIO,
    NOT_ASSESSED,
    ComponentIdentity,
    ContractViolation,
    Fields,
    ParameterSpec,
    Provenance,
    check_entrypoint,
    content_sha256,
    parse_parameters,
    read_identity,
)
from pcmef.platform.sensors import SENSOR_FAMILIES

__all__ = [
    "CATEGORIES",
    "CONTRACT",
    "SUPPORT_PLUGIN",
    "SUPPORT_REQUIRED",
    "SUPPORT_TEMPLATE",
    "ScenarioPlugin",
    "parse_scenario",
    "physics_templates",
    "thesis_scenarios",
]

CONTRACT = "pcmef.scenario_plugin/v1"

#: §7.2 Step 1 的情境分類。
CATEGORIES: tuple[str, ...] = (
    "empty_gas", "liquid", "liquid_with_bubbles", "aerosol_mist",
    "granular_material", "solid_object", "custom",
)

#: §7.3 的三種物理支援。由定義**推導**，不由作者宣告。
SUPPORT_TEMPLATE = "template_backed"       # 現有模擬器的模板能表示
SUPPORT_PLUGIN = "plugin_implemented"      # 有自己的實作
SUPPORT_REQUIRED = "plugin_required"       # 兩者皆無：只是一份定義


def physics_templates() -> dict[str, str]:
    """現有模擬器能表示的物理模板：`pcmef.medium.<preset>` → preset。

    由 MediumPreset 推導，不另列一份。模板只是**名字**，不含任何散射係數
    —— 具體物理參數由校準決定（見 simulation.scenario.MediumPreset）。
    """
    from pcmef.simulation.scenario import MediumPreset

    return {f"pcmef.medium.{preset.value}": preset.value for preset in MediumPreset}


@dataclass(frozen=True)
class ScenarioPlugin:
    """一個已通過契約驗證的 Scenario Plugin 描述。"""

    identity: ComponentIdentity
    display_name: str
    category: str
    description: str
    classification_label: str | None
    environment_state: bool
    template_id: str | None
    implementation: str | None
    parameters: tuple[ParameterSpec, ...]
    declared_sensor_families: tuple[str, ...]
    dependencies: tuple[str, ...]
    known_limitations: tuple[str, ...]
    tests: tuple[str, ...]
    provenance: Provenance

    @property
    def simulation_support(self) -> str:
        if self.template_id is not None:
            return SUPPORT_TEMPLATE
        if self.implementation is not None:
            return SUPPORT_PLUGIN
        return SUPPORT_REQUIRED

    def describe(self) -> dict[str, Any]:
        return {
            **self.identity.describe(),
            "contract": CONTRACT,
            "display_name": self.display_name,
            "category": self.category,
            "description": self.description,
            "label_role": {
                "classification_label": self.classification_label,
                "environment_state": self.environment_state,
            },
            "physics": {
                "support": self.simulation_support,
                "template_id": self.template_id,
                "implementation": self.implementation,
                "parameters": [p.describe() for p in self.parameters],
            },
            # 作者的**宣告**。哪些感測器真的相容，是 compatibility matrix 的事。
            "declared_sensor_families": list(self.declared_sensor_families),
            "dependencies": list(self.dependencies),
            "known_limitations": list(self.known_limitations),
            "tests": list(self.tests),
            "provenance": self.provenance.describe(),
            "compatibility": dict(NOT_ASSESSED),
        }


def parse_scenario(data: Mapping[str, Any], *, source: str, location: str,
                   builtin: bool = False) -> ScenarioPlugin:
    """驗證一份 Scenario Plugin 定義。不合契約就 ContractViolation，列出全部問題。"""
    problems: list[str] = []
    fields = Fields(data, "scenario", problems)
    contract = fields.text("contract")
    if contract and contract != CONTRACT:
        problems.append(f"contract {contract!r} is not {CONTRACT!r}")
    identity = read_identity(fields, KIND_SCENARIO, builtin=builtin)
    display_name = fields.text("display_name")
    category = fields.text("category")
    description = fields.text("description")

    role = Fields(fields.take("label_role", (dict,), default={}), "label_role", problems)
    label = role.take("classification_label", (str, type(None)), default=None)
    environment_state = role.take("environment_state", (bool,), default=False)
    role.done()

    physics = Fields(fields.take("physics", (dict,), default={}), "physics", problems)
    template_id = physics.take("template_id", (str, type(None)), default=None)
    implementation = physics.take("implementation", (str, type(None)), default=None)
    parameters = parse_parameters(physics.take("parameters", (dict,), default={}),
                                  "physics.parameters", problems)
    physics.done()

    families = fields.strings("declared_sensor_families")
    dependencies = fields.strings("dependencies")
    known_limitations = fields.strings("known_limitations")
    tests = fields.strings("tests", required=False)
    fields.done()

    if category and category not in CATEGORIES:
        problems.append(f"category {category!r} is not one of {list(CATEGORIES)}")
    if label is not None and not label.strip():
        problems.append("label_role.classification_label must not be blank")
    if (label is not None) == environment_state:
        # §7.2 Step 2：新增為分類標籤，**或**只是 environment / nuisance state。
        problems.append(
            "label_role must be exactly one of: a classification_label, or "
            "environment_state true"
        )
    if label in CLASS_ORDER and not builtin:
        problems.append(
            f"classification_label {label!r} is a frozen thesis class; a plugin "
            "may not extend or redefine the thesis class space (ACC-SCN-04)"
        )
    templates = physics_templates()
    if template_id is not None and template_id not in templates:
        problems.append(
            f"physics.template_id {template_id!r} is not a known template "
            f"{sorted(templates)}; an unknown template is refused, never "
            "replaced by a default medium"
        )
    if template_id is not None and implementation is not None:
        problems.append(
            "physics names both a template and an implementation; which one "
            "simulates this scenario would be ambiguous"
        )
    check_entrypoint(implementation, "physics.implementation", problems)
    unknown = [f for f in families if f not in SENSOR_FAMILIES]
    if unknown:
        problems.append(
            f"declared_sensor_families {unknown} are not in {list(SENSOR_FAMILIES)}"
        )
    if len(set(families)) != len(families):
        problems.append(f"declared_sensor_families repeat an entry: {list(families)}")

    if problems:
        raise ContractViolation(location, problems)
    return ScenarioPlugin(
        identity=identity, display_name=display_name, category=category,
        description=description, classification_label=label,
        environment_state=environment_state, template_id=template_id,
        implementation=implementation, parameters=parameters,
        declared_sensor_families=families, dependencies=dependencies,
        known_limitations=known_limitations, tests=tests,
        provenance=Provenance(source, location, content_sha256(data)),
    )


# ---------------------------------------------------------------------------
# 碩論的四個情境：被描述，不被改寫
# ---------------------------------------------------------------------------

#: 分類對照。鍵必須與 CLASS_ORDER 完全一致 —— 少一類就在建立時 KeyError。
_CATEGORY_BY_CLASS: dict[str, str] = {
    "Empty": "empty_gas",
    "Water-filled": "liquid",
    "Bubbly": "liquid_with_bubbles",
    "Misty": "aerosol_mist",
}

_BUILTIN_LOCATION = "pcmef.platform.scenarios:thesis_scenarios"


def thesis_scenarios() -> tuple[ScenarioPlugin, ...]:
    """碩論的四類，**依 CLASS_ORDER 的順序**。沒有模組層級的快取或註冊。"""
    from pcmef.simulation.scenario import MediumPreset

    scenarios = []
    for index, label in enumerate(CLASS_ORDER):
        definition = {
            "contract": CONTRACT,
            "id": f"pcmef.scenario.{label.lower()}",
            "version": "1.0.0",
            "author": "PC-MEF thesis (frozen semantics)",
            "display_name": label,
            "category": _CATEGORY_BY_CLASS[label],
            "description": f"Thesis class {label!r} (CLASS_ORDER index {index}).",
            "label_role": {"classification_label": label, "environment_state": False},
            "physics": {
                "template_id": f"pcmef.medium.{MediumPreset.for_class(label).value}",
                "implementation": None,
                "parameters": {},
            },
            "declared_sensor_families": ["rgb_camera", "time_of_flight"],
            "dependencies": [],
            "known_limitations": [
                "physical parameters are owned by the frozen thesis ScenarioConfig "
                "and calibration and are not re-declared here",
                "the class order is CLASS_ORDER; the registry's ordering is not a "
                "class order",
            ],
        }
        scenarios.append(parse_scenario(definition, source="builtin",
                                        location=_BUILTIN_LOCATION, builtin=True))
    return tuple(scenarios)
