# PC-MEF Research System source maintenance contract
# 上下游: 由 experiments.e2_formal 建立後傳給 perception.pcmef_orchestrator
#         的 decide_case(case_arbiter=...)；內部呼叫 agents.cache 的
#         AgentArtifactCache 與 agents.pcmef_agents 的 case_cache_key /
#         run_pcmef_case。
# 檔案路徑: pcmef/agents/cached_case.py
# 產生時間: 2026-09-04 17:40 +08:00
# 版本: v0.1.0
# 功能說明: 把 content-addressed cache 接到正式決策路徑上。
#           同一份證據配同一組模型/prompt/schema/runtime，只問外部模型一次。
# 模組定位: §48 與 FR-031 的接線層。cache 本體（agents/cache.py）與鑰匙組成
#           （pcmef_agents.case_cache_key）早就存在但沒有任何 production
#           呼叫者；本檔就是那條線。
#           它**在決策路徑上**，不是旁路 —— 與 decision trace 不同，
#           這裡的失敗不能默默吞掉。
# 主要責任:
#   1. CachedArbiter 依 §48 七要素組鑰匙，命中就不呼叫 provider
#   2. 記錄每個 case 是命中還是實呼，供 trace 與費用統計使用
#   3. 未提供 cache 時完全不介入 —— 預設行為與接線前逐 byte 相同
# 維護提醒:
#   - 不得在命中後「順便再問一次確認」。FR-031 與 LLM-RESUME-01 要求
#     已完成的 formal case 只讀 frozen response，provider call 不得增加。
#   - 不得把讀取失敗當成命中。半套的目錄、對不上的 manifest 一律視為未命中
#     並重新呼叫 —— 服務一份錯的答案，比多花一次錢嚴重得多。
#   - 不得把 cache 寫入失敗吞掉。這一層在決策路徑上：寫不進去代表下一次
#     resume 會重新付費，使用者必須當下就知道。
#   - 不得因為命中就在 trace 裡假裝有 role payload。cache 依 §48 只存六份
#     artifact，不存 role-projected payload；命中時那些投影根本沒有發生
#     （NOTE-070）。
#   - v0.1.0 新增：首版接線，對應 P3-1。
# 驗證方式:
#   - py -3.10 -m pytest tests/cache/test_cached_case.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = ["CacheIdentity", "CachedArbiter", "CaseCacheOutcome"]


@dataclass(frozen=True)
class CacheIdentity:
    """鑰匙裡與 case 無關的那幾項，整場 run 固定。

    刻意做成一個值物件而不是散落的參數：七要素少填一項，cache 就會在
    模型換版之後仍然命中舊答案，而那不會有任何症狀。
    """

    model_id: str
    provider_revision: str
    runtime_config_hash: str
    schema_dir: Path = Path("schemas")

    def __post_init__(self) -> None:
        for name in ("model_id", "runtime_config_hash"):
            if not getattr(self, name):
                raise ValueError(
                    f"cache identity needs {name!r}; 少了它，換模型或換 runtime "
                    "設定之後仍會命中舊答案"
                )


@dataclass
class CaseCacheOutcome:
    """一個 case 的快取結果，供 trace 與費用統計使用。"""

    cache_key: str
    hit: bool


@dataclass
class CachedArbiter:
    """decide_case 的 `case_arbiter`：命中就不呼叫 provider。

    介面刻意與 `run_pcmef_case` 相同 —— `(runner, evidence) -> AgentBundle` ——
    這樣 decide_case 完全不需要知道有沒有快取這回事。
    """

    cache: Any
    identity: CacheIdentity
    hits: int = 0
    misses: int = 0
    #: 最近一次的結果。e2_formal 在每個 case 之後讀它寫進 trace。
    last: CaseCacheOutcome | None = field(default=None)

    def __call__(self, runner: Any, evidence: Mapping[str, Any]):
        from pcmef.agents.pcmef_agents import case_cache_key, run_pcmef_case

        key = case_cache_key(
            evidence,
            model_id=self.identity.model_id,
            provider_revision=self.identity.provider_revision,
            runtime_config_hash=self.identity.runtime_config_hash,
            schema_dir=self.identity.schema_dir,
        )
        digest = key.digest()

        # resolve() 內部：命中就絕不呼叫 producer。把實際呼叫包在 producer
        # 裡而不是先 get 再自行判斷，是為了讓「命中不呼叫」由 cache 保證，
        # 而不是由這裡的一個 if 保證。
        bundle, hit = self.cache.resolve(
            key, lambda: run_pcmef_case(runner, evidence)
        )
        if hit:
            self.hits += 1
        else:
            self.misses += 1
        self.last = CaseCacheOutcome(cache_key=digest, hit=hit)
        return bundle

    def summary(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": True,
            "cache_root": str(getattr(self.cache, "root", "")),
            "hits": self.hits,
            "misses": self.misses,
            "cases": total,
            "hit_rate": (self.hits / total) if total else None,
            "model_id": self.identity.model_id,
            "provider_revision": self.identity.provider_revision,
            "runtime_config_hash": self.identity.runtime_config_hash,
        }
