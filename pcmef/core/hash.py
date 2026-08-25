# 檔案路徑: pcmef/core/hash.py
# 模組定位: PC-MEF 全系統唯一的 canonical hashing 實作。
# 功能說明: 提供 canonical JSON 序列化、SHA-256 摘要、檔案摘要與 evidence/config hash 組裝。
# 主要責任: 讓相同語意內容在任何機器、任何執行順序下都產生相同 hash，作為 freeze 與 cache 的基礎。
# 呼叫來源: core.locks、core.config、adapters、simulation、agents.cache、registry、所有 freeze 指令。
# 輸入契約: JSON-safe 物件（dict/list/str/int/float/bool/None）或既有檔案路徑；不接受 NaN/Inf。
# 輸出契約: 小寫 hex SHA-256 字串；序列化結果為 UTF-8 bytes，key 已排序且無多餘空白。
# 安全邊界: 呼叫端必須先移除 secret；本模組不做 secret 偵測，會忠實 hash 收到的內容。
# 維護提醒: 序列化參數任一改動都會讓所有既有 lock hash 失效，等同全系統重新 freeze。
# 版本: v0.1.0 / 2026-08-25
# ----------------------------------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePath
from typing import Any

import numpy as np

__all__ = [
    "CanonicalJSONError",
    "canonical_json",
    "canonical_json_bytes",
    "sha256_hex",
    "hash_object",
    "hash_file",
    "hash_array",
    "combine_hashes",
]

_FILE_CHUNK_BYTES = 1024 * 1024


class CanonicalJSONError(TypeError):
    """物件無法被確定性序列化（含 NaN/Inf、非 JSON-safe 型別、非字串 key）。"""


def _coerce(value: Any) -> Any:
    """把 numpy 純量/陣列轉成 JSON-safe 型別，並拒絕非有限浮點數。

    刻意不使用 json.dumps 的 default= 掛勾處理 numpy：default 只在
    「json 不認識的型別」時被呼叫，而 np.float64 是 float 的子類別，
    會被直接當成 float 走進 C 加速路徑而繞過檢查。先整棵樹正規化才可靠。
    """
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return _coerce(float(value))
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.ndarray):
        return [_coerce(item) for item in value.tolist()]
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalJSONError(
                f"non-finite float {value!r} cannot be canonically hashed; "
                "resolve the numeric invariant before freezing"
            )
        return value
    if isinstance(value, dict):
        coerced: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError(
                    f"canonical JSON requires string keys, got {type(key).__name__}"
                )
            coerced[key] = _coerce(item)
        return coerced
    if isinstance(value, (list, tuple)):
        return [_coerce(item) for item in value]
    if isinstance(value, PurePath):
        # 比對 PurePath 而非 Path：PureWindowsPath 不是 Path 的子類別，
        # 只認 Path 會讓 Windows 上構造的純路徑物件掉進「不支援型別」分支。
        # 一律轉 POSIX 形式，否則同一份內容在 Windows 與 Linux 會得到不同 hash。
        return value.as_posix()
    raise CanonicalJSONError(
        f"type {type(value).__name__} is not JSON-safe for canonical hashing"
    )


def canonical_json(obj: Any) -> str:
    """回傳確定性 JSON 字串：key 已排序、無多餘空白、非 ASCII 保持原樣。

    ensure_ascii=False 讓中文欄位以 UTF-8 原樣輸出而非 \\uXXXX escape；
    因為最終一律以 UTF-8 編碼再 hash，兩種寫法都可行，但保持原樣讓
    lock 檔在人工檢視時可讀，而可讀性是 audit 的一部分。
    """
    return json.dumps(
        _coerce(obj),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_object(obj: Any) -> str:
    """對任意 JSON-safe 物件取 canonical SHA-256。"""
    return sha256_hex(canonical_json_bytes(obj))


def hash_file(path: str | Path) -> str:
    """對檔案內容取 SHA-256，分塊讀取以免大型 transient/RGB artifact 佔滿記憶體。"""
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(_FILE_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_array(array: np.ndarray) -> str:
    """對 ndarray 取 SHA-256，內容之外同時涵蓋 dtype 與 shape。

    只 hash raw bytes 是不夠的：同一段 bytes 在 float32/float64 或不同 shape 下
    語意完全不同，而 evidence_hash 會被拿來當 agent cache key（SRC-SAI §48），
    cache 誤命中會讓兩個不同 evidence 共用同一份 s_A。
    """
    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(str(contiguous.dtype).encode("utf-8"))
    digest.update(b"|")
    digest.update(str(contiguous.shape).encode("utf-8"))
    digest.update(b"|")
    digest.update(contiguous.tobytes())
    return digest.hexdigest()


def combine_hashes(*parts: str) -> str:
    """把多個 hex hash 依給定順序組合成單一 hash。

    順序有意義（SRC-SAI §48 的 agent_cache_key 是有序串接），
    因此不排序也不去重；呼叫端必須自行固定傳入順序。
    """
    digest = hashlib.sha256()
    for index, part in enumerate(parts):
        if not isinstance(part, str) or not part:
            raise CanonicalJSONError(
                f"combine_hashes part {index} must be a non-empty hex string"
            )
        digest.update(part.encode("utf-8"))
        digest.update(b"|")
    return digest.hexdigest()
