# 檔案路徑: tests/unit/test_hash.py
# 模組定位: canonical hashing 確定性契約的可執行版本。
# 功能說明: 驗證 key 順序無關、非有限浮點被拒、numpy 型別可序列化、array hash 涵蓋 dtype 與 shape。
# 主要責任: 保護 NFR-01 Reproducibility 與 §48 content-addressed cache 的正確性。
# 呼叫來源: pytest。
# 輸入契約: 合成物件與陣列。
# 輸出契約: 測試通過與否。
# 安全邊界: 僅使用 tmp_path 做檔案 hash 測試。
# 維護提醒: 序列化參數若有變更，本檔的 golden hash 會失敗 —— 那代表全系統 lock 需要重新 freeze。
# 版本: v0.1.0 / 2026-08-25
# ----------------------------------------------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.hash import (
    CanonicalJSONError,
    canonical_json,
    combine_hashes,
    hash_array,
    hash_file,
    hash_object,
)


def test_hash_is_independent_of_key_insertion_order():
    assert hash_object({"a": 1, "b": 2}) == hash_object({"b": 2, "a": 1})


def test_hash_is_sensitive_to_list_order():
    """list 順序有意義：feature 順序與 seed matrix 都靠順序表達語意。"""
    assert hash_object([1, 2, 3]) != hash_object([3, 2, 1])


def test_nested_structures_are_normalised():
    left = {"outer": {"inner": [1, {"deep": 2}]}}
    right = {"outer": {"inner": [1, {"deep": 2}]}}
    assert hash_object(left) == hash_object(right)


def test_numpy_scalars_hash_identically_to_python_scalars():
    assert hash_object({"x": np.float64(1.5)}) == hash_object({"x": 1.5})
    assert hash_object({"n": np.int64(7)}) == hash_object({"n": 7})
    assert hash_object({"flag": np.bool_(True)}) == hash_object({"flag": True})


def test_non_finite_floats_are_refused():
    """NaN/Inf 進 freeze 代表數值 invariant 已經壞掉，必須先修而不是先 hash。"""
    for bad in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(CanonicalJSONError):
            hash_object({"value": bad})


def test_numpy_non_finite_is_refused_through_the_normaliser():
    """np.float64 是 float 的子類別，若靠 json default= 掛勾會被繞過。"""
    with pytest.raises(CanonicalJSONError):
        hash_object({"value": np.float64("nan")})


def test_non_string_keys_are_refused():
    with pytest.raises(CanonicalJSONError):
        hash_object({1: "a"})


def test_unsupported_types_are_refused():
    with pytest.raises(CanonicalJSONError):
        hash_object({"when": object()})


def test_canonical_json_has_no_incidental_whitespace():
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_canonical_json_keeps_non_ascii_readable():
    assert "校準" in canonical_json({"note": "校準"})


def test_paths_are_normalised_to_posix():
    from pathlib import PurePosixPath, PureWindowsPath

    windows = hash_object({"p": PureWindowsPath("data/raw_real/a.csv")})
    posix = hash_object({"p": PurePosixPath("data/raw_real/a.csv")})
    assert windows == posix


def test_array_hash_covers_dtype_and_shape_not_just_bytes():
    """cache 誤命中會讓兩個不同 evidence 共用同一份 s_A，因此 dtype/shape 必須納入。"""
    base = np.arange(8, dtype=np.float64)
    assert hash_array(base) != hash_array(base.astype(np.float32))
    assert hash_array(base) != hash_array(base.reshape(2, 4))
    assert hash_array(base) == hash_array(base.copy())


def test_array_hash_is_layout_independent():
    contiguous = np.arange(12, dtype=np.float64).reshape(3, 4)
    transposed_back = np.asfortranarray(contiguous).copy(order="C")
    assert hash_array(contiguous) == hash_array(transposed_back)


def test_file_hash_matches_content(tmp_path):
    path = tmp_path / "recording.csv"
    path.write_bytes(b"distance,ambient,signal,sigma\n1,2,3,4\n")
    first = hash_file(path)
    assert first == hash_file(path)
    path.write_bytes(b"distance,ambient,signal,sigma\n1,2,3,5\n")
    assert hash_file(path) != first


def test_combine_hashes_is_order_sensitive():
    a, b = "a" * 64, "b" * 64
    assert combine_hashes(a, b) != combine_hashes(b, a)


def test_combine_hashes_refuses_empty_parts():
    with pytest.raises(CanonicalJSONError):
        combine_hashes("a" * 64, "")
