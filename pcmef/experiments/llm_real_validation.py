# PC-MEF Research System source maintenance contract
# 上下游: 讀 outputs/perception/ds_v2（frozen weights）、outputs/perception/gate/
#         （frozen gate rule 與 stress manifest）、outputs/perception/gate_validation/；
#         經 pcmef.llm.registry 解析 binding、pcmef.agents.pcmef_agents 執行四角色；
#         產出 outputs/llm_validation/real_agent_validation.json。
#         **不讀、不寫、不觸碰 families 36-43。**
# 檔案路徑: pcmef/experiments/llm_real_validation.py
# 產生時間: 2026-08-31 23:40 +08:00
# 版本: v0.1.0
# 功能說明: 用真實 provider 跑一次四 agent，並在**線路層**檢查該檢查的事：
#           影像真的以 bytes 送出、角色隔離真的發生在 payload、structured 回應
#           真的經過 schema 驗證、retry 與 fail-closed 真的會觸發。
# 模組定位: freeze 前的最後一道閘。它「不是」研究結果 —— 這裡只驗執行與格式，
#           不看 accuracy，也不得依 gate-validation 分數回頭改 prompt。
# 主要責任:
#   1. 從 frozen artifact 重建每個 case 的 p_m / q_m / D-U-Q（不重新擬合任何東西）
#   2. 以 record_wire() 攔截 httpx，保留每次真實請求的 body 供事後檢查
#   3. CHECKS 逐條驗證影像、隔離、洩漏、schema、retry、cache、class_support
#   4. 以故障注入驗證 retry 與 ABORT_FORMAL_RUN，不需要真實呼叫
#   5. 產出可稽核報告，任何一條 FAIL 就讓 READY_FOR_FINAL_E2 = NO
# 維護提醒:
#   - 不得為了讓檢查通過而改 prompt、gate threshold、reliability proxy 或
#     temperature scaling。這一輪只能修 provider serialization / schema /
#     圖片傳輸 / retry / cache 這類 execution bug。
#   - 不得把本檔指向 families 36-43。那批是一次性的 final test，
#     在 llm_runtime.lock 凍結前不得產生，遑論讀取。
#   - 不得把這裡的輸出當成 PC-MEF 的效能證據。它不量 accuracy。
#   - 不得放寬任何一條 CHECK 讓報告變綠；CHECK 失敗的正確反應是修 execution。
#   - v0.1.0 新增：首版 real-agent 驗證。
# 驗證方式:
#   - docker compose exec console python -m pcmef.cli llm validate-agents
# ------------------------------------------------------------

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from pcmef.agents.pcmef_agents import (
    AGENTS,
    MAX_ATTEMPTS,
    AgentError,
    AgentRunner,
    RetryExhaustedError,
    build_case_evidence,
    case_cache_key,
    project_for_role,
    run_pcmef_case,
)
from pcmef.agents.provider import (
    EVIDENCE_IMAGES_KEY,
    ConnectionProfile,
    ModelDescriptor,
    ProviderError,
    get_adapter,
)
from pcmef.core.hash import hash_file, hash_object

__all__ = ["run_real_validation", "CheckResult", "record_wire"]

#: 這一輪允許讀取的資料。families 36-43 不在其中，而且本檔沒有任何路徑指向它。
GATE_VALIDATION_DIR = Path("outputs/perception/gate_validation")
DS_DIR = Path("outputs/perception/ds_v2")
GATE_DIR = Path("outputs/perception/gate")

#: 真實呼叫的 case 數。刻意小 —— 這裡驗的是執行路徑，不是統計量，
#: 而每個 case 是四次真實 provider 呼叫（要花錢也要花時間）。
DEFAULT_CASES = 3

#: payload 裡絕對不能出現的欄位名與值。前三個是 ground truth 與產生條件
#: （NOTE-003 / NOTE-004），後兩個是會反推出類別的路徑片段。
FORBIDDEN_PAYLOAD_KEYS: tuple[str, ...] = (
    "class_label", "class_index", "true_class", "ground_truth", "label",
    "condition", "severity", "family_index", "physical_scene_family",
    "scenario_id", "split", "rgb_path", "tof_path", "exr_path",
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class WireRecord:
    """一次真實 HTTP 請求的紀錄。body 保留原樣供線路層檢查。"""

    url: str
    body: dict[str, Any]

    @property
    def parts(self) -> list[dict[str, Any]]:
        return (self.body.get("contents") or [{}])[0].get("parts", [])

    @property
    def text_part(self) -> str:
        return "".join(str(p.get("text", "")) for p in self.parts)

    @property
    def image_parts(self) -> list[dict[str, Any]]:
        return [p["inlineData"] for p in self.parts if "inlineData" in p]


@contextmanager
def record_wire() -> Iterator[list[WireRecord]]:
    """攔截 httpx.request，把送出的 body 收下來後**照常送出**。

    這是唯一能證明「影像真的上了線路」的方法。檢查 adapter 的中間結果只能
    證明轉換函式對，證明不了 invoke 用了那個轉換 —— 這個缺陷實際發生過
    （NOTE-047），而且沒有任何症狀。
    """
    import httpx

    original = httpx.request
    records: list[WireRecord] = []

    def recording(method, url, headers=None, json=None, timeout=None, **kwargs):
        if json is not None:
            records.append(WireRecord(url=str(url), body=json))
        return original(
            method, url, headers=headers, json=json, timeout=timeout, **kwargs
        )

    httpx.request = recording
    try:
        yield records
    finally:
        httpx.request = original


# ---------------------------------------------------------------------------
# 由 frozen artifact 重建每個 case 的證據
# ---------------------------------------------------------------------------


#: 主機算好的 case 證據落腳處。
#:
#: 為什麼要分兩段：perception 需要 torch（在主機上），而 registry 與 vault
#: 只存在於容器的具名 volume。把 torch 裝進容器只為了跑幾個 case 不划算，
#: 把 secrets 搬到主機則會違反 docker-compose 明列的禁令。
#: outputs/ 兩邊都掛得到，所以拿它當交接點：主機寫證據，容器讀證據去問模型。
#: 這個檔**不含 ground truth、condition、family index 或任何路徑**。
EVIDENCE_CACHE = Path("outputs/llm_validation/evidence_cases.json")


def prepare_evidence_cases(
    limit: int = DEFAULT_CASES, progress: Callable[[str], None] | None = None
) -> Path:
    """在主機上算好每個 case 的 p_m / q_m / D-U-Q 並落盤。需要 torch。"""
    say = progress or (lambda _m: None)
    import base64

    from pcmef.agents.pcmef_agents import encode_image_evidence

    cases = _compute_cases(limit)
    payload = []
    for case in cases:
        payload.append(
            {
                "index": case["index"],
                # 影像先編成 PNG：容器端不必再碰 EXR，也不必知道它從哪來。
                "image": encode_image_evidence(case["rgb"]),
                "tof": np.asarray(case["tof"], dtype=np.float64).tolist(),
                "p_vision": case["p_vision"],
                "p_tof": case["p_tof"],
                "q_vision": case["q_vision"],
                "q_tof": case["q_tof"],
                "duq": case["duq"],
                "route": case["route"],
            }
        )
    EVIDENCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_CACHE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    leaked = _contains_forbidden(payload)
    if leaked:
        raise AgentError(f"evidence cache leaks {leaked}; refusing to write it")
    say(f"wrote {len(payload)} case(s) to {EVIDENCE_CACHE}")
    return EVIDENCE_CACHE


def _load_cases(limit: int) -> list[dict[str, Any]]:
    """讀主機算好的證據。容器端走這條，不需要 torch。"""
    if not EVIDENCE_CACHE.exists():
        raise AgentError(
            f"{EVIDENCE_CACHE} not found. Run `pcmef llm prepare-evidence` on the "
            "host first: the perception models need torch, which the container "
            "image deliberately does not carry."
        )
    raw = json.loads(EVIDENCE_CACHE.read_text(encoding="utf-8"))
    return [
        {
            "index": c["index"],
            "image": c["image"],
            "tof": np.asarray(c["tof"], dtype=np.float64),
            "p_vision": c["p_vision"], "p_tof": c["p_tof"],
            "q_vision": c["q_vision"], "q_tof": c["q_tof"],
            "duq": c["duq"], "route": c["route"],
        }
        for c in raw[:limit]
    ]


def _compute_cases(limit: int) -> list[dict[str, Any]]:
    """從 gate-validation 取樣本並算出 p_m / q_m / D-U-Q。

    全部使用既有 frozen 模型與 frozen gate rule，**不重新擬合任何東西**。
    """
    from pcmef.perception.dataset import _load_exr, _rgb_tonemap, _tof_transform
    from pcmef.perception.gate import (
        GateRule,
        _logits,
        duq_signals,
        fit_reliability_model,
        load_frozen_models,
        quality_signals,
        reliability_route,
        reliability_scores,
        softmax,
    )

    vision_model, tof_model, preprocessing = load_frozen_models(DS_DIR)
    manifest = json.loads(
        (GATE_VALIDATION_DIR / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    frozen = json.loads((GATE_DIR / "gate_rule.json").read_text(encoding="utf-8"))
    rule_cfg = frozen["gate_rule"]
    rule = GateRule(
        q_vision_threshold=rule_cfg["q_vision_threshold"],
        q_tof_threshold=rule_cfg["q_tof_threshold"],
        disagreement_threshold=rule_cfg["disagreement_threshold"],
        fusion_weight=rule_cfg["fusion_weight"],
        temperature_vision=rule_cfg["temperature_vision"],
        temperature_tof=rule_cfg["temperature_tof"],
    )

    samples = manifest["samples"]
    raw_rgb, raw_tof, prep_rgb, prep_tof = [], [], [], []
    for s in samples:
        image = _rgb_tonemap(_load_exr(s["rgb"]["exr_path"]))
        raw_rgb.append(image)
        normed = (image - np.asarray(preprocessing["rgb"]["mean"])) / np.asarray(
            preprocessing["rgb"]["std"]
        )
        prep_rgb.append(np.ascontiguousarray(normed.transpose(2, 0, 1), dtype=np.float32))
        record = np.load(s["tof"]["path"])
        raw_tof.append(record)
        rec = _tof_transform(record)
        rec = (rec - np.asarray(preprocessing["tof"]["mean"])) / np.asarray(
            preprocessing["tof"]["std"]
        )
        prep_tof.append(np.ascontiguousarray(rec.transpose(1, 0), dtype=np.float32))

    p_vision = softmax(_logits(vision_model, np.stack(prep_rgb)), rule.temperature_vision)
    p_tof = softmax(_logits(tof_model, np.stack(prep_tof)), rule.temperature_tof)
    # quality_signals 回的是 vision_sharpness / tof_snr，duq_signals 再把它們
    # 轉成 Q_vision / Q_tof。照原始 API 走，不自己另算一份同名的量。
    per_case = [quality_signals(r, t) for r, t in zip(raw_rgb, raw_tof)]
    quality = {
        "vision_sharpness": np.asarray([q["vision_sharpness"] for q in per_case]),
        "tof_snr": np.asarray([q["tof_snr"] for q in per_case]),
    }
    signals = duq_signals(p_vision, p_tof, quality)
    clean_mask = np.ones(len(samples), dtype=bool)  # gate_validation 本體即 clean
    reliability = fit_reliability_model(signals, clean_mask, rule)
    q = reliability_scores(signals, reliability)
    routes = reliability_route(q, signals, rule)

    cases = []
    for index in range(min(limit, len(samples))):
        cases.append(
            {
                "index": index,
                "rgb": raw_rgb[index],
                "tof": raw_tof[index],
                "p_vision": p_vision[index].tolist(),
                "p_tof": p_tof[index].tolist(),
                "q_vision": float(q["q_vision"][index]),
                "q_tof": float(q["q_tof"][index]),
                "duq": {k: float(v[index]) for k, v in signals.items()},
                "route": str(routes[index]),
            }
        )
    return cases


def _binding(store_dir: Path) -> tuple[ConnectionProfile, ModelDescriptor, Any, dict]:
    """從 draft registry 解析四角色共用的 binding，並取得可用的 adapter。"""
    from pcmef.llm.registry import LLMRegistry
    from pcmef.secrets.vault import SecretVault

    registry = LLMRegistry(store_dir / "llm_admin.db")
    vault = SecretVault("secrets/vault.json", local_master_key=True)

    bindings = {b.task_code: b for b in registry.list_bindings()}
    profiles = {code: b.model_profile_id for code, b in bindings.items()}
    # 2026-09-01（NOTE-052）：四個角色**可以**分散到多把 API key，那是
    # 吞吐決策。但 model_id 與 provider_revision 必須完全相同 —— 換模型
    # 是科學決策，會讓 llm_runtime.lock 記載的推論器對應不到執行的東西。
    resolved = {
        code: registry.get_model_profile(pid) for code, pid in profiles.items()
    }
    identities = {
        code: (mp.model_id, mp.provider_revision or "")
        for code, mp in resolved.items()
    }
    if len(set(identities.values())) != 1:
        raise AgentError(
            "the four roles must share one model identity (model_id and "
            f"provider_revision); got {identities}. Multiple API keys are "
            "allowed; multiple models are not."
        )
    model_profile = resolved[sorted(resolved)[0]]
    connection = registry.get_connection(model_profile.connection_id)

    profile = ConnectionProfile(
        connection_id=connection.connection_id,
        provider=connection.provider,
        secret_ref=connection.secret_ref,
        base_url=connection.base_url or "",
        timeout_sec=connection.timeout_sec,
    )
    descriptor = ModelDescriptor(
        model_id=model_profile.model_id,
        provider=connection.provider,
        provider_revision=model_profile.provider_revision or "",
    )
    adapter = get_adapter(connection.provider, resolve_secret=vault.resolve)
    identity = {
        "provider": connection.provider,
        "model_id": model_profile.model_id,
        "provider_revision": model_profile.provider_revision or "",
        "verified_capabilities": sorted(
            c.value if hasattr(c, "value") else str(c)
            for c in model_profile.verified_capabilities
        ),
        "connection_id": connection.connection_id,
        "bound_tasks": sorted(profiles),
        # 多把 key 時，role -> connection 的對應必須記下來：
        # 「哪個角色打了哪一把 key」是重跑與配額診斷的前提。
        "role_connection_map": {
            code: resolved[code].connection_id for code in sorted(resolved)
        },
        "distinct_connections": sorted(
            {mp.connection_id for mp in resolved.values()}
        ),
    }
    role_bindings: dict[str, tuple[Any, Any, Any]] = {}
    if len(identity["distinct_connections"]) > 1:
        for code, mp in resolved.items():
            conn = registry.get_connection(mp.connection_id)
            role_bindings[code] = (
                get_adapter(conn.provider, resolve_secret=vault.resolve),
                ConnectionProfile(
                    connection_id=conn.connection_id,
                    provider=conn.provider,
                    secret_ref=conn.secret_ref,
                    base_url=conn.base_url or "",
                    timeout_sec=conn.timeout_sec,
                ),
                ModelDescriptor(
                    model_id=mp.model_id,
                    provider=conn.provider,
                    provider_revision=mp.provider_revision or "",
                ),
            )
    identity["role_bindings"] = role_bindings
    return profile, descriptor, adapter, identity


# ---------------------------------------------------------------------------
# 檢查
# ---------------------------------------------------------------------------


def _contains_forbidden(payload: Any) -> list[str]:
    """遞迴找出 payload 裡的洩漏欄位名。值不檢查 —— 類別詞彙本身是合法的。"""
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key) in FORBIDDEN_PAYLOAD_KEYS:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(payload, "payload")
    return found


def _check_wire(records: list[WireRecord], checks: list[CheckResult]) -> None:
    """線路層檢查：影像、隔離、洩漏、schema、temperature。"""
    import base64

    vision_calls = [r for r in records if r.image_parts]
    checks.append(CheckResult(
        "vision_roles_send_real_image_bytes",
        len(vision_calls) >= 2,
        f"{len(vision_calls)} request(s) carried inlineData",
    ))
    if vision_calls:
        raw = base64.b64decode(vision_calls[0].image_parts[0]["data"])
        checks.append(CheckResult(
            "image_bytes_are_a_decodable_png",
            raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 1000,
            f"magic={raw[:4]!r} bytes={len(raw)}",
        ))

    text_only = [r for r in records if not r.image_parts]
    checks.append(CheckResult(
        "text_only_roles_send_no_image",
        len(text_only) >= 2,
        f"{len(text_only)} request(s) had no image part",
    ))

    schemas = [r for r in records
               if (r.body.get("generationConfig") or {}).get("responseSchema")]
    checks.append(CheckResult(
        "every_request_asks_for_a_json_schema",
        len(schemas) == len(records),
        f"{len(schemas)}/{len(records)} carried responseSchema",
    ))

    temps = [(r.body.get("generationConfig") or {}).get("temperature") for r in records]
    checks.append(CheckResult(
        "temperature_is_zero_on_the_wire",
        all(t == 0.0 for t in temps),
        f"temperatures={sorted({str(t) for t in temps})}",
    ))

    leaked = []
    for record in records:
        try:
            payload = json.loads(record.text_part)
        except (json.JSONDecodeError, TypeError):
            continue
        leaked.extend(_contains_forbidden(payload))
    checks.append(CheckResult(
        "no_ground_truth_or_condition_in_the_payload",
        not leaked,
        "clean" if not leaked else f"leaked {sorted(set(leaked))[:6]}",
    ))


def _check_isolation(evidence: dict, checks: list[CheckResult]) -> None:
    """角色隔離必須發生在 payload，而不是只寫在 prompt 裡。"""
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    physics = project_for_role("physics_agent", body)
    visual = project_for_role("visual_semantic_agent", body)
    observation = project_for_role("observation_agent", body)
    arbitration = project_for_role("arbitration_agent", body)

    def flat(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    checks.append(CheckResult(
        "physics_receives_no_vision_evidence",
        physics["calibrated_class_probabilities"].get("vision") is None
        and "q_vision" not in flat(physics)
        and "Q_vision" not in flat(physics)
        and "U_vision" not in flat(physics),
        f"sections={sorted(physics)}",
    ))
    checks.append(CheckResult(
        "visual_receives_no_tof_prediction",
        "tof_summary" not in visual
        and visual["calibrated_class_probabilities"].get("tof") is None
        and "q_tof" not in flat(visual)
        and "Q_tof" not in flat(visual),
        f"sections={sorted(visual)}",
    ))
    checks.append(CheckResult(
        "observation_receives_no_class_probabilities",
        "calibrated_class_probabilities" not in observation
        and "modality_reliability" not in observation
        and "cross_modal" not in observation
        and "predictive_entropy" not in observation,
        f"sections={sorted(observation)}",
    ))
    checks.append(CheckResult(
        "no_role_receives_the_gate_route",
        all("gate_route" not in flat(p)
            for p in (observation, physics, visual, arbitration)),
        "gate_route correlates with condition and is withheld from every role",
    ))
    checks.append(CheckResult(
        "arbitration_gets_numerics_but_not_raw_evidence",
        "cross_modal" in arbitration and "tof_summary" not in arbitration,
        f"sections={sorted(arbitration)}",
    ))
    checks.append(CheckResult(
        "specialists_only_disagree_on_independent_evidence",
        not ({"vision", "q_vision", "Q_vision", "U_vision"} & set(flat(physics).split('"')))
        and not ({"tof", "q_tof", "Q_tof", "U_tof"} & set(flat(visual).split('"'))),
        "neither specialist sees any quantity from the other modality",
    ))
    checks.append(CheckResult(
        "reliability_is_not_derived_from_softmax_or_entropy",
        set(evidence["modality_reliability"]["forbidden_sources"]) >= {
            "max softmax probability", "predictive entropy",
        },
        "q_m declares its forbidden sources and reads only sensor evidence",
    ))


def _check_fault_paths(
    profile, descriptor, adapter, evidence, checks: list[CheckResult]
) -> None:
    """retry 與 fail-closed 用故障注入驗證，不需要真實呼叫。"""
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    images = evidence[EVIDENCE_IMAGES_KEY]
    spec = AGENTS["observation_agent"]

    class _Corrupting:
        """第一次回傳壞 JSON，第二次放行 —— 驗證 retry 真的會發生。"""

        def __init__(self, inner, failures: int) -> None:
            self.inner, self.failures = inner, failures
            self.calls = 0

        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            self.calls += 1
            if self.calls <= self.failures:
                from pcmef.agents.provider import ProviderResponse

                return ProviderResponse(
                    text="not json at all", model_id=model.model_id,
                    provider=model.provider,
                )
            return self.inner.invoke(connection, model, task_code, payload, runtime_cfg)

    recovering = AgentRunner(
        adapter=_Corrupting(adapter, failures=1),
        connection=profile, model=descriptor,
    )
    try:
        recovering.run(spec, body, images=images)
        recovered = [a.ok for a in recovering.attempts] == [False, True]
        detail = f"attempts={[a.ok for a in recovering.attempts]}"
    except RetryExhaustedError as error:
        recovered, detail = False, str(error)[:120]
    checks.append(CheckResult("invalid_json_is_retried_and_can_recover", recovered, detail))

    exhausting = AgentRunner(
        adapter=_Corrupting(adapter, failures=MAX_ATTEMPTS),
        connection=profile, model=descriptor,
    )
    aborted = False
    detail = "no exception raised"
    try:
        exhausting.run(spec, body, images=images)
    except RetryExhaustedError as error:
        aborted = "ABORT_FORMAL_RUN" in str(error)
        detail = f"attempts={len(exhausting.attempts)}; aborts the run"
    checks.append(CheckResult(
        "retry_exhaustion_aborts_instead_of_dropping_the_case", aborted, detail
    ))


def _check_cache_identity(evidence, identity, checks: list[CheckResult]) -> None:
    kwargs = dict(
        model_id=identity["model_id"],
        provider_revision=identity["provider_revision"],
        runtime_config_hash="cfg",
    )
    base = case_cache_key(evidence, **kwargs).digest()
    same = case_cache_key(evidence, **kwargs).digest()
    other_model = case_cache_key(evidence, **{**kwargs, "model_id": "other"}).digest()
    other_case = case_cache_key(
        {**evidence, "gate_route": "trust_vision"}, **kwargs
    ).digest()
    components = case_cache_key(evidence, **kwargs).components()
    checks.append(CheckResult(
        "cache_key_covers_evidence_model_prompt_and_schema",
        base == same and base != other_model and base != other_case
        and set(components["prompt_hashes"]) == set(AGENTS)
        and bool(components["schema_hash"]),
        f"stable, model-sensitive, evidence-sensitive; "
        f"prompt_hashes={len(components['prompt_hashes'])}",
    ))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_real_validation(
    store_dir: str | Path = "registry",
    out_dir: str | Path = "outputs/llm_validation",
    cases: int = DEFAULT_CASES,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    say = progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    say("resolving the draft binding")
    profile, descriptor, adapter, identity = _binding(Path(store_dir))
    say(f"  {identity['provider']} / {identity['model_id']} "
        f"rev={identity['provider_revision']}")

    say(f"rebuilding evidence for {cases} gate-validation case(s)")
    loaded = _load_cases(cases)

    checks: list[CheckResult] = []
    bundles: list[dict[str, Any]] = []
    wire: list[WireRecord] = []

    for case in loaded:
        evidence = build_case_evidence(
            image=case["image"], tof=case["tof"],
            p_vision=case["p_vision"], p_tof=case["p_tof"],
            q_vision=case["q_vision"], q_tof=case["q_tof"],
            duq=case["duq"],
        )
        say(f"case {case['index']} route={case['route']} -> four real calls")
        runner = AgentRunner(adapter=adapter, connection=profile, model=descriptor)
        with record_wire() as records:
            bundle = run_pcmef_case(runner, evidence)
        wire.extend(records)

        arbitration = bundle.artifacts["arbitration_validated"]
        support = arbitration["class_support"]
        bundles.append({
            "case_index": case["index"],
            "route": case["route"],
            "class_support": support,
            "support_sum": round(sum(support.values()), 6),
            "raw_support_sum": arbitration.get("support_sum_before_normalisation"),
            "within_tolerance": arbitration.get("support_sum_within_tolerance"),
            "conflict_tag": arbitration["conflict_tag"],
            "attempts": [a.ok for a in runner.attempts],
        })

        if case is loaded[0]:
            _check_isolation(evidence, checks)
            _check_cache_identity(evidence, identity, checks)
            _check_fault_paths(profile, descriptor, adapter, evidence, checks)

    say("checking the recorded wire traffic")
    _check_wire(wire, checks)

    checks.append(CheckResult(
        "all_four_agents_produced_schema_valid_output",
        len(bundles) == len(loaded) and all(b["conflict_tag"] for b in bundles),
        f"{len(bundles)} case(s) completed all six artifacts",
    ))
    checks.append(CheckResult(
        "arbitration_class_support_sums_to_100",
        all(abs(b["support_sum"] - 100.0) < 1e-6 for b in bundles),
        f"sums={[b['support_sum'] for b in bundles]} "
        f"(raw={[b['raw_support_sum'] for b in bundles]})",
    ))

    prompt_hashes = {code: spec.prompt_hash() for code, spec in AGENTS.items()}
    schema_hashes = {
        spec.schema_name: hash_file(Path("schemas") / f"{spec.schema_name}.schema.json")
        for spec in AGENTS.values()
    }
    passed = all(c.passed for c in checks)
    document = {
        "report_id": "llm_real_agent_validation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scientific_result": False,
        "purpose": (
            "execution and format validation only. This report does not measure "
            "accuracy and must not be cited as evidence that PC-MEF works."
        ),
        "data_source": GATE_VALIDATION_DIR.as_posix(),
        "families_touched": "gate-validation only; families 36-43 untouched",
        "runtime_identity": identity,
        "prompt_hashes": prompt_hashes,
        "schema_hashes": schema_hashes,
        "real_calls": len(wire),
        "cases": bundles,
        "checks": [c.to_dict() for c in checks],
        "all_checks_passed": passed,
        "READY_FOR_FINAL_E2": "YES" if passed else "NO",
    }
    path = out / "real_agent_validation.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    say(f"wrote {path}")
    return document
