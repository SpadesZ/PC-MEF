# PC-MEF Research System source maintenance contract
# 上下游: 匯入 pcmef.agents.provider（normalized adapter 契約）、
#         pcmef.agents.cache（六份 artifact 的 content-addressed 快取）、
#         pcmef.llm.capabilities（§45 task registry）。
#         被未來的 experiments.e2_formal 呼叫；本檔**不**讀取任何 dataset。
# 檔案路徑: pcmef/agents/pcmef_agents.py
# 產生時間: 2026-08-31 20:05 +08:00
# 版本: v0.1.0
# 功能說明: 四個正式 Agent（observation / physics / visual_semantic / arbitration）
#           的執行體。負責把一個 escalated case 的證據組成 FIXED_SUMMARY payload、
#           送進 provider、把回應 parse 成 schema-valid 物件，並在重試耗盡時
#           讓整個 formal run 失敗而不是悄悄少算一個 case。
# 模組定位: SRC-SAI §45 role contract 的唯一實作處。
#           它「不是」路由器 —— 要不要 escalate 由 perception.gate.reliability_route
#           決定，本模組只在被 escalate 時才會被呼叫。
#           它也「不是」provider —— 任何 HTTP 都必須經過 LLMProviderAdapter。
# 主要責任:
#   1. AGENTS 依 §45 宣告四個角色的 schema、是否需要影像、以及上游依賴
#   2. build_case_evidence() 產生 FIXED_SUMMARY payload（含 q_v / q_t）
#   3. encode_image_evidence() 把 RGB 陣列編成 PNG bytes，**不傳路徑**
#   4. AgentRunner.run() 執行 invoke + parse + jsonschema 驗證 + bounded retry
#   5. 重試耗盡拋 RetryExhaustedError（ABORT_FORMAL_RUN），不得回傳 None
#   6. run_pcmef_case() 串起四個 agent，產出 cache 要的六份 artifact
# 維護提醒:
#   - 不得在本模組直接呼叫 httpx / google.generativeai / openai。所有外呼
#     一律經過 LLMProviderAdapter；否則 NOTE-007 的 URL 洩漏防護、
#     error sanitization 與 capability probe 全部繞過。
#   - 不得把 max-softmax、temperature-scaled confidence 或 predictive entropy
#     當成 modality reliability 送進 payload。q_m 只能來自
#     perception.gate.reliability_scores（sensor-quality / degradation evidence）。
#     pilot 已實測：ToF 校準溫度 T=0.0498 讓劣化的 ToF「高信心地錯」。
#   - 不得以檔案路徑代替影像 bytes。路徑會把 class/condition 洩漏進 payload
#     （NOTE-003 / NOTE-004），而且 provider 收到路徑也看不到圖 ——
#     vision agent 會安靜地退化成純文字 agent，指標照樣算得出來。
#   - 不得在重試耗盡時 drop case。少算一個 case 會讓分母悄悄變小，
#     而那正是 bounded retry 要防的事（SRC-SAI §28 / FR-013）。
#   - 不得讓 stub_offline 的輸出進入任何 formal 結果。它只證明工程路徑走得通。
#   - v0.1.0 新增：首版四 agent 實作，決策見 NOTE-047。
# 驗證方式:
#   - py -3.10 -m pytest tests/agents/test_pcmef_agents.py -v
# ------------------------------------------------------------

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from pcmef.agents.cache import AgentBundle, AgentCacheKey
from pcmef.agents.provider import (
    EVIDENCE_IMAGES_KEY,
    ProviderError,
    ProviderResponse,
)
from pcmef.core.hash import hash_object

__all__ = [
    "AgentError",
    "RetryExhaustedError",
    "AgentSpec",
    "AGENTS",
    "REPRESENTATION_MODE",
    "MAX_ATTEMPTS",
    "RELIABILITY_CLAUSE",
    "encode_image_evidence",
    "tof_fixed_summary",
    "build_case_evidence",
    "AgentRunner",
    "run_pcmef_case",
]


class AgentError(RuntimeError):
    """Agent 執行失敗：回應無法 parse、不符 schema，或角色契約被違反。"""


class RetryExhaustedError(AgentError):
    """重試預算用盡。

    這是一個**終止整個 formal run** 的訊號，不是「這個 case 跳過」。
    configs/base.yaml 的 agents.retry.on_exhaustion = ABORT_FORMAL_RUN。
    呼叫端若把它吞掉再 continue，分母就會悄悄變小。
    """


#: 已核定的表徵模式（NOTE-046）。payload 建構器只認這一個值。
REPRESENTATION_MODE = "FIXED_SUMMARY"

#: 第一次 + 一次 retry。與 configs/base.yaml 的 agents.retry.max_attempts 一致。
MAX_ATTEMPTS = 2

#: 四個類別的固定順序，與 configs/base.yaml 的 agents.class_order 相同。
CLASS_ORDER: tuple[str, ...] = ("Empty", "Water-filled", "Bubbly", "Misty")


#: 每個 prompt 都必須攜帶的一段話。
#:
#: 這不是禮貌性的提醒 —— pilot 的 negative result 正是「把預測信心當成
#: 感測可靠度」造成的：溫度校準後 ToF 的 T = 0.0498，劣化的 ToF 因此
#: 高信心地錯，信心加權仲裁被拉向錯的那一邊（conflict 0.302，
#: 單看 Vision 反而 0.677）。模型若自行把 class probability 的高低
#: 解讀成「這個感測器比較可信」，就會在 prompt 層重演同一個錯誤。
RELIABILITY_CLAUSE = (
    "CRITICAL — predictive confidence is NOT sensor reliability. "
    "The class probabilities you are given are calibrated p(y|x) from a frozen "
    "classifier. A sharply peaked distribution means the classifier committed, "
    "NOT that the sensor was working. A degraded sensor can and does produce "
    "confidently wrong predictions. "
    "Judge modality reliability ONLY from the supplied reliability scores "
    "q_vision / q_tof and the sensor-quality evidence behind them "
    "(sharpness, signal-to-noise, degradation margin, cross-modal support). "
    "Do NOT derive reliability from the maximum class probability, from the "
    "temperature-scaled confidence, or from the entropy of the class "
    "distribution."
)

_ANONYMITY_CLAUSE = (
    "The two specialist opinions are anonymous. Do not assume either modality "
    "is inherently superior, and do not try to identify which is which beyond "
    "the modality tag already given."
)

_SYSTEM_PROMPTS: dict[str, str] = {
    "observation_agent": (
        "You are the Observation Agent in a multi-modal pipe-content inspection "
        "system. You receive an RGB image and a summary of a time-of-flight (ToF) "
        "return waveform from the SAME physical scene. "
        "Report only what is observable. Do NOT propose or hint at a class. "
        "Assess the quality of each modality's evidence and list what is missing. "
        + RELIABILITY_CLAUSE
    ),
    "physics_agent": (
        "You are the Physics Agent. You reason from the time-of-flight return "
        "waveform: its timing, spread, amplitude and signal-to-noise, and what "
        "those imply about the medium filling the pipe. "
        "You may consult the RGB-side evidence only to note contradictions. "
        "Give per-class evidence strengths in [0, 1]; they need not sum to 1. "
        + RELIABILITY_CLAUSE
    ),
    "visual_semantic_agent": (
        "You are the Visual-Semantic Agent. You reason from the RGB image: "
        "texture, transparency, specular highlights, colour and visible structure, "
        "and what those imply about the medium filling the pipe. "
        "You may consult the ToF-side evidence only to note contradictions. "
        "Give per-class evidence strengths in [0, 1]; they need not sum to 1. "
        + RELIABILITY_CLAUSE
    ),
    "arbitration_agent": (
        "You are the Arbitration Agent. You receive a neutral observation brief "
        "and two anonymous specialist proposals, together with the reliability "
        "scores of each modality. Produce a class_support distribution and tag "
        "the level of conflict. "
        "When one modality is markedly more reliable, weight it accordingly. "
        "When both are weak, say so through a higher conflict tag rather than "
        "inventing certainty. "
        + _ANONYMITY_CLAUSE + " " + RELIABILITY_CLAUSE
    ),
}


@dataclass(frozen=True)
class AgentSpec:
    """一個角色的執行契約。欄位對應 §45 的一列。"""

    task_code: str
    schema_name: str
    #: 是否必須收到影像。與 capabilities.TASK_REGISTRY 的 VISION 要求一致，
    #: 由 assert_registry_consistent() 在 import 後驗證，不是各寫一份。
    needs_image: bool
    #: 這個角色的產物寫進 cache 的哪一份 artifact（raw, validated）。
    artifact_names: tuple[str, str]

    @property
    def system_prompt(self) -> str:
        return _SYSTEM_PROMPTS[self.task_code]

    def prompt_hash(self) -> str:
        return hash_object({"system": self.system_prompt, "task": self.task_code})


AGENTS: dict[str, AgentSpec] = {
    spec.task_code: spec
    for spec in (
        AgentSpec(
            task_code="observation_agent",
            schema_name="observation_brief_v1",
            needs_image=True,
            artifact_names=("observation_raw", "observation_validated"),
        ),
        AgentSpec(
            task_code="physics_agent",
            schema_name="specialist_proposal_v1",
            needs_image=False,
            artifact_names=("physics_proposal", "physics_proposal"),
        ),
        AgentSpec(
            task_code="visual_semantic_agent",
            schema_name="specialist_proposal_v1",
            needs_image=True,
            artifact_names=("visual_proposal", "visual_proposal"),
        ),
        AgentSpec(
            task_code="arbitration_agent",
            schema_name="arbitration_output_v1",
            needs_image=False,
            artifact_names=("arbitration_raw", "arbitration_validated"),
        ),
    )
}


def assert_registry_consistent() -> None:
    """本模組的 needs_image 必須等於 §45 的 VISION 要求。

    兩處各寫一份「誰需要看圖」遲早會分岔，而分岔的症狀是
    vision agent 安靜地不再收到圖 —— 沒有任何例外，指標照樣算得出來。
    """
    from pcmef.agents.provider import Capability
    from pcmef.llm.capabilities import FORMAL_TASK_CODES, required_capabilities

    if tuple(AGENTS) != tuple(FORMAL_TASK_CODES):
        raise AgentError(
            f"agent set {tuple(AGENTS)} does not match the formal task codes "
            f"{tuple(FORMAL_TASK_CODES)}; §45/Appendix J3 fixes the four roles"
        )
    for task_code, spec in AGENTS.items():
        registry_vision = Capability.VISION in required_capabilities(task_code)
        if spec.needs_image != registry_vision:
            raise AgentError(
                f"{task_code}: needs_image={spec.needs_image} but the §45 registry "
                f"says vision-required={registry_vision}"
            )


# ---------------------------------------------------------------------------
# 證據建構（FIXED_SUMMARY）
# ---------------------------------------------------------------------------


def encode_image_evidence(rgb: np.ndarray, mime_type: str = "image/png") -> dict[str, str]:
    """把 RGB 陣列編成 PNG bytes 再 base64。

    刻意接受**陣列**而不是路徑：路徑會把 class 與 condition 寫進 payload
    （NOTE-003 / NOTE-004 的 opaque-evidence 紅線），而且 provider 收到
    一個路徑字串也看不到影像。
    """
    from PIL import Image

    array = np.asarray(rgb)
    if array.ndim == 3 and array.shape[0] in (1, 3) and array.shape[-1] not in (1, 3):
        array = np.transpose(array, (1, 2, 0))  # CHW -> HWC
    if array.ndim == 2:
        array = np.stack([array] * 3, axis=-1)
    if array.ndim != 3 or array.shape[-1] not in (1, 3):
        raise AgentError(
            f"image evidence must be HxWx3 (or CHW/greyscale), got shape {array.shape}"
        )
    if array.shape[-1] == 1:
        array = np.repeat(array, 3, axis=-1)

    finite = np.nan_to_num(array.astype(np.float64), nan=0.0, posinf=1.0, neginf=0.0)
    if finite.dtype != np.uint8 and finite.max() <= 1.0 + 1e-6:
        finite = finite * 255.0
    pixels = np.clip(finite, 0.0, 255.0).astype(np.uint8)

    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="RGB").save(buffer, format="PNG")
    payload = buffer.getvalue()
    return {
        "mime_type": mime_type,
        "data_b64": base64.b64encode(payload).decode("ascii"),
        "sha256": hash_object(base64.b64encode(payload).decode("ascii")),
    }


def tof_fixed_summary(tof: np.ndarray) -> dict[str, float]:
    """ToF 回波的固定欄位摘要。

    §18 FIXED_SUMMARY：欄位集合固定，因此每個 case 的 prompt 長度一致。
    長度一致本身是必要的 —— 長度隨 case 變動會讓 case 之間的條件不同，
    那是一個額外變因。
    """
    waveform = np.asarray(tof, dtype=np.float64).ravel()
    if waveform.size == 0:
        raise AgentError("ToF evidence is empty")
    total = float(waveform.sum())
    index = np.arange(waveform.size, dtype=np.float64)
    peak = int(np.argmax(waveform))
    # 質心與寬度以強度為權重；總和為 0 時退回幾何中心，避免除以 0。
    if total > 0:
        centroid = float((index * waveform).sum() / total)
        spread = float(np.sqrt(((index - centroid) ** 2 * waveform).sum() / total))
    else:
        centroid, spread = float(waveform.size) / 2.0, 0.0
    noise_floor = float(np.percentile(waveform, 10.0))
    peak_value = float(waveform[peak])
    return {
        "n_bins": int(waveform.size),
        "peak_bin": peak,
        "peak_value": round(peak_value, 6),
        "centroid_bin": round(centroid, 4),
        "temporal_spread_bins": round(spread, 4),
        "total_return": round(total, 6),
        "noise_floor_p10": round(noise_floor, 6),
        "peak_to_noise_ratio": round(peak_value / max(noise_floor, 1e-9), 4),
        "leading_edge_bin": int(np.argmax(waveform >= 0.5 * peak_value)),
        "late_tail_fraction": round(
            float(waveform[peak:].sum() / max(total, 1e-9)), 6
        ),
    }


def _class_distribution(proba: Sequence[float]) -> dict[str, float]:
    values = [float(v) for v in proba]
    if len(values) != len(CLASS_ORDER):
        raise AgentError(
            f"expected {len(CLASS_ORDER)} class probabilities, got {len(values)}"
        )
    return {name: round(v, 6) for name, v in zip(CLASS_ORDER, values)}


def build_case_evidence(
    *,
    rgb: np.ndarray,
    tof: np.ndarray,
    p_vision: Sequence[float],
    p_tof: Sequence[float],
    q_vision: float,
    q_tof: float,
    duq: Mapping[str, float],
    route: str,
) -> dict[str, Any]:
    """組成一個 case 的 FIXED_SUMMARY 證據包。

    刻意分成三塊並在 payload 裡明說它們的語意不同：
      * calibrated_class_probabilities —— p_m(y|x)，會不會答對的信心
      * modality_reliability —— q_m，感測器有沒有在正常工作
    這兩者在 pilot 被證實是**不同的東西**；payload 若只給一個數字，
    模型沒有辦法不把它們混為一談。
    """
    return {
        "schema_version": "1.0",
        "representation_mode": REPRESENTATION_MODE,
        "class_order": list(CLASS_ORDER),
        "tof_summary": tof_fixed_summary(tof),
        "calibrated_class_probabilities": {
            "_meaning": (
                "temperature-calibrated p(y|x) from the frozen per-modality "
                "classifiers. This is predictive confidence, NOT sensor reliability."
            ),
            "vision": _class_distribution(p_vision),
            "tof": _class_distribution(p_tof),
        },
        "modality_reliability": {
            "_meaning": (
                "q_m in [0,1], computed ONLY from sensor-quality and degradation "
                "evidence plus cross-modal support. Independent of p(y|x). "
                "q >= 0.5 counts as reliable."
            ),
            "q_vision": round(float(q_vision), 6),
            "q_tof": round(float(q_tof), 6),
            "evidence_sources": [
                "sensor_quality", "degradation_margin", "cross_modal_support",
            ],
            "forbidden_sources": [
                "max softmax probability",
                "temperature-scaled confidence",
                "predictive entropy",
            ],
        },
        "duq_signals": {
            "_meaning": (
                "D = total-variation disagreement between the two modalities, "
                "U = normalised predictive entropy, Q = raw sensor quality."
            ),
            **{k: round(float(v), 6) for k, v in sorted(duq.items())},
        },
        "gate_route": route,
        EVIDENCE_IMAGES_KEY: [encode_image_evidence(rgb)],
    }


# ---------------------------------------------------------------------------
# 執行器
# ---------------------------------------------------------------------------


def _load_schema(schema_name: str, schema_dir: Path) -> dict[str, Any]:
    path = schema_dir / f"{schema_name}.schema.json"
    if not path.exists():
        raise AgentError(f"schema {schema_name!r} not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_json(text: str) -> Any:
    """把回應 parse 成 JSON。容忍 ```json 圍欄，但不容忍缺 JSON。"""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -3]
    body = body.strip()
    if not body:
        raise AgentError("provider returned an empty body where JSON was required")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        # 不回吐整個 body：它可能含 provider 原樣回吐的內容。
        raise AgentError(
            f"provider response is not valid JSON ({error.msg} at pos {error.pos}); "
            f"body length {len(body)}"
        ) from None


@dataclass
class AttemptLog:
    """一次嘗試的結果。失敗原因已遮蔽，可以安全落盤。"""

    task_code: str
    attempt: int
    ok: bool
    error: str = ""


@dataclass
class AgentRunner:
    """把 adapter、connection、model 綁在一起執行四個角色。"""

    adapter: Any
    connection: Any
    model: Any
    schema_dir: Path = field(default_factory=lambda: Path("schemas"))
    max_attempts: int = MAX_ATTEMPTS
    attempts: list[AttemptLog] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise AgentError("max_attempts must be at least 1")
        self.schema_dir = Path(self.schema_dir)

    def run(
        self,
        spec: AgentSpec,
        payload: Mapping[str, Any],
        *,
        images: Sequence[Mapping[str, str]] | None = None,
    ) -> tuple[dict[str, Any], ProviderResponse]:
        """執行一個角色：invoke -> parse -> jsonschema 驗證，含 bounded retry。

        影像與 schema 都是**強制**的：needs_image 的角色沒收到圖就直接拒絕，
        而不是送出一個純文字請求然後照樣回傳一個看起來正常的結果。
        """
        import jsonschema

        schema = _load_schema(spec.schema_name, self.schema_dir)
        body = {k: v for k, v in payload.items() if k != EVIDENCE_IMAGES_KEY}
        supplied = list(images if images is not None else payload.get(EVIDENCE_IMAGES_KEY, []))

        if spec.needs_image and not supplied:
            raise AgentError(
                f"{spec.task_code} requires vision evidence but no image was supplied; "
                "refusing to fall back to a text-only request (that would silently "
                "turn a vision agent into a text agent)"
            )
        if not spec.needs_image and supplied:
            raise AgentError(
                f"{spec.task_code} is a text-only role (§45) but {len(supplied)} "
                "image(s) were supplied; sending them would change the role contract"
            )

        request = {"system": spec.system_prompt, **body}
        if supplied:
            request[EVIDENCE_IMAGES_KEY] = supplied
        runtime_cfg = {"response_schema": schema, "task_code": spec.task_code}

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.adapter.invoke(
                    self.connection, self.model, spec.task_code, request, runtime_cfg
                )
                parsed = _extract_json(response.text)
                jsonschema.validate(parsed, schema)
            except (ProviderError, AgentError, jsonschema.ValidationError) as error:
                last_error = f"{type(error).__name__}: {error}"
                self.attempts.append(
                    AttemptLog(spec.task_code, attempt, ok=False, error=last_error[:400])
                )
                continue
            self.attempts.append(AttemptLog(spec.task_code, attempt, ok=True))
            return parsed, response

        raise RetryExhaustedError(
            f"{spec.task_code} failed all {self.max_attempts} attempts; "
            f"last error: {last_error[:400]}. "
            "agents.retry.on_exhaustion = ABORT_FORMAL_RUN — the run must stop. "
            "Do NOT drop this case: a missing case shrinks the denominator, which "
            "is exactly what bounded retry exists to prevent (SRC-SAI §28 / FR-013)."
        )


# ---------------------------------------------------------------------------
# 一個 case 的完整四 agent 流程
# ---------------------------------------------------------------------------


def _strip_images(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != EVIDENCE_IMAGES_KEY}


def _image_digests(payload: Mapping[str, Any]) -> list[str]:
    return [str(i.get("sha256", "")) for i in payload.get(EVIDENCE_IMAGES_KEY, [])]


def case_cache_key(
    evidence: Mapping[str, Any],
    *,
    model_id: str,
    provider_revision: str,
    runtime_config_hash: str,
    schema_dir: Path = Path("schemas"),
) -> AgentCacheKey:
    """§48 的七要素。evidence_hash 涵蓋影像內容（以其 sha256 進入雜湊）。"""
    identity = {**_strip_images(evidence), "image_digests": _image_digests(evidence)}
    schema_hash = hash_object(
        {
            spec.schema_name: _load_schema(spec.schema_name, Path(schema_dir))
            for spec in AGENTS.values()
        }
    )
    return AgentCacheKey(
        evidence_hash=hash_object(identity),
        representation_mode=REPRESENTATION_MODE,
        provider_model_id=model_id,
        provider_revision=provider_revision or "-",
        prompt_hashes={code: spec.prompt_hash() for code, spec in AGENTS.items()},
        schema_hash=schema_hash,
        runtime_config_hash=runtime_config_hash,
    )


def run_pcmef_case(runner: AgentRunner, evidence: Mapping[str, Any]) -> AgentBundle:
    """跑完四個 agent，產出 cache 要的六份 artifact。

    順序是契約：observation 先給出中性摘要，兩位專家在**同一份**摘要上
    各自發言，arbitration 只看摘要與兩份匿名意見。專家之間不互相看見 ——
    否則第二位會被第一位錨定，兩份意見的一致度就不再是獨立證據。
    """
    images = list(evidence.get(EVIDENCE_IMAGES_KEY, []))
    body = _strip_images(evidence)
    usage = {"request_id": "", "tokens": 0, "latency": 0}

    def track(response: ProviderResponse) -> None:
        usage["request_id"] = usage["request_id"] or response.request_id
        usage["tokens"] += response.prompt_tokens + response.completion_tokens
        usage["latency"] += response.latency_ms

    observation, response = runner.run(
        AGENTS["observation_agent"], body, images=images
    )
    track(response)

    physics, response = runner.run(
        AGENTS["physics_agent"],
        {**body, "observation_brief": observation},
        images=[],
    )
    track(response)
    if physics.get("modality") != "physics":
        raise AgentError(
            f"physics_agent returned modality={physics.get('modality')!r}; "
            "the role contract fixes it to 'physics'"
        )

    visual, response = runner.run(
        AGENTS["visual_semantic_agent"],
        {**body, "observation_brief": observation},
        images=images,
    )
    track(response)
    if visual.get("modality") != "visual_semantic":
        raise AgentError(
            f"visual_semantic_agent returned modality={visual.get('modality')!r}; "
            "the role contract fixes it to 'visual_semantic'"
        )

    arbitration, response = runner.run(
        AGENTS["arbitration_agent"],
        {
            **body,
            "observation_brief": observation,
            # 匿名化：仲裁者看得到是哪個模態（schema 要求），但看不到
            # 任何 agent 身分或呼叫順序以外的線索。
            "anonymous_proposals": [physics, visual],
        },
        images=[],
    )
    track(response)

    return AgentBundle(
        artifacts={
            "observation_raw": observation,
            "observation_validated": observation,
            "physics_proposal": physics,
            "visual_proposal": visual,
            "arbitration_raw": arbitration,
            "arbitration_validated": arbitration,
        },
        provider_request_id=usage["request_id"],
        token_usage=int(usage["tokens"]),
        latency_ms=int(usage["latency"]),
    )


assert_registry_consistent()
