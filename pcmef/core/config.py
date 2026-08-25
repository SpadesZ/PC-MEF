# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 與 experiments.e1/e2、splits、simulation、models、gate、stats 呼叫；
#         讀取 configs/ 下的 YAML 檔（低優先序在前）與 CLI override，
#         輸出的 ResolvedConfig 供全系統讀值，config_hash() 流向 formal_config.lock。
# 檔案路徑: pcmef/core/config.py
# 產生時間: 2026-08-25 21:50 +08:00
# 版本: v0.1.0
# 功能說明: 載入並依優先序合併多層設定檔，同時實作一個關鍵機制 —— 尚未經教授核定的
#           數值在 YAML 裡寫成 !required，任何程式讀到它就直接中斷，讓「缺一個裁決」
#           不可能被一個看似合理的預設值蓋過去。
# 模組定位: 設定的唯一入口與 formal-blocking 的強制執行點。它不載入 secret；
#           secret 只能來自環境變數或 secret vault，config 內僅保存 secret_ref。
# 主要責任:
#   1. Required sentinel 在布林與數值語境下一律拋 FormalBlockingError
#   2. _PCMEFLoader 以 SafeLoader 為基底解析 !required tag
#   3. _deep_merge() 實作 scalar 覆蓋、dict 逐層合併、list 整體取代的優先序規則
#   4. _label_required() 為每個 sentinel 補上自己的 key path 供錯誤訊息定位
#   5. ResolvedConfig.get() 讀到 sentinel 即中斷，且 default 不得吸收它
#   6. load_config() 在 formal 模式下全樹掃描並拒絕任何 CLI override
# 維護提醒:
#   - 不得為尚未核定的數值填入任何預設值，包含「暫時的」預設值；一律寫 !required。
#     這是 SRC-PLAN Appendix A 與 SRC-SAI Appendix F 的硬性要求（NOTE-005）。
#   - 不得用 get(key, default) 繞過 sentinel；default 只在 key 不存在時生效。
#   - 不得在 formal 模式接受 CLI override；會改變研究結果的參數只能來自版本化 config。
#   - 不得把 Required 改成 None：None 會被 or 與 falsy 判斷悄悄吸收掉。
#   - v0.1.0 新增：首版 formal-blocking 機制，決策見 NOTE-005。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_config_and_locks.py -k "required or formal or override or config_hash"
#   - py -3.10 -m pcmef.cli config check
# ------------------------------------------------------------

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pcmef.core.hash import hash_object

__all__ = [
    "FormalBlockingError",
    "ConfigError",
    "Required",
    "REQUIRED",
    "ResolvedConfig",
    "load_config",
]


class ConfigError(ValueError):
    """設定檔結構錯誤、路徑不存在或 override 格式非法。"""


class FormalBlockingError(RuntimeError):
    """讀到尚未由教授核定的數值。

    NOTE(NOTE-005): 這是 SRC-PLAN Appendix A Formal-readiness note 與
    SRC-SAI Appendix F 的直接實作 —— 未核定的 numeric threshold/allocation
    必須保持 formal-blocking，禁止實作端自行補值。

    看到這個例外時的正確處置是「去要一個裁決」，不是「先填一個值讓它跑起來」。
    """


@dataclass(frozen=True)
class Required:
    """代表一個等待教授裁決的設定值。

    刻意做成物件而不是 None：None 會被 `or` 預設值、`get(key, default)`
    與各種 falsy 判斷悄悄吸收掉，最後變成一個沒人記得出處的數字；
    這個 sentinel 只要被當成數值使用就會炸開。
    """

    key: str = "<unspecified>"
    source: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        raise FormalBlockingError(
            f"config value {self.key!r} awaits advisor approval and cannot be used in "
            f"a boolean context. {self._detail()}"
        )

    def __float__(self) -> float:
        raise FormalBlockingError(
            f"config value {self.key!r} awaits advisor approval and has no numeric "
            f"value. {self._detail()}"
        )

    __int__ = __float__

    def _detail(self) -> str:
        parts = []
        if self.source:
            parts.append(f"source: {self.source}")
        if self.reason:
            parts.append(f"reason: {self.reason}")
        parts.append(
            "Implementations must not substitute a default; obtain the decision and "
            "record it in a versioned config."
        )
        return " ".join(parts)

    def __repr__(self) -> str:
        return f"Required({self.key!r})"


REQUIRED = Required()


# ---------------------------------------------------------------------------
# YAML loader：支援 !required tag
# ---------------------------------------------------------------------------


class _PCMEFLoader(yaml.SafeLoader):
    """SafeLoader 加上 !required tag。

    以 SafeLoader 為基底而非 FullLoader：config 檔會在不同機器之間流通，
    任意物件建構是不必要的風險面。
    """


def _construct_required(loader: yaml.Loader, node: yaml.Node) -> Required:
    if isinstance(node, yaml.ScalarNode):
        raw = str(loader.construct_scalar(node) or "").strip()
        return Required(reason=raw)
    if isinstance(node, yaml.MappingNode):
        mapping = loader.construct_mapping(node, deep=True)
        return Required(
            source=str(mapping.get("source", "")),
            reason=str(mapping.get("reason", "")),
        )
    return Required()


_PCMEFLoader.add_constructor("!required", _construct_required)


# ---------------------------------------------------------------------------
# 合併與展平
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """遞迴合併。overlay 的 scalar 覆蓋 base；dict 逐層合併；list 整體取代。

    list 刻意整體取代而非串接：像 feature 順序、grid 這類清單，
    串接語意會產生重複項且順序不可預測，比覆蓋更難察覺。
    """
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _iter_leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(node, dict):
        leaves: list[tuple[str, Any]] = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            leaves.extend(_iter_leaves(value, path))
        return leaves
    if isinstance(node, list):
        leaves = []
        for index, item in enumerate(node):
            leaves.extend(_iter_leaves(item, f"{prefix}[{index}]"))
        return leaves
    return [(prefix, node)]


def _label_required(node: Any, prefix: str = "") -> Any:
    """把 Required sentinel 補上它自己的 key path，讓錯誤訊息能指出缺哪一格。"""
    if isinstance(node, Required):
        return Required(key=prefix, source=node.source, reason=node.reason)
    if isinstance(node, dict):
        return {
            key: _label_required(value, f"{prefix}.{key}" if prefix else str(key))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [
            _label_required(item, f"{prefix}[{index}]")
            for index, item in enumerate(node)
        ]
    return node


# ---------------------------------------------------------------------------
# ResolvedConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedConfig:
    """合併後的設定樹。formal 模式下保證不含任何未解析的 Required。"""

    data: dict[str, Any]
    sources: tuple[str, ...]
    formal: bool

    def get(self, dotted_key: str, default: Any = ...) -> Any:
        """依點號路徑取值。讀到 Required 一律 fail-fast。

        default 只在「路徑不存在」時生效；路徑存在但值是 Required 時
        **不會**套用 default —— 否則呼叫端只要順手寫個 default，
        formal-blocking 就被繞過了。
        """
        node: Any = self.data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is ...:
                    raise ConfigError(
                        f"config key {dotted_key!r} not found "
                        f"(sources: {', '.join(self.sources) or 'none'})"
                    )
                return default
            node = node[part]
        if isinstance(node, Required):
            raise FormalBlockingError(
                f"config value {dotted_key!r} awaits advisor approval. "
                f"{node._detail()}"
            )
        return node

    def unresolved(self) -> list[Required]:
        """列出所有仍待裁決的設定值。"""
        return [
            value
            for _, value in _iter_leaves(self.data)
            if isinstance(value, Required)
        ]

    def config_hash(self) -> str:
        """resolved config 的 canonical hash，供 formal_config.lock 使用。

        Required sentinel 會被序列化成顯式標記而非消失，
        讓「這份 config 還缺裁決」本身也成為 hash 的一部分 ——
        一個帶缺口的 config 不應該和補完之後的 config 得到相同 hash。
        """
        return hash_object(self._hashable(self.data))

    @classmethod
    def _hashable(cls, node: Any) -> Any:
        if isinstance(node, Required):
            return {"__required__": node.key, "source": node.source}
        if isinstance(node, dict):
            return {key: cls._hashable(value) for key, value in node.items()}
        if isinstance(node, list):
            return [cls._hashable(item) for item in node]
        return node


# ---------------------------------------------------------------------------
# 載入入口
# ---------------------------------------------------------------------------


def _coerce_override(raw: str) -> Any:
    """把 CLI override 的字串值轉成 YAML scalar，保持與檔案內同樣的型別語意。"""
    return yaml.safe_load(raw)


def load_config(
    paths: list[str | Path],
    overrides: dict[str, str] | None = None,
    formal: bool = False,
) -> ResolvedConfig:
    """依優先序載入並合併設定。

    paths 由低優先序排到高（SRC-SAI §33：code defaults < base.yaml <
    module config < experiment config < CLI override）。

    formal=True 時做兩件事：
      1. 全樹掃描，任何殘留的 Required 直接拒絕啟動 —— 不等到該值被讀到才失敗。
         SRC-SAI NFR-04 要求 fail-fast，而且 formal run 前段可能是數小時的
         simulation，把缺值留到後段才爆等於白跑。
      2. 拒絕任何 CLI override —— SRC-SAI §33 規定 formal 模式下 CLI 不得覆蓋
         會改變研究結果的參數。這裡採全面禁止而非白名單，
         run 命名與輸出路徑另由 CLI 專屬參數處理，不走 config override。
    """
    resolved: dict[str, Any] = {}
    used_sources: list[str] = []

    for path in paths:
        config_path = Path(path)
        if not config_path.exists():
            raise ConfigError(f"config file not found: {config_path}")
        loaded = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_PCMEFLoader)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"config file {config_path} must contain a mapping at the top level"
            )
        resolved = _deep_merge(resolved, loaded)
        used_sources.append(config_path.as_posix())

    if overrides:
        if formal:
            raise ConfigError(
                "CLI overrides are forbidden in --formal mode; parameters that can "
                "change research results must come from versioned config only "
                f"(attempted: {sorted(overrides)})"
            )
        for dotted_key, raw_value in overrides.items():
            parts = dotted_key.split(".")
            if not all(parts):
                raise ConfigError(f"invalid override key {dotted_key!r}")
            node = resolved
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = _coerce_override(raw_value)
        used_sources.append("cli-override")

    resolved = _label_required(resolved)
    config = ResolvedConfig(
        data=resolved, sources=tuple(used_sources), formal=bool(formal)
    )

    if formal:
        pending = config.unresolved()
        if pending:
            details = "\n".join(
                f"  - {item.key}"
                + (f"  [{item.source}]" if item.source else "")
                + (f"\n      {item.reason}" if item.reason else "")
                for item in sorted(pending, key=lambda entry: entry.key)
            )
            raise FormalBlockingError(
                f"{len(pending)} config value(s) still await advisor approval and "
                f"must not be defaulted by the implementation:\n{details}"
            )

    return config
