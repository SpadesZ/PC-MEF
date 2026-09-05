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
    #: 這一場 run 的身分，寫進 producer trace 供日後追溯。
    run_identity: Mapping[str, Any] = field(default_factory=dict)
    #: 目前正在決定哪一列。由 executor 在每個 case 之前設定；只用於
    #: producer trace 的可讀性，不參與 cache key（那會破壞內容定址）。
    case_index: int | None = None
    #: 寫 producer trace 時發生的錯誤。summary() 會帶出去，讓畫面看得到
    #: 「這次有幾筆命中之後將無法追溯」。
    provenance_errors: list[str] = field(default_factory=list)

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

        # 這個 case 開始前 call_log 的長度。命中時不會有新紀錄，未命中時
        # 新增的那幾筆就是四個角色實際送出／收回的內容。
        before = len(getattr(runner, "call_log", []) or [])

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
            # 未命中 = 這一次真的問了模型，也就是這把鑰匙的**產生者**。
            # 四個角色的實際 payload 只有現在存在：cache 依 §48 不存它們，
            # 之後任何一次命中都再也組不出來（NOTE-074）。
            self._write_provenance(key, digest, runner, before, bundle)
        self.last = CaseCacheOutcome(cache_key=digest, hit=hit)
        return bundle

    def _write_provenance(
        self, key: Any, digest: str, runner: Any, before: int, bundle: Any
    ) -> None:
        """把這一次實際執行的四份角色紀錄寫進 cache 目錄。

        寫入失敗**不中止決策**：答案已經拿到、六份 artifact 已經落盤，
        少一份稽核紀錄不該讓一次 formal run 失敗。但它必須被說出來 ——
        沉默地少掉解釋能力，正是這一段要修的東西。
        """
        writer = getattr(self.cache, "write_provenance", None)
        if writer is None:
            return
        try:
            records = [
                r.to_json() for r in list(getattr(runner, "call_log", []) or [])[before:]
            ]
            writer(
                key,
                {
                    "schema_version": "agent_producer_trace_v1",
                    "cache_key": digest,
                    # 內容定址的身分。case_id / class_label / condition 刻意
                    # 缺席：它們是 §48 明列不得進入 cache 的欄位，寫進來就
                    # 等於讓這個目錄帶上 case 身分。
                    "evidence_hash": key.components().get("evidence_hash"),
                    "key_components": key.components(),
                    "producer_run": dict(self.run_identity),
                    "producer_row_index": self.case_index,
                    "model_id": self.identity.model_id,
                    "provider_revision": self.identity.provider_revision,
                    "runtime_config_hash": self.identity.runtime_config_hash,
                    "provider_request_id": getattr(bundle, "provider_request_id", ""),
                    "token_usage": getattr(bundle, "token_usage", 0),
                    "latency_ms": getattr(bundle, "latency_ms", 0),
                    "n_roles": len(records),
                    "total_attempts": sum(
                        int(r.get("attempt_count") or 0) for r in records
                    ),
                    "artifacts": records,
                    # 純文字：這份 JSON 會被畫面讀出來直接顯示，markdown 的
                    # 星號不會被渲染成粗體，只會原樣印在頁面上。
                    "note": (
                        "這一份記錄的是產生這把鑰匙的那一次執行：四個角色"
                        "各自實際收到的 payload、有沒有收到影像、重試幾次、"
                        "原始與驗證後的回覆。任何一次命中都不會重新產生這些"
                        "投影，因此解釋能力只能靠這一份保存。"
                    ),
                },
            )
        except Exception as error:  # noqa: BLE001
            self.provenance_errors.append(f"{type(error).__name__}: {error}")

    def summary(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": True,
            "cache_root": str(getattr(self.cache, "root", "")),
            "hits": self.hits,
            "misses": self.misses,
            "cases": total,
            "hit_rate": (self.hits / total) if total else None,
            # 命中一次就是省下四次呼叫（四個角色各一次，不含 retry）。
            # 沒有這個換算，畫面上的 hits 說不出它到底省了什麼。
            "provider_calls_avoided": self.hits * 4,
            "model_id": self.identity.model_id,
            "provider_revision": self.identity.provider_revision,
            "runtime_config_hash": self.identity.runtime_config_hash,
            "provenance_errors": list(self.provenance_errors),
        }
