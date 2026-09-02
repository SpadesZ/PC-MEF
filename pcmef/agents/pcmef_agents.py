# PC-MEF Research System source maintenance contract
# 上下游: 匯入 pcmef.agents.provider（normalized adapter 契約）、
#         pcmef.agents.cache（六份 artifact 的 content-addressed 快取）、
#         pcmef.llm.capabilities（§45 task registry）。
#         被未來的 experiments.e2_formal 呼叫；本檔**不**讀取任何 dataset。
# 檔案路徑: pcmef/agents/pcmef_agents.py
# 產生時間: 2026-08-31 20:05 +08:00
# 版本: v0.2.0
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
#   5b. assert_support_is_usable() 在 schema 之後擋下全零 class_support
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
#   - 不得把全零 class_support 救成 25/25/25/25。均分是一個合法的分布，
#     會進 F(x)、會被 argmax 取走 CLASS_ORDER[0]、會進統計，而它實際
#     代表「仲裁者什麼都沒說」—— 下游無法區分兩者（NOTE-060）。
#   - v0.1.0 新增：首版四 agent 實作，決策見 NOTE-047。
#   - v0.2.0 新增：全零 class_support 改判 semantic failure（NOTE-060）。
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
# CLASS_ORDER 原本在本檔重新宣告一份字面值。那是漂移風險而不只是重複 ——
# agent_schema.lock 記載的 class_order 取自 pcmef.core.constants.CLASS_ORDER，
# 但 agent 實際用的是那一份；兩者若不一致，lock 會證明一個 agent 沒用過的
# 順序，而且不會有任何症狀（NOTE-052）。
from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.core.hash import hash_file, hash_object

__all__ = [
    "AgentError",
    "RetryExhaustedError",
    "EmptyClassSupport",
    "assert_support_is_usable",
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



#: 每份 prompt 都必須逐字出現的一句話。
#:
#: 這不是禮貌性的提醒 —— pilot 的 negative result 正是「把預測信心當成
#: 感測可靠度」造成的：溫度校準後 ToF 的 T = 0.0498，劣化的 ToF 因此
#: 高信心地錯，信心加權仲裁被拉向錯的那一邊（conflict 0.302，
#: 單看 Vision 反而 0.677）。
RELIABILITY_CLAUSE = "Predictive confidence is NOT sensor reliability"

#: prompt 檔的所在。與 pcmef/llm/snapshot.py 的 PROMPT_KEYS 指同一個目錄 ——
#: 那裡取雜湊寫進 llm_runtime.lock，這裡讀內容送給 provider。同一份檔案。
PROMPT_DIR = Path("configs/agents/prompts")

#: schema 違反時，第二次嘗試附上的更正指示。
#:
#: 刻意只談格式：evidence 的詮釋不得因為重試而改變，否則第二次拿到的
#: 就不是同一個問題的答案，而重試的前提是「同一個問題再問一次」。
SCHEMA_CORRECTION = (
    "Your previous response violated the required output schema. "
    "Correct the formatting without changing the evidence interpretation."
)

#: 取樣溫度。0 讓同一份證據盡可能得到同一個答案 —— 這是 formal run 的
#: 可重現性前提，不是品質偏好。它會進 runtime_config_hash，因此改動它
#: 等同改動 runtime identity。
FORMAL_TEMPERATURE = 0.0


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
    #: payload 中**不得**送進這個角色的鍵。角色隔離靠不送，不靠 prompt 拜託。
    withheld_keys: tuple[str, ...] = ()

    @property
    def prompt_path(self) -> Path:
        return PROMPT_DIR / f"{self.task_code}.md"

    @property
    def system_prompt(self) -> str:
        """從檔案讀，**不從 Python 常數讀**。

        freeze/llm_runtime.lock 的 prompt_hashes 是由 snapshot.py 對
        configs/agents/prompts/ 下的檔案取雜湊。prompt 若同時存在於
        Python 常數與檔案，凍結記錄的就會是「檔案的雜湊」而執行送出的是
        「常數的內容」—— 兩者不一致，而且不會有任何症狀。
        單一來源就是這些檔案。
        """
        path = self.prompt_path
        if not path.exists():
            raise AgentError(
                f"prompt file for {self.task_code} not found at {path}. "
                "Prompts are frozen artifacts hashed into llm_runtime.lock; "
                "they must not be reintroduced as Python constants."
            )
        return path.read_text(encoding="utf-8").strip()

    def prompt_hash(self) -> str:
        """與 snapshot.py 的 _hash_files 對齊：同一個檔案、同一個雜湊函式。

        兩邊都用 hash_file，因此 cache key 裡的 prompt 雜湊與
        llm_runtime.lock 裡的 prompt_hashes 對得起來 —— 這是可稽核性的前提，
        不是巧合。
        """
        return hash_file(self.prompt_path)


AGENTS: dict[str, AgentSpec] = {
    spec.task_code: spec
    for spec in (
        AgentSpec(
            task_code="observation_agent",
            schema_name="observation_brief_v1",
            needs_image=True,
            artifact_names=("observation_raw", "observation_validated"),
            withheld_keys=("class_probabilities",),
        ),
        # 兩位專家的角色隔離**靠不送，不靠 prompt 拜託**。
        #
        # prompt 寫著「You are NOT allowed to infer from the RGB image」，
        # 卻把 p_vision 與 q_vision 一起遞過去，等於一邊叫它別看一邊把東西
        # 放在它面前 —— 那正是 Observation Agent 不做分類所要避開的
        # anchoring，只是換了位置。沒收到就是事實，收到了才需要承諾。
        AgentSpec(
            task_code="physics_agent",
            schema_name="specialist_proposal_v1",
            needs_image=False,
            artifact_names=("physics_proposal", "physics_proposal"),
            withheld_keys=("vision",),
        ),
        AgentSpec(
            task_code="visual_semantic_agent",
            schema_name="specialist_proposal_v1",
            needs_image=True,
            artifact_names=("visual_proposal", "visual_proposal"),
            withheld_keys=("tof",),
        ),
        AgentSpec(
            task_code="arbitration_agent",
            schema_name="arbitration_output_v1",
            needs_image=False,
            artifact_names=("arbitration_raw", "arbitration_validated"),
        ),
    )
}


#: class_support 總和的容許區間。
#:
#: JSON Schema **沒有跨欄位算術約束**，所以「四個數字加起來等於 100」
#: 驗證不了。硬在程式裡要求精確 100 的代價不對稱得離譜：retry 只有 2 次，
#: 耗盡即 ABORT_FORMAL_RUN，而 families 36-43 只能跑一次 ——
#: 整場實驗會因為某個 case 寫了 99 而中止。
#: 因此接受容差後正規化：那是確定性的、可記錄的轉換。
SUPPORT_SUM_TOLERANCE = 5.0


class EmptyClassSupport(AgentError):
    """Arbitration 回了一份總證據量為 0 的 class_support。

    與一般 AgentError 分開命名，是因為它的成因與處置都不同：這不是格式
    問題，而是**仲裁者沒有給出任何判斷**。它照樣通過 JSON Schema
    （四個欄位都在，`minimum: 0` 允許 0），所以只有語意層攔得住。
    """


def normalise_class_support(support: Mapping[str, float]) -> dict[str, float]:
    """把 class_support 正規化成總和 100。

    回傳新的 dict，不改動輸入。總和落在 100 ± SUPPORT_SUM_TOLERANCE 之外時
    仍然正規化，但呼叫端會把原始總和記進 artifact —— 偏離太多是模型沒有
    遵守指示的證據，不該被正規化悄悄抹平。

    **總和為 0 一律 raise，不得救成 uniform。** 25/25/25/25 是一個合法的
    分布：它會進 F(x)、會被 argmax 取走 CLASS_ORDER[0]、會進統計，而它
    實際代表的是「仲裁者什麼都沒說」。兩者在下游無法區分，於是一次
    semantic failure 會偽裝成一個「四類等可能」的判斷。

    `core.numeric.normalize_support()` 早就拒絕全零
    （SRC-SAI FR-024：「不得靠 EPS_S 轉成 uniform 再繼續」），但那道防線
    先前永遠觸發不到 —— 本函式在上游就把證據抹掉了。core 的 EPS_S 是拿來
    穩定**合法的非零** support 的，不是拿來救全零的（NOTE-060）。
    """
    values = {name: float(support.get(name, 0.0)) for name in CLASS_ORDER}
    total = sum(values.values())
    if total <= 0:
        raise EmptyClassSupport(
            f"class_support sums to {total}; a zero total means the arbiter "
            "produced no judgement at all. Refusing to spread it evenly: that "
            "would disguise a semantic failure as a four-way tie, and nothing "
            "downstream could tell the difference."
        )
    return {name: round(value * 100.0 / total, 6) for name, value in values.items()}


def assert_support_is_usable(spec: AgentSpec, parsed: Mapping[str, Any]) -> None:
    """schema 之外的語意檢查：帶 class_support 的角色不得回全零。

    JSON Schema 管得了「四個鍵都在、每個都是 0..100 的數」，管不了
    「加起來要大於 0」。這道檢查放在 `AgentRunner.run` 的 schema 驗證之後，
    因此 raise 出來的 `AgentError` 會被既有的 retry 分支接住 ——
    重試一次，附上更正指示；耗盡則 `RetryExhaustedError` 一路上拋成
    `ABORT_FORMAL_RUN`，而不是 drop 這個 case。
    """
    support = parsed.get("class_support")
    if support is None:
        return
    if not isinstance(support, Mapping):
        raise AgentError(
            f"{spec.task_code} returned class_support of type "
            f"{type(support).__name__}; expected an object keyed by class"
        )
    total = sum(float(support.get(name, 0.0)) for name in CLASS_ORDER)
    if total <= 0:
        raise EmptyClassSupport(
            f"{spec.task_code} returned a class_support summing to {total}. "
            "Every class carries zero evidence, which is schema-valid but "
            "means no judgement was made. Retrying rather than treating it as "
            "a uniform distribution."
        )


def assert_prompts_state_the_rule() -> None:
    """四份 prompt 都必須逐字帶著那句話，且必須真的存在。

    這句話是 pilot 那個 negative result 的直接對策。它若在某次編輯中掉了，
    不會有任何症狀 —— 模型照樣回得出 schema-valid 的東西，只是可能又把
    信心當成可靠度。所以用測試盯著，不靠人記得。
    """
    for task_code, spec in AGENTS.items():
        text = spec.system_prompt
        if RELIABILITY_CLAUSE.lower() not in text.lower():
            raise AgentError(
                f"{task_code} prompt no longer states {RELIABILITY_CLAUSE!r}; "
                "that sentence is the direct countermeasure to the pilot's "
                "negative result and must not be dropped"
            )


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
    """**已廢止（NOTE-052）。** 跨 channel 攤平的 ToF 摘要。

    保留原因只有一個：v1 vs v2 的 regression 比較需要能重算 v1 的值。
    **不得**在任何新的 payload 路徑使用；正式路徑一律用
    `tof_fixed_summary_v2_channel_preserving()`。

    廢止理由見 v2 的 docstring：攤平之後四個不同單位的 channel 被當成
    同一個量的連續取樣，peak/centroid/spread 因此不對應任何可量測的東西。

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


#: v2 摘要的版本識別。它進 runtime_config_hash，因此改它等同改 runtime identity。
TOF_SUMMARY_VERSION = "tof_fixed_summary_v2_channel_preserving"

#: 每個 channel 固定回報的六個統計量。順序固定，長度固定。
TOF_CHANNEL_STATISTICS: tuple[str, ...] = (
    "mean", "std", "median", "p10", "p90", "temporal_diff_std",
)


def tof_fixed_summary_v2_channel_preserving(tof: np.ndarray) -> dict[str, Any]:
    """(n, 4) ToF recording 的 **channel-preserving** 固定欄位摘要。

    v1（`tof_fixed_summary`）把 500x4 攤平成一條 2000 點「波形」再取
    peak / centroid / spread。那是錯的，而且錯得不明顯：攤平之後
    `distance_mm`（毫米）、`ambient_rate_mcps` 與 `signal_rate_mcps`
    （每秒百萬計數）、`sigma_like`（無因次寬度）被當成同一個量的連續取樣。
    argmax 落在哪裡完全由**單位最大的那個 channel** 決定，質心則是四個
    不同物理量的加權平均 —— 它不對應任何可量測的東西。v1 因此正式廢止。

    v2 固定保留四個 channel 各自獨立摘要：欄位集合與順序固定
    （§18 FIXED_SUMMARY 要求逐 case 等長），channel 名稱與單位語意原樣保留，
    且**不含**任何 GT / condition / family metadata。
    """
    array = np.asarray(tof, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != len(TOF_SCHEMA):
        raise AgentError(
            f"ToF evidence must be (n, {len(TOF_SCHEMA)}) with channels "
            f"{list(TOF_SCHEMA)}, got shape {array.shape}. v2 refuses a flattened "
            "waveform: mixing channels was exactly the v1 defect."
        )
    if array.shape[0] < 2:
        raise AgentError(
            f"ToF evidence has {array.shape[0]} sample(s); temporal statistics "
            "need at least 2"
        )

    channels: dict[str, dict[str, float]] = {}
    for index, name in enumerate(TOF_SCHEMA):
        column = array[:, index]
        # temporal_diff_std 是唯一對時間順序敏感的量。分開命名而不是混進
        # std，讓「打亂時間順序只會動到它」成為可測試的性質。
        channels[name] = {
            "mean": round(float(np.mean(column)), 6),
            "std": round(float(np.std(column)), 6),
            "median": round(float(np.median(column)), 6),
            "p10": round(float(np.percentile(column, 10.0)), 6),
            "p90": round(float(np.percentile(column, 90.0)), 6),
            "temporal_diff_std": round(float(np.std(np.diff(column))), 6),
        }

    signal = array[:, TOF_SCHEMA.index("signal_rate_mcps")]
    ambient = array[:, TOF_SCHEMA.index("ambient_rate_mcps")]
    distance = array[:, TOF_SCHEMA.index("distance_mm")]
    # 以 median 相除而不是 mean：兩者都是重尾的計數率，median 對單一尖峰
    # 不敏感，而 gate 的 Q_tof 也是用 median 定義的（同一個讀法）。
    ratio = float(np.median(signal)) / max(float(np.median(ambient)), 1e-12)
    valid = np.isfinite(array).all(axis=1) & (distance > 0.0)

    return {
        "summary_version": TOF_SUMMARY_VERSION,
        "channel_order": list(TOF_SCHEMA),
        "n_samples": int(array.shape[0]),
        "channels": channels,
        "derived": {
            "signal_to_ambient_ratio": round(ratio, 6),
            "valid_sample_ratio": round(float(valid.mean()), 6),
            "_meaning": (
                "signal_to_ambient_ratio is median(signal_rate_mcps) / "
                "median(ambient_rate_mcps); valid_sample_ratio is the fraction "
                "of samples whose four channels are all finite and whose "
                "distance_mm is positive."
            ),
        },
        "_meaning": (
            "Per-channel statistics of a multi-channel ToF recording. The four "
            "channels are different physical quantities in different units and "
            "are never combined: there is no cross-channel peak, centroid or "
            "spread. temporal_diff_std is the only order-sensitive statistic."
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
    rgb: np.ndarray | None = None,
    tof: np.ndarray,
    image: Mapping[str, str] | None = None,
    p_vision: Sequence[float],
    p_tof: Sequence[float],
    q_vision: float,
    q_tof: float,
    duq: Mapping[str, float],
) -> dict[str, Any]:
    """組成一個 case 的 FIXED_SUMMARY 證據包。

    刻意分成三塊並在 payload 裡明說它們的語意不同：
      * calibrated_class_probabilities —— p_m(y|x)，會不會答對的信心
      * modality_reliability —— q_m，感測器有沒有在正常工作
    這兩者在 pilot 被證實是**不同的東西**；payload 若只給一個數字，
    模型沒有辦法不把它們混為一談。
    """
    if (rgb is None) == (image is None):
        raise AgentError(
            "supply exactly one of rgb (an array to encode) or image "
            "(an already-encoded entry); supplying both makes it ambiguous "
            "which one actually reached the provider"
        )
    number = lambda v: round(float(v), 6)  # noqa: E731
    cues = {k: number(v) for k, v in duq.items()}
    return {
        "schema_version": "1.0",
        "representation_mode": REPRESENTATION_MODE,
        "evidence_contract_version": ROLE_EVIDENCE_CONTRACT_VERSION,
        "class_order": list(CLASS_ORDER),
        "tof_summary": tof_fixed_summary_v2_channel_preserving(tof),
        # Q 與 D/U 拆開：Q 只由**輸入本身**算得出（銳利度 / SNR），
        # 是 Observation Agent 可以看的原始感測品質；D/U 由模型輸出導出，
        # 屬於已解讀的量，只給需要它們的角色。
        "sensing_quality_cues": {
            # _meaning 一律**不指名另一個模態**。它會跟著 modality_scoped 的
            # 欄位一起送到 specialist 手上，若在這裡寫「Q_vision 是影像的
            # 高頻能量」，physics agent 就從一句說明得知了 vision 側的存在與
            # 讀法 —— 那是同一種 anchoring，只是換成散文形式。
            # 各模態的具體讀法寫在該角色自己的 prompt 裡。
            "_meaning": (
                "raw observable sensing quality for the modality shown below, "
                "computed from that sensor's input alone and independent of any "
                "classifier output. Higher means a cleaner input."
            ),
            "Q_vision": cues.get("Q_vision"),
            "Q_tof": cues.get("Q_tof"),
        },
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
            "q_vision": number(q_vision),
            "q_tof": number(q_tof),
            "evidence_sources": [
                "sensor_quality", "degradation_margin", "cross_modal_support",
            ],
            "forbidden_sources": [
                "max softmax probability",
                "temperature-scaled confidence",
                "predictive entropy",
            ],
        },
        "predictive_entropy": {
            "_meaning": (
                "the normalised predictive entropy of the modality's own "
                "classifier, shown below. It is a property of p(y|x) and is NOT "
                "sensor reliability."
            ),
            "U_vision": cues.get("U_vision"),
            "U_tof": cues.get("U_tof"),
        },
        "cross_modal": {
            "_meaning": (
                "D is the total-variation distance between the two modalities' "
                "class probabilities. It is symmetric and belongs to neither "
                "modality, so it goes only to the arbiter."
            ),
            "D": cues.get("D"),
        },
        # gate_route 刻意**不再**放進 evidence bundle（NOTE-052）。
        # 它與 condition 高度相關（degraded 的 case 才會 trust_*），
        # 等於把「這一筆被劣化過」告訴模型，而那是 benchmark metadata。
        # 路由由系統決定，agent 不需要知道自己是怎麼被叫來的。
        "route_withheld_from_all_roles": True,
        # image 允許傳入**已編碼**的影像：real validation 把 perception 放在
        # 主機（需要 torch）、把 provider 呼叫放在容器（需要 vault），兩段之間
        # 傳的是 PNG bytes 而不是 EXR 路徑。編碼結果與 encode_image_evidence
        # 相同，因此 evidence_hash 不受影響。
        EVIDENCE_IMAGES_KEY: [
            dict(image) if image is not None else encode_image_evidence(rgb)
        ],
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
    temperature: float = FORMAL_TEMPERATURE
    attempts: list[AttemptLog] = field(default_factory=list)
    #: 逐角色的 (adapter, connection, model)。用於把四個角色分散到多把
    #: API key —— 同一個 model_id + provider_revision，不同 connection。
    #: 空的時候四個角色共用上面那一組。
    #:
    #: 這**不是**多模型：模型與版本必須相同，否則四個角色就不是同一個
    #: 推論器，而 llm_runtime.lock 記的 model identity 會對應不到執行的東西。
    #: 一致性由 assert_single_model_identity() 檢查，不靠呼叫端自律。
    role_bindings: dict[str, tuple[Any, Any, Any]] = field(default_factory=dict)

    def binding_for(self, task_code: str) -> tuple[Any, Any, Any]:
        """回傳這個角色實際使用的 (adapter, connection, model)。"""
        if task_code in self.role_bindings:
            return self.role_bindings[task_code]
        return self.adapter, self.connection, self.model

    def assert_single_model_identity(self) -> None:
        """四個角色的 model_id 與 provider_revision 必須完全相同。

        分散到多把 key 是**吞吐**決策；換模型是**科學**決策。
        這道檢查讓前者不會悄悄變成後者。
        """
        identities = {
            code: (
                getattr(self.binding_for(code)[2], "model_id", None),
                getattr(self.binding_for(code)[2], "provider_revision", None),
            )
            for code in AGENTS
        }
        distinct = set(identities.values())
        if len(distinct) > 1:
            raise AgentError(
                "the four roles must share one model identity (model_id and "
                f"provider_revision); got {identities}. Splitting roles across "
                "API keys is allowed for throughput, but splitting them across "
                "models would mean llm_runtime.lock names an inference engine "
                "that never ran."
            )

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
        runtime_cfg = {
            "response_schema": schema,
            "task_code": spec.task_code,
            "temperature": self.temperature,
        }

        last_error = ""
        correction: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            # 只有「上一次違反 schema」才附更正指示。transport 失敗時請求
            # 本身沒有任何問題，改動它反而引入一個新的變因。
            outgoing = dict(request)
            if correction:
                outgoing["format_correction"] = correction
            try:
                adapter, connection, model = self.binding_for(spec.task_code)
                response = adapter.invoke(
                    connection, model, spec.task_code, outgoing, runtime_cfg
                )
                parsed = _extract_json(response.text)
                jsonschema.validate(parsed, schema)
                # schema 之外的語意檢查。全零 class_support 是 schema-valid
                # 的，但它代表「沒有判斷」—— 必須重試，不得當成均勻分布。
                assert_support_is_usable(spec, parsed)
            except ProviderError as error:
                # 傳輸/HTTP 失敗。原封不動重送 —— 這類失敗多半是暫態，
                # 若不重試，一次網路抖動就會終止整場 formal run。
                last_error = f"{type(error).__name__}: {error}"
                correction = None
                self.attempts.append(
                    AttemptLog(spec.task_code, attempt, ok=False, error=last_error[:400])
                )
                continue
            except (AgentError, jsonschema.ValidationError) as error:
                last_error = f"{type(error).__name__}: {error}"
                correction = SCHEMA_CORRECTION
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


#: 一個模態的證據散落在哪些欄位。withhold() 用它把整個模態拿掉。
_MODALITY_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    # Observation Agent 不做分類，因此也不該看到分類器的輸出。
    # 它的 prompt 寫「If model probabilities are accidentally present in the
    # input, do not use them」—— 最忠實的實作是根本不送，而不是送了再叫它
    # 別看。這同時擋掉一條 anchoring 路徑：看得到 p(y|x) 的話，它的
    # 「觀察」很容易被機率最高的那一類染色。
    "class_probabilities": (
        ("calibrated_class_probabilities",),
    ),
    "vision": (
        ("calibrated_class_probabilities", "vision"),
        ("modality_reliability", "q_vision"),
        ("duq_signals", "Q_vision"),
        ("duq_signals", "U_vision"),
    ),
    "tof": (
        ("tof_summary",),
        ("calibrated_class_probabilities", "tof"),
        ("modality_reliability", "q_tof"),
        ("duq_signals", "Q_tof"),
        ("duq_signals", "U_tof"),
    ),
}


def withhold(spec: AgentSpec, payload: Mapping[str, Any]) -> dict[str, Any]:
    """**已由 `project_for_role()` 取代（NOTE-052）。**

    保留為 v1 相容路徑，供 regression 比較重建舊 payload 之用；
    正式路徑不得再呼叫。

    舊做法是**減法**：從完整 payload 移除該角色不該看的欄位。減法的問題
    在於它對「新增的欄位」預設放行 —— 之後任何人往 evidence bundle 加一個
    欄位，四個角色全部自動看得到，而且不會有任何測試失敗。
    v2 改為加法（正向白名單），新欄位預設不送。
    """
    if not spec.withheld_keys:
        return dict(payload)

    result = json.loads(json.dumps(payload, ensure_ascii=False))  # 深拷貝
    removed: list[str] = []
    for modality in spec.withheld_keys:
        for path in _MODALITY_FIELDS.get(modality, ()):
            node = result
            for part in path[:-1]:
                node = node.get(part) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, dict) and path[-1] in node:
                node.pop(path[-1])
                removed.append(".".join(path))
    result["withheld_from_this_role"] = sorted(removed)
    return result


#: 角色證據契約的版本識別。進 runtime_config_hash。
ROLE_EVIDENCE_CONTRACT_VERSION = "role_evidence_contract_v2"

#: 每個角色**可以收到**的欄位。正向白名單，不是黑名單。
#:
#: 設計原則不是「每個 agent 資訊越少越好」，而是只移除兩件事：
#:   1. 角色重複 —— specialists 已經解讀過的 raw evidence 不再送給 arbiter
#:   2. 跨模態 anchoring —— 專家不看另一個模態的任何量，兩份意見才是獨立證據
#:
#: `predictive confidence is NOT sensor reliability` 由結構維持：
#: p_m 與 q_m 永遠分屬不同區塊且各自帶 _meaning，任何角色拿到其中一個
#: 都同時拿到另一個的語意說明。
#:
#: `gate_route` 不在任何角色的白名單內：它與 condition 高度相關，
#: 送出去等於洩漏 benchmark metadata。
ROLE_EVIDENCE_CONTRACT_V2: dict[str, dict[str, Any]] = {
    "observation_agent": {
        "receives_image": True,
        "common": ("schema_version", "representation_mode",
                   "evidence_contract_version", "class_order"),
        "sections": ("tof_summary", "sensing_quality_cues"),
        "modality_scoped": (),
        "rationale": (
            "factual observation only. It does not classify, so it receives no "
            "classifier output at all: no p_m, no q_m, no D, no U. Q_* is raw "
            "sensing quality computed from the input itself, which is an "
            "observation rather than an interpretation."
        ),
    },
    "physics_agent": {
        "receives_image": False,
        "common": ("schema_version", "representation_mode",
                   "evidence_contract_version", "class_order"),
        "sections": ("tof_summary", "observation_brief"),
        # (section, key) 只保留該模態那一側。
        "modality_scoped": (
            ("calibrated_class_probabilities", "tof"),
            ("modality_reliability", "q_tof"),
            ("sensing_quality_cues", "Q_tof"),
            ("predictive_entropy", "U_tof"),
        ),
        "rationale": (
            "the ToF specialist. It sees its own modality's evidence and its own "
            "numerics, and nothing from vision -- otherwise its agreement with "
            "the visual specialist stops being independent evidence. D is "
            "withheld because D is a cross-modal quantity and belongs to the "
            "arbiter."
        ),
    },
    "visual_semantic_agent": {
        "receives_image": True,
        "common": ("schema_version", "representation_mode",
                   "evidence_contract_version", "class_order"),
        "sections": ("observation_brief",),
        "modality_scoped": (
            ("calibrated_class_probabilities", "vision"),
            ("modality_reliability", "q_vision"),
            ("sensing_quality_cues", "Q_vision"),
            ("predictive_entropy", "U_vision"),
        ),
        "rationale": (
            "the vision specialist, mirror image of the physics role. It gets "
            "the image and its own numerics; no ToF summary, no ToF numerics, "
            "no D."
        ),
    },
    "arbitration_agent": {
        "receives_image": False,
        "common": ("schema_version", "representation_mode",
                   "evidence_contract_version", "class_order"),
        "sections": ("observation_brief", "anonymous_proposals",
                     "calibrated_class_probabilities", "modality_reliability",
                     "predictive_entropy", "cross_modal", "sensing_quality_cues"),
        "modality_scoped": (),
        "rationale": (
            "final evidence arbitration. It keeps the full numerical picture "
            "including D, because deciding between two proposals is exactly what "
            "those numbers are for. It does NOT get the raw image or the raw ToF "
            "summary: both specialists have already read them, and re-sending "
            "raw evidence would duplicate their role rather than arbitrate it."
        ),
    },
}


def project_for_role(
    task_code: str, evidence: Mapping[str, Any], extras: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """依 `role_evidence_contract_v2` 組出一個角色實際收到的 payload。

    正向選取（加法），不是移除（減法）。未列在白名單的欄位一律不送，
    因此之後往 evidence bundle 加欄位不會自動洩漏給四個角色。
    """
    contract = ROLE_EVIDENCE_CONTRACT_V2.get(task_code)
    if contract is None:
        raise AgentError(
            f"no evidence contract for role {task_code!r}; "
            f"known roles: {sorted(ROLE_EVIDENCE_CONTRACT_V2)}"
        )
    source: dict[str, Any] = {**evidence, **(extras or {})}
    payload: dict[str, Any] = {}

    for key in contract["common"]:
        if key in source:
            payload[key] = source[key]
    for key in contract["sections"]:
        if key in source:
            payload[key] = source[key]

    for section, field_name in contract["modality_scoped"]:
        block = source.get(section)
        if not isinstance(block, dict) or field_name not in block:
            continue
        kept = payload.setdefault(section, {})
        # _meaning 一併帶上：那是 p_m 與 q_m 語意區分的載體，
        # 少了它角色就只拿到一個沒有說明的數字。
        if "_meaning" in block:
            kept["_meaning"] = block["_meaning"]
        kept[field_name] = block[field_name]

    payload["evidence_contract_version"] = ROLE_EVIDENCE_CONTRACT_VERSION
    payload["role"] = task_code
    return payload


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
        AGENTS["observation_agent"],
        project_for_role("observation_agent", body),
        images=images,
    )
    track(response)

    physics, response = runner.run(
        AGENTS["physics_agent"],
        project_for_role(
            "physics_agent", body, {"observation_brief": observation}
        ),
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
        project_for_role(
            "visual_semantic_agent", body, {"observation_brief": observation}
        ),
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
        project_for_role(
            "arbitration_agent", body,
            {
                "observation_brief": observation,
                # 匿名化：仲裁者看得到是哪個模態（schema 要求），但看不到
                # 任何 agent 身分或呼叫順序以外的線索。
                "anonymous_proposals": [physics, visual],
            },
        ),
        images=[],
    )
    track(response)

    # raw 保留模型原話，validated 是正規化後的版本。兩份都留：
    # 原始總和偏離 100 多少，是模型有沒有遵守指示的證據，
    # 不該被正規化悄悄抹平。
    raw_support = dict(arbitration.get("class_support") or {})
    raw_sum = sum(float(v) for v in raw_support.values())
    arbitration_validated = {
        **arbitration,
        "class_support": normalise_class_support(raw_support),
        "support_sum_before_normalisation": round(raw_sum, 6),
        "support_sum_within_tolerance": abs(raw_sum - 100.0) <= SUPPORT_SUM_TOLERANCE,
    }

    return AgentBundle(
        artifacts={
            "observation_raw": observation,
            "observation_validated": observation,
            "physics_proposal": physics,
            "visual_proposal": visual,
            "arbitration_raw": arbitration,
            "arbitration_validated": arbitration_validated,
        },
        provider_request_id=usage["request_id"],
        token_usage=int(usage["tokens"]),
        latency_ms=int(usage["latency"]),
    )


assert_registry_consistent()
