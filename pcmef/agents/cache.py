# PC-MEF Research System source maintenance contract
# 上下游: 由未來的 experiments.e2_formal 與 agents.observation/physics/
#         visual_semantic/arbitration 呼叫；讀寫 artifacts/agents/<cache_key>/
#         下的六份 JSON 與 manifest；命中與費用索引寫進 pcmef.llm.registry
#         的 llm_cache_index。cache key 的組成來自 freeze/llm_runtime.lock。
# 檔案路徑: pcmef/agents/cache.py
# 產生時間: 2026-08-27 04:10 +08:00
# 版本: v0.1.0
# 功能說明: 同一個場景在多組 checkpoint pair 之間只問一次外部模型。
#           把「問什麼」的所有要素雜湊成一把鑰匙，鑰匙一樣就直接沿用上次的回答，
#           任何一項變了就必須重問。
# 模組定位: §48 content-addressed agent artifact cache 的實作，
#           也是 FR-031 「resume 不得重呼叫 provider」的強制點。
#           它「不是」通用快取 —— 只收 Agent 階段的產物，且刻意與 case 身分無關。
# 主要責任:
#   1. AgentCacheKey 依 §48 的七個要素組成，並在建構時擋下不該入鍵的欄位
#   2. AgentCacheKey.digest() 以固定順序串接，順序本身是契約的一部分
#   3. AgentArtifactCache.get() 只在六份 artifact 與 manifest 齊全時算命中
#   4. AgentArtifactCache.resolve() 命中就絕不呼叫 producer（LLM-RESUME-01）
#   5. AgentArtifactCache.put() 對既有內容冪等，內容不同即拒絕覆寫
#   6. assert_pair_independent() 擋下把 checkpoint-pair 專屬量寫進快取
# 維護提醒:
#   - 不得把 training_pair_id、checkpoint 或 case 身分放進 cache key。§48 明訂
#     Agent 路徑只看 evidence 與 frozen Agent config；放進去會讓同一份證據在
#     每組 pair 各問一次，s_A 因此變成 pair 的函數，G5 對 G4 的比較失去意義。
#   - 不得把 p_T / p_V / r_T / r_V / D / U / Q / g / F 或預測類別寫進快取
#     （Appendix J2 右欄）；那些必須每組 pair 各自重算。
#   - 不得在 resume 時「順便重問一次確認」；FR-031 與 LLM-RESUME-01 要求
#     已完成的 formal case 只讀 frozen response，provider call 次數不得增加。
#   - 不得覆寫既有 artifact；內容不同代表輸入變了，那是另一把鑰匙。
#   - v0.1.0 新增：首版 content-addressed cache，決策見 NOTE-021。
# 驗證方式:
#   - py -3.10 -m pytest tests/cache/test_agent_cache.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from pcmef.core.hash import combine_hashes, hash_object

__all__ = [
    "CacheError",
    "AGENT_ARTIFACT_NAMES",
    "PAIR_SPECIFIC_FIELDS",
    "AgentCacheKey",
    "AgentBundle",
    "AgentArtifactCache",
    "DEFAULT_CACHE_ROOT",
    "assert_pair_independent",
]

DEFAULT_CACHE_ROOT = Path("artifacts/agents")

#: §48 Cache Contract 1 的六份 artifact，順序即目錄樹的順序。
AGENT_ARTIFACT_NAMES: tuple[str, ...] = (
    "observation_raw",
    "observation_validated",
    "physics_proposal",
    "visual_proposal",
    "arbitration_raw",
    "arbitration_validated",
)

#: Appendix J2 右欄：不可跨 checkpoint pair 共用的量。
#: 它們一旦進了共用快取，就等於宣稱不同 pair 會得到相同的 p_rel 與 F。
PAIR_SPECIFIC_FIELDS: frozenset[str] = frozenset(
    {
        "p_t", "p_v", "p_T", "p_V",
        "r_t", "r_v", "r_T", "r_V",
        "p_rel", "d", "u", "q", "g",
        "f_pcmef", "f_pc_mef", "predicted_class",
        "training_pair_id", "checkpoint_pair_id", "checkpoint_id",
    }
)

#: 不得參與 cache key 的欄位名。case 身分與 pair 身分都在此列。
_FORBIDDEN_KEY_PARTS: frozenset[str] = frozenset(
    {
        "case_id", "opaque_case_id", "canonical_case_id",
        "training_pair_id", "checkpoint_pair_id", "checkpoint_id",
        "class_label", "condition", "severity", "parent_scene_family",
    }
)


class CacheError(RuntimeError):
    """cache key 組成非法、artifact 不完整，或試圖以不同內容覆寫。"""


def assert_pair_independent(payload: Any, location: str = "artifact") -> None:
    """遞迴確認 payload 不含任何 checkpoint-pair 專屬量。

    Appendix J2 把可共用與不可共用的量分成兩欄。可共用的那一欄是「同一份
    evidence 加同一份 frozen Agent config 必然得到同一個答案」的量；
    不可共用的那一欄取決於是哪一組 checkpoint。把後者寫進共用快取，
    等於讓第二組 pair 讀到第一組的數字，而且不會有任何症狀。
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in {f.lower() for f in PAIR_SPECIFIC_FIELDS}:
                raise CacheError(
                    f"{location}.{key} is checkpoint-pair specific and must not enter "
                    "the shared agent cache (SRC-SAI Appendix J2); recompute it per "
                    "pair instead"
                )
            assert_pair_independent(value, f"{location}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_pair_independent(item, f"{location}[{index}]")


@dataclass(frozen=True)
class AgentCacheKey:
    """§48 的 agent_cache_key 七要素。"""

    evidence_hash: str
    representation_mode: str
    provider_model_id: str
    provider_revision: str
    prompt_hashes: Mapping[str, str]
    schema_hash: str
    runtime_config_hash: str

    def __post_init__(self) -> None:
        for name in (
            "evidence_hash", "representation_mode", "provider_model_id",
            "schema_hash", "runtime_config_hash",
        ):
            if not getattr(self, name):
                raise CacheError(f"agent cache key component {name!r} must not be empty")
        if not self.prompt_hashes:
            raise CacheError("agent cache key needs at least one prompt hash")
        for key in self.prompt_hashes:
            if str(key).lower() in _FORBIDDEN_KEY_PARTS:
                raise CacheError(
                    f"prompt hash key {key!r} names a case or checkpoint attribute; "
                    "the cache key is content-addressed and must stay independent of "
                    "case identity (SRC-SAI §48, NOTES.md NOTE-004)"
                )

    def components(self) -> dict[str, Any]:
        return {
            "evidence_hash": self.evidence_hash,
            "representation_mode": self.representation_mode,
            "provider_model_id": self.provider_model_id,
            "provider_revision": self.provider_revision,
            "prompt_hashes": dict(sorted(self.prompt_hashes.items())),
            "schema_hash": self.schema_hash,
            "runtime_config_hash": self.runtime_config_hash,
        }

    def digest(self) -> str:
        """依 §48 給定的順序串接後取 SHA-256。

        用 combine_hashes 而非把整個 dict 丟去 hash_object：§48 寫的是
        有序串接，而順序本身是契約 —— 兩個不同的 key 若只是欄位換位置
        就得到相同雜湊，cache 會誤命中。
        """
        return combine_hashes(
            self.evidence_hash,
            self.representation_mode,
            self.provider_model_id,
            self.provider_revision or "-",
            hash_object(dict(sorted(self.prompt_hashes.items()))),
            self.schema_hash,
            self.runtime_config_hash,
        )


@dataclass(frozen=True)
class AgentBundle:
    """一次完整 Agent 流程的六份產物。"""

    artifacts: Mapping[str, Any]
    provider_request_id: str = ""
    token_usage: int = 0
    latency_ms: int = 0

    def __post_init__(self) -> None:
        missing = [name for name in AGENT_ARTIFACT_NAMES if name not in self.artifacts]
        if missing:
            raise CacheError(
                f"agent bundle is missing artifact(s) {missing}; §48 requires all six "
                "so that a cache hit can never serve a half-finished run"
            )
        assert_pair_independent(dict(self.artifacts))


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    provider_calls: int = 0


class AgentArtifactCache:
    """content-addressed 的 Agent 產物快取。"""

    def __init__(
        self,
        root: str | Path = DEFAULT_CACHE_ROOT,
        registry=None,
    ) -> None:
        """registry 為選用的 LLMRegistry，用來維護 llm_cache_index（費用稽核）。"""
        self.root = Path(root)
        self.registry = registry
        self.stats = CacheStats()

    # -- 路徑 -------------------------------------------------------------

    def path_for(self, key: AgentCacheKey) -> Path:
        return self.root / key.digest()

    # -- 讀取 -------------------------------------------------------------

    def get(self, key: AgentCacheKey) -> AgentBundle | None:
        """§48：cache hit iff ALL hashes match。六份 artifact 缺一即視為未命中。"""
        folder = self.path_for(key)
        manifest_path = folder / "manifest.json"
        if not manifest_path.exists():
            return None
        paths = {name: folder / f"{name}.json" for name in AGENT_ARTIFACT_NAMES}
        if not all(path.exists() for path in paths.values()):
            # 半套的目錄不算命中：沿用它會讓一次中斷的 run 悄悄變成完整結果。
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("cache_key") != key.digest():
            return None
        return AgentBundle(
            artifacts={
                name: json.loads(path.read_text(encoding="utf-8"))
                for name, path in paths.items()
            },
            provider_request_id=str(manifest.get("provider_request_id", "")),
            token_usage=int(manifest.get("token_usage", 0)),
            latency_ms=int(manifest.get("latency_ms", 0)),
        )

    def resolve(
        self, key: AgentCacheKey, producer: Callable[[], AgentBundle]
    ) -> tuple[AgentBundle, bool]:
        """命中則回傳既有產物，未命中才呼叫 producer。回傳 (bundle, hit)。

        producer 是唯一會真的呼叫 provider 的路徑，而它只在未命中時被呼叫 ——
        這就是 LLM-CACHE-01 與 LLM-RESUME-01 的結構性保證：不是靠呼叫端
        記得先查快取，而是查快取的人就是唯一有能力發問的人。
        """
        cached = self.get(key)
        if cached is not None:
            self.stats.hits += 1
            return cached, True
        self.stats.misses += 1
        self.stats.provider_calls += 1
        bundle = producer()
        self.put(key, bundle)
        return bundle, False

    # -- 寫入 -------------------------------------------------------------

    def put(self, key: AgentCacheKey, bundle: AgentBundle) -> Path:
        """落盤六份 artifact 與 manifest。相同內容冪等，不同內容拒絕。"""
        folder = self.path_for(key)
        folder.mkdir(parents=True, exist_ok=True)

        for name in AGENT_ARTIFACT_NAMES:
            path = folder / f"{name}.json"
            payload = bundle.artifacts[name]
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if hash_object(existing) != hash_object(payload):
                    raise CacheError(
                        f"refusing to overwrite immutable agent artifact {path}; "
                        "identical inputs must produce the same cache key, so "
                        "differing content means an input changed (SRC-SAI §49)"
                    )
                continue
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        manifest = {
            "cache_key": key.digest(),
            "components": key.components(),
            "artifacts": list(AGENT_ARTIFACT_NAMES),
            "provider_request_id": bundle.provider_request_id,
            "token_usage": bundle.token_usage,
            "latency_ms": bundle.latency_ms,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = folder / "manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        if self.registry is not None:
            self.registry.record_cache_entry(
                cache_key=key.digest(),
                artifact_path=folder.as_posix(),
                provider_request_id=bundle.provider_request_id,
                token_usage=bundle.token_usage,
                latency_ms=bundle.latency_ms,
            )
        return folder
