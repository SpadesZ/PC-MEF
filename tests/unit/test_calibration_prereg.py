# PC-MEF Research System source maintenance contract
# 上下游: 驗證 pcmef.experiments.calibration_prereg 的 CP-01..CP-16 與凍結契約；
#         以 repo 內真實的 configs/calibration_preregistration.yaml 為基準，
#         逐項複製後刻意破壞，確認每一條檢查都會咬人。
# 檔案路徑: tests/unit/test_calibration_prereg.py
# 產生時間: 2026-08-30 12:10 +08:00
# 版本: v0.2.0
# 功能說明: 確認「26 個參數不能一起 fit」這件事是被程式擋住的，而不是靠
#           預註冊檔上寫了一段文字。
# 模組定位: 校準預註冊驗證器的行為契約測試。重點是每一條 CP 檢查都有一個
#           對應的、**會失敗的**破壞情境 —— 少了它們，驗證器可能整條都是
#           空轉而沒有人會發現。
# 主要責任:
#   1. 真實預註冊檔必須 16/16 PASS（基準線）
#   2. 每一條 CP 檢查各有至少一個對應的破壞情境會 FAIL
#   3. gauge 固定項與 estimator 參數進入搜尋空間必須被擋
#   4. real class mean 掃描器本身必須真的抓得到植入的字面值
#   5. 未通過檢查時不得凍結；已凍結後不得覆寫
#   6. stage 0 退回以 s_f 為分母、或預算表被手改，皆必須 FAIL
# 維護提醒:
#   - 不得刪除 test_the_real_protocol_passes_every_check：少了基準線，
#     其餘負向測試會在「驗證器永遠拒絕一切」的情況下全部通過。
#   - 不得刪除 test_cp15_allows_explaining_why_s_f_was_rejected：它擋的是
#     「守衛過嚴逼人刪掉理由」這個相反方向的失敗。
#   - 不得為了讓測試變簡單而把驗證器改成只檢查欄位存在；
#     CP-02 與 CP-03 比對的是內容，不是欄位有沒有寫。
#   - 不得在本檔內讀取 calibration partition 的任何數值。
#   - v0.2.0 新增 CP-13..CP-16 的負向測試，並補上 amendment 綁定檢查
#     （NOTE-042 / AMD-003）。
#   - v0.1.0 新增：對應 NOTE-041 / CAL-PREREG-001。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_prereg.py -v
#   - py -3.10 -m pcmef.cli calibration preregister --validate
# ------------------------------------------------------------

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from pcmef.audit.result import CheckStatus
from pcmef.experiments.calibration_prereg import (
    PREREGISTRATION_PATH,
    REQUIRED_CHECKS,
    PreregistrationError,
    _scan_for_real_means,
    freeze_preregistration,
    load_protocol,
    validate_preregistration,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FREEZE_DIR = REPO_ROOT / "freeze"


def _write(tmp_path: Path, protocol: dict) -> Path:
    target = tmp_path / "calibration_preregistration.yaml"
    target.write_text(
        yaml.safe_dump(protocol, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def _status(protocol: dict, tmp_path: Path, check: str) -> CheckStatus:
    report = validate_preregistration(_write(tmp_path, protocol), FREEZE_DIR, REPO_ROOT)
    return report.get(check).status


@pytest.fixture()
def protocol() -> dict:
    return copy.deepcopy(load_protocol())


# ---------------------------------------------------------------------------
# 基準線
# ---------------------------------------------------------------------------


def test_the_real_protocol_passes_every_check():
    report = validate_preregistration(PREREGISTRATION_PATH, FREEZE_DIR, REPO_ROOT)
    unmet = report.unmet(REQUIRED_CHECKS)
    assert not unmet, [f"{c.identifier}: {c.findings}" for c in unmet]
    assert len(report.results) == len(REQUIRED_CHECKS)


def test_every_calibration_only_parameter_is_accounted_for():
    """26 項全部有歸屬；沒有任何一項是「忘了寫」而悄悄留在初始值。"""
    from pcmef.core.parameters import ParameterRegistry

    protocol = load_protocol()
    registry = ParameterRegistry.load()
    expected = {p.name for p in registry.parameters if p.kind == "calibration_only"}
    declared = {n for s in protocol["stagewise"] for n in s["parameters"]}
    declared |= {m["name"] for m in protocol["not_fitted"]["members"]}
    assert declared == expected


def test_stages_are_disjoint():
    """同一個參數不得出現在兩個階段；否則後一階段會覆寫前一階段的結果。"""
    protocol = load_protocol()
    seen: set[str] = set()
    for stage in protocol["stagewise"]:
        overlap = seen & set(stage["parameters"])
        assert not overlap, f"stage {stage['id']} 與前面階段重疊：{overlap}"
        seen |= set(stage["parameters"])


# ---------------------------------------------------------------------------
# CP-01 覆蓋
# ---------------------------------------------------------------------------


def test_cp01_fails_when_a_parameter_is_unclassified(protocol, tmp_path):
    protocol["stagewise"][2]["parameters"].remove("ambient_jitter_relative")
    assert _status(protocol, tmp_path, "CP-01") is CheckStatus.FAIL


def test_cp01_fails_when_a_parameter_is_classified_twice(protocol, tmp_path):
    protocol["stagewise"][1]["parameters"].append("ambient_energy_to_mcps")
    assert _status(protocol, tmp_path, "CP-01") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# CP-02 搜尋空間
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gauge_member",
    ["_SIGMA_T_REFERENCE_PER_M", "lighting.irradiance", "_ROOM_LIGHT_RADIANCE"],
)
def test_cp02_fails_when_a_gauge_member_enters_the_search_space(
    protocol, tmp_path, gauge_member
):
    """CG-1/2/3 的固定項進了搜尋空間，等於那三次裁決沒有發生。"""
    protocol["stagewise"][0]["parameters"].append(gauge_member)
    assert _status(protocol, tmp_path, "CP-02") is CheckStatus.FAIL


def test_cp02_fails_when_a_derived_parameter_enters_the_search_space(protocol, tmp_path):
    protocol["stagewise"][2]["parameters"].append("optical_path_to_distance")
    assert _status(protocol, tmp_path, "CP-02") is CheckStatus.FAIL


@pytest.mark.parametrize("knob", ["detection_threshold_sigma", "min_return_bins"])
def test_cp02_fails_when_calibration_tries_to_move_the_estimator(
    protocol, tmp_path, knob
):
    """NOTE-037：不得因為 calibration 的結果回頭換 estimator 或它的參數。"""
    protocol["stagewise"][4]["parameters"].append(knob)
    assert _status(protocol, tmp_path, "CP-02") is CheckStatus.FAIL


_CG5_ENTRY = {
    "name": "_ALBEDO_BY_PRESET.water",
    "group": "CG-5_effective_medium",
    "why": "density <-> albedo 近乎精確簡併（實測 |cos| 0.9987）",
}


def test_cp02_enforces_every_gauge_member_the_protocol_lists_not_just_cg123(
    protocol, tmp_path
):
    """AMD-004 P0-3：CG-5 的 albedo 必須真的被擋住，不是只寫在 YAML 裡。

    先前 CP-02 只硬寫 CG-1/2/3 那三個名字，於是任何**新**的 gauge 固定項
    加進 forbidden_parameters 之後，validator 完全不會檢查它有沒有溜回
    搜尋空間 —— 「寫下裁決」與「裁決生效」是兩回事。
    """
    # 用一個**目前確實在擬合**的參數：把它列為 gauge 固定項卻沒有移出
    # stagewise，必須 FAIL。CG-5 的 albedo 已經移出去了，拿它測不出東西。
    protocol["forbidden_parameters"]["gauge_fixed"].append(
        {"name": "sensor.fov_deg", "group": "CG-6_effective_angular_response",
         "why": "測試用：列為固定項但未移出搜尋空間"}
    )
    assert _status(protocol, tmp_path, "CP-02") is CheckStatus.FAIL


def test_cp02_passes_once_the_gauge_member_actually_leaves_the_search_space(
    protocol, tmp_path
):
    """成對：真的把它移出擬合之後就該 PASS。

    少了這一條，上一條會在「CP-02 永遠 FAIL」的情況下通過。
    """
    protocol["forbidden_parameters"]["gauge_fixed"].append(
        {"name": "sensor.fov_deg", "group": "CG-6_effective_angular_response",
         "why": "測試用"}
    )
    for stage in protocol["stagewise"]:
        if "sensor.fov_deg" in stage["parameters"]:
            stage["parameters"].remove("sensor.fov_deg")
    protocol["not_fitted"]["members"].append(
        {"name": "sensor.fov_deg", "value": 45.0, "why": "測試用"}
    )
    assert _status(protocol, tmp_path, "CP-02") is CheckStatus.PASS


def test_cp02_fails_when_a_gauge_member_is_no_longer_listed(protocol, tmp_path):
    """把固定項從禁令清單移掉，即使沒人擬合它也必須 FAIL。"""
    protocol["forbidden_parameters"]["gauge_fixed"] = [
        entry
        for entry in protocol["forbidden_parameters"]["gauge_fixed"]
        if entry["name"] != "lighting.irradiance"
    ]
    assert _status(protocol, tmp_path, "CP-02") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# CP-03 邊界
# ---------------------------------------------------------------------------


def test_cp03_fails_when_a_string_bound_has_no_declared_interpretation(
    protocol, tmp_path
):
    """三個上界在 lock 內是字串；沒有明確宣告解讀就不得擬合。"""
    protocol["bounds"]["declared_numeric_interpretations"] = []
    assert _status(protocol, tmp_path, "CP-03") is CheckStatus.FAIL


def test_cp03_fails_when_the_declared_interpretation_changes_the_bound(
    protocol, tmp_path
):
    """宣告的解讀必須與凍結字面值是同一個數，不得藉「解讀」偷偷放寬。"""
    for entry in protocol["bounds"]["declared_numeric_interpretations"]:
        if entry["parameter"] == "signal_energy_to_mcps":
            entry["interpreted_as"] = [1.0e-6, 1.0e12]
    assert _status(protocol, tmp_path, "CP-03") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# CP-04 目標函數
# ---------------------------------------------------------------------------


def test_cp04_fails_when_the_objective_is_not_distribution_level(protocol, tmp_path):
    protocol["objective"]["discrepancy"]["level"] = "mean"
    assert _status(protocol, tmp_path, "CP-04") is CheckStatus.FAIL


def test_the_real_mean_scanner_actually_catches_a_planted_literal(tmp_path):
    """守衛本身必須真的會抓 —— 否則 CP-04 只是永遠 PASS 的裝飾。"""
    package = tmp_path / "pcmef"
    package.mkdir()
    (package / "objective.py").write_text(
        "TARGET_MEANS = {'Empty': 100.91, 'Water-filled': 113.87}\n", encoding="utf-8"
    )
    findings = _scan_for_real_means(tmp_path, None)
    assert findings and "objective.py" in findings[0]


def test_the_scanner_skips_only_the_marked_guard_definition(tmp_path):
    """略過標記只對定義那一行有效，同一檔案的其他行仍必須被抓。"""
    package = tmp_path / "pcmef"
    package.mkdir()
    (package / "guard.py").write_text(
        'LITERALS = ("100.91",)  # REAL_MEAN_GUARD_DEFINITION\n'
        "SNEAKY = 113.87\n",
        encoding="utf-8",
    )
    findings = _scan_for_real_means(tmp_path, None)
    assert len(findings) == 1
    assert "SNEAKY" in findings[0]


# ---------------------------------------------------------------------------
# CP-05 正規化與權重
# ---------------------------------------------------------------------------


def test_cp05_fails_when_s_f_is_recomputed_per_stage(protocol, tmp_path):
    """逐階段重算 s_f 會讓 optimizer 靠放大模擬離散度稀釋自己的誤差。"""
    protocol["normalization"]["frozen"] = False
    assert _status(protocol, tmp_path, "CP-05") is CheckStatus.FAIL


def test_cp05_fails_when_weights_are_not_fixed_in_advance(protocol, tmp_path):
    protocol["weights"]["fixed_before_seeing_data"] = False
    assert _status(protocol, tmp_path, "CP-05") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# CP-06 可辨識性
# ---------------------------------------------------------------------------


def test_cp06_fails_when_a_stage_has_no_identifiability_argument(protocol, tmp_path):
    protocol["stagewise"][3]["identifiability"] = "會 fit 出來的"
    assert _status(protocol, tmp_path, "CP-06") is CheckStatus.FAIL


def test_cp06_fails_when_stages_are_merged_into_one_joint_fit(protocol, tmp_path):
    """把 26 個參數塞進一個階段一起 fit，正是本預註冊要防的事。"""
    everything = [n for s in protocol["stagewise"] for n in s["parameters"]]
    protocol["stagewise"] = [
        {
            "stage": 1,
            "id": "EVERYTHING",
            "parameters": everything,
            "observables": ["all four features"],
            "identifiability": "x" * 200,
        }
    ]
    assert _status(protocol, tmp_path, "CP-06") is CheckStatus.FAIL


def test_cp06_fails_when_stage_0_would_read_the_calibration_partition(
    protocol, tmp_path
):
    protocol["stage_0"]["reads_calibration_partition"] = True
    assert _status(protocol, tmp_path, "CP-06") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# CP-07 optimizer
# ---------------------------------------------------------------------------


def test_cp07_fails_without_common_random_numbers(protocol, tmp_path):
    """不共用種子的話 optimizer 追的是 Monte Carlo 雜訊，不是參數。"""
    protocol["optimizer"]["common_random_numbers"]["seeds"] = {}
    assert _status(protocol, tmp_path, "CP-07") is CheckStatus.FAIL


def test_cp07_fails_without_a_seed_derivation_rule(protocol, tmp_path):
    protocol["optimizer"]["seeds"]["seed_derivation"] = ""
    assert _status(protocol, tmp_path, "CP-07") is CheckStatus.FAIL


def test_cp07_fails_when_polish_is_enabled(protocol, tmp_path):
    """polish 走 L-BFGS-B，對帶雜訊的目標取數值梯度沒有意義。"""
    protocol["optimizer"]["multivariate_stages"]["options"]["polish"] = True
    assert _status(protocol, tmp_path, "CP-07") is CheckStatus.FAIL


def test_cp07_fails_when_non_convergence_is_not_a_legal_outcome(protocol, tmp_path):
    """NOT_CONVERGED 不合法的話，唯一出路就是放寬門檻重跑。"""
    protocol["convergence"]["non_convergence"]["is_legal_terminal_state"] = False
    assert _status(protocol, tmp_path, "CP-07") is CheckStatus.FAIL


def test_cp07_fails_without_a_tie_break_rule(protocol, tmp_path):
    protocol["tie_break"]["rule"] = ""
    assert _status(protocol, tmp_path, "CP-07") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# CP-08 資料來源
# ---------------------------------------------------------------------------


def test_cp08_fails_when_the_calibration_split_hash_does_not_match_the_lock(
    protocol, tmp_path
):
    protocol["data"]["calibration_set_hash"] = "0" * 64
    assert _status(protocol, tmp_path, "CP-08") is CheckStatus.FAIL


def test_cp08_fails_when_the_raw_data_hash_is_precomputed(protocol, tmp_path):
    """現在就算得出 raw_data_hash，代表已經讀過 calibration partition。"""
    protocol["data"]["raw_data_hash"] = "a" * 64
    assert _status(protocol, tmp_path, "CP-08") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# CP-10 / CP-11 artifact 與副作用監看
# ---------------------------------------------------------------------------


def test_cp10_fails_when_evaluations_are_not_logged(protocol, tmp_path):
    protocol["artifacts"]["per_evaluation"]["fields"] = ["evaluation_index"]
    assert _status(protocol, tmp_path, "CP-10") is CheckStatus.FAIL


def test_cp11_fails_without_a_regression_guard(protocol, tmp_path):
    """只看被最佳化的項，就看不到「修好 signal、弄壞 distance」。"""
    protocol["reporting"]["regression_guard"]["regression_tolerance"] = None
    assert _status(protocol, tmp_path, "CP-11") is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# 凍結
# ---------------------------------------------------------------------------


def test_freeze_is_refused_when_any_check_fails(protocol, tmp_path):
    protocol["stagewise"][0]["parameters"].append("lighting.irradiance")
    spec = _write(tmp_path, protocol)
    freeze_dir = tmp_path / "freeze"
    with pytest.raises(PreregistrationError, match="not freezable"):
        freeze_preregistration(spec, freeze_dir, REPO_ROOT)
    assert not (freeze_dir / "preregistrations").exists()


def test_freeze_binds_every_required_hash(tmp_path):
    """凍結記錄必須綁住 protocol / split / registry / lock / erratum / optimizer。"""
    freeze_dir = tmp_path / "freeze"
    freeze_dir.mkdir()
    for name in ("real_split_policy.lock.json", "initial_simulation.lock.json"):
        (freeze_dir / name).write_bytes((FREEZE_DIR / name).read_bytes())
    errata_dir = freeze_dir / "errata"
    errata_dir.mkdir()
    (errata_dir / "ERR-001.erratum.json").write_bytes(
        (FREEZE_DIR / "errata" / "ERR-001.erratum.json").read_bytes()
    )
    amendments_dir = freeze_dir / "amendments"
    amendments_dir.mkdir()
    for amendment in sorted((FREEZE_DIR / "amendments").glob("*.amendment.json")):
        (amendments_dir / amendment.name).write_bytes(amendment.read_bytes())

    _, document = freeze_preregistration(
        PREREGISTRATION_PATH, freeze_dir, REPO_ROOT, code_version="deadbeef"
    )
    payload = document["payload"]
    assert payload["protocol_hash"]
    assert payload["code_version"] == "deadbeef"
    assert payload["split"]["calibration_set_hash"]
    assert payload["split"]["heldout_access_count"] == 0
    assert payload["registry"]["parameter_set_hash"]
    assert (
        payload["initial_simulation"]["lock_hash"]
        == "dc15c9543a3aecacafcd1cbc110c48fc5fdd08d28457d4859d2a0bf573cb8335"
    )
    assert "ERR-001" in payload["errata"]
    assert payload["optimizer"]["optimizer_seed"] == 20260829
    assert len(payload["stagewise"]) == 5


def test_refreezing_is_refused(tmp_path):
    freeze_dir = tmp_path / "freeze"
    freeze_dir.mkdir()
    for name in ("real_split_policy.lock.json", "initial_simulation.lock.json"):
        (freeze_dir / name).write_bytes((FREEZE_DIR / name).read_bytes())
    errata_dir = freeze_dir / "errata"
    errata_dir.mkdir()
    (errata_dir / "ERR-001.erratum.json").write_bytes(
        (FREEZE_DIR / "errata" / "ERR-001.erratum.json").read_bytes()
    )
    amendments_dir = freeze_dir / "amendments"
    amendments_dir.mkdir()
    for amendment in sorted((FREEZE_DIR / "amendments").glob("*.amendment.json")):
        (amendments_dir / amendment.name).write_bytes(amendment.read_bytes())

    freeze_preregistration(PREREGISTRATION_PATH, freeze_dir, REPO_ROOT)
    with pytest.raises(PreregistrationError, match="already frozen"):
        freeze_preregistration(PREREGISTRATION_PATH, freeze_dir, REPO_ROOT)


# ---------------------------------------------------------------------------
# CP-13..CP-16（AMD-003）
# ---------------------------------------------------------------------------


def test_cp13_fails_when_the_budget_table_is_hand_edited(protocol, tmp_path):
    """預算表由公式導出；手改一個數字就代表預註冊與程式分家了。"""
    protocol["evaluation_budget"]["resolved"]["SCENE_GEOMETRY_SURFACE_FOIL"]["per_stage"] = 3000
    assert _status(protocol, tmp_path, "CP-13") is CheckStatus.FAIL


def test_cp13_fails_when_the_total_does_not_add_up(protocol, tmp_path):
    protocol["evaluation_budget"]["total_evaluations"] = 3000
    assert _status(protocol, tmp_path, "CP-13") is CheckStatus.FAIL


def test_cp13_fails_when_maxiter_changes_without_re_deriving(protocol, tmp_path):
    """改 maxiter 卻不重導預算表，正是 CAL-PREREG-001 的那個缺陷。"""
    protocol["optimizer"]["multivariate_stages"]["maxiter"] = 200
    assert _status(protocol, tmp_path, "CP-13") is CheckStatus.FAIL


def test_cp14_fails_when_a_string_bound_loses_its_interpretation(protocol, tmp_path):
    protocol["bounds"]["declared_numeric_interpretations"] = []
    assert _status(protocol, tmp_path, "CP-14") is CheckStatus.FAIL


def test_cp14_fails_when_the_resolver_is_not_named(protocol, tmp_path):
    """沒指名唯一的解析路徑，就可能有第二條路徑算出第二套 bounds。"""
    protocol["bounds"]["uniqueness"]["resolver"] = ""
    assert _status(protocol, tmp_path, "CP-14") is CheckStatus.FAIL


def test_cp15_fails_when_stage_0_goes_back_to_s_f(protocol, tmp_path):
    """把 s_f 放回 stage 0 的判準，simulation-only 的宣告就再次變成假的。"""
    protocol["stage_0"]["admission_threshold"]["rule"] = (
        "leverage(p, o) = |delta o| / s_f(o); admit if >= 0.5"
    )
    assert _status(protocol, tmp_path, "CP-15") is CheckStatus.FAIL


def test_cp15_fails_when_the_normaliser_is_not_sigma_mc(protocol, tmp_path):
    protocol["stage_0"]["normaliser"]["symbol"] = "s_f"
    assert _status(protocol, tmp_path, "CP-15") is CheckStatus.FAIL


def test_cp15_fails_when_stage_0_declares_it_reads_calibration(protocol, tmp_path):
    protocol["stage_0"]["reads_calibration_partition"] = True
    assert _status(protocol, tmp_path, "CP-15") is CheckStatus.FAIL


def test_cp15_fails_when_the_s_f_diagnostic_becomes_gating(protocol, tmp_path):
    """事後依真實資料剔除參數，就是資料相依的模型選擇。"""
    protocol["s_f_relative_leverage_diagnostic"]["gating"] = True
    assert _status(protocol, tmp_path, "CP-15") is CheckStatus.FAIL


def test_cp15_allows_explaining_why_s_f_was_rejected(protocol, tmp_path):
    """說明性欄位提到 s_f 不算違規 —— 否則等於逼人刪掉理由才能過關。"""
    assert "s_f" in protocol["stage_0"]["normaliser"]["why_not_s_f"]
    assert _status(protocol, tmp_path, "CP-15") is CheckStatus.PASS


def test_cp16_fails_once_the_calibration_partition_has_been_read(protocol, tmp_path):
    """帳上出現第一筆讀取之後，預註冊就不再是「先寫完再讀」。"""
    fake_root = tmp_path / "root"
    (fake_root / "data" / "splits").mkdir(parents=True)
    (fake_root / "data" / "splits" / "calibration_access_ledger.json").write_text(
        json.dumps({"calibration_access_count": 1, "entries": [{"purpose": "s_f"}]}),
        encoding="utf-8",
    )
    (fake_root / "pcmef").mkdir()
    # ERR-001 的證據以 repo_root 為基準，因此假根目錄也要有那兩份 manifest。
    for run in ("repro_a", "repro_b"):
        source = REPO_ROOT / "outputs" / run / "simulation_smoke_manifest.json"
        target = fake_root / "outputs" / run
        target.mkdir(parents=True)
        (target / source.name).write_bytes(source.read_bytes())

    report = validate_preregistration(_write(tmp_path, protocol), FREEZE_DIR, fake_root)
    assert report.get("CP-16").status is CheckStatus.FAIL


def test_amendment_003_is_frozen_and_bound():
    """協定宣稱依 AMD-003 修訂；那份修訂必須真的存在且雜湊可重算。"""
    from pcmef.core.amendments import AmendmentStore

    protocol = load_protocol()
    declared = [entry["id"] for entry in protocol["amendments"]]
    assert "AMD-003" in declared
    store = AmendmentStore(FREEZE_DIR)
    payload = store.load("AMD-003")
    assert payload["precondition_evidence"]["heldout_access_count"] == 0
    assert payload["precondition_evidence"]["calibration_access_count"] == 0
    assert payload["precondition_evidence"]["stage_0_executed"] is False
    assert payload["invariants_preserved"]["objective_unchanged"] is True


def test_freeze_is_refused_when_a_declared_amendment_is_missing(tmp_path):
    """引用一份不存在的 amendment，等於沒有修訂記錄。"""
    freeze_dir = tmp_path / "freeze"
    freeze_dir.mkdir()
    for name in ("real_split_policy.lock.json", "initial_simulation.lock.json"):
        (freeze_dir / name).write_bytes((FREEZE_DIR / name).read_bytes())
    errata_dir = freeze_dir / "errata"
    errata_dir.mkdir()
    (errata_dir / "ERR-001.erratum.json").write_bytes(
        (FREEZE_DIR / "errata" / "ERR-001.erratum.json").read_bytes()
    )
    # 刻意不複製 freeze/amendments/
    with pytest.raises(PreregistrationError, match="not frozen"):
        freeze_preregistration(PREREGISTRATION_PATH, freeze_dir, REPO_ROOT)


def test_no_calibration_artifact_exists_yet():
    """本 session 的紅線：預註冊凍結時不得已經跑過校準。"""
    from pcmef.audit.firewall import CALIBRATION_ARTIFACT_GLOBS

    outputs = REPO_ROOT / "outputs"
    for pattern in CALIBRATION_ARTIFACT_GLOBS:
        assert not list(outputs.glob(pattern))


def test_heldout_remains_sealed():
    import json

    registry = json.loads(
        (REPO_ROOT / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["heldout_access_count"] == 0


# ---------------------------------------------------------------------------
# 預註冊身分（AMD-004 P0-2）
# ---------------------------------------------------------------------------


def test_the_identity_comes_from_the_protocol_not_from_a_code_constant():
    """YAML 說 003、程式常數說 002 -> 不得凍出一份自稱 002 的記錄。"""
    from pcmef.experiments.calibration_prereg import declared_preregistration_id

    assert declared_preregistration_id({"preregistration_id": "CAL-PREREG-003"}) == (
        "CAL-PREREG-003"
    )
    assert declared_preregistration_id(load_protocol()) == "CAL-PREREG-003"


@pytest.mark.parametrize("bad", [None, "", "CAL-PREREG-3", "PREREG-003", "003"])
def test_a_malformed_or_missing_identity_is_refused(bad):
    protocol = {} if bad is None else {"preregistration_id": bad}
    with pytest.raises(PreregistrationError):
        from pcmef.experiments.calibration_prereg import declared_preregistration_id

        declared_preregistration_id(protocol)


def test_the_freeze_target_follows_the_protocol_id(protocol, tmp_path, monkeypatch):
    """freeze 檔名、payload 內的 id 與協定宣告的 id 必須是同一個值。

    先前三者由一個模組常數決定，於是把協定改成 CAL-PREREG-003 卻忘了改常數，
    會凍出「檔名與內容都寫 002、協定其實是 003」的記錄。
    """
    protocol["preregistration_id"] = "CAL-PREREG-009"
    path = _write(tmp_path, protocol)

    # 只驗身分推導，不重跑整套 CP 檢查（那需要完整 freeze 目錄）。
    from pcmef.experiments.calibration_prereg import declared_preregistration_id

    assert declared_preregistration_id(load_protocol(path)) == "CAL-PREREG-009"

    # 而且 001/002 必須永遠留著（append-only）。
    frozen = REPO_ROOT / "freeze" / "preregistrations"
    assert (frozen / "CAL-PREREG-001.prereg.json").exists()
    assert (frozen / "CAL-PREREG-002.prereg.json").exists()


def test_frozen_records_are_self_consistent_about_their_own_identity():
    """已凍結的每一份記錄，檔名 / 文件 id / payload id 三者必須一致。"""
    frozen = REPO_ROOT / "freeze" / "preregistrations"
    for path in sorted(frozen.glob("CAL-PREREG-*.prereg.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        stem = path.name.split(".")[0]
        assert document["preregistration_id"] == stem
        assert document["payload"]["preregistration_id"] == stem


# ---------------------------------------------------------------------------
# CP-02 fail closed（AMD-004 P0-C）
# ---------------------------------------------------------------------------


def test_cp02_does_not_pass_when_the_expansion_check_cannot_run(monkeypatch):
    """算不出展開維度時，CG gauge 固定項有沒有溜回搜尋空間就是未知的。

    先前這裡 catch Exception 後仍回 PASS，只在 detail 註明「未比對」——
    於是一個讀不到 lock 的環境會讓 CP-02 整條靜默失效，而那正是它唯一的用途。
    """
    from pcmef.experiments import calibration_prereg as module

    real = module.resolve_numeric_bounds
    calls = {"n": 0}

    def boom_once(*args, **kwargs):
        # 只讓 CP-02 那一次呼叫失敗。CP-14 也用同一個函式，
        # 全域打壞它會變成「驗證器整個炸掉」，那不是本條要驗的失敗模式。
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("lock unreadable")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "resolve_numeric_bounds", boom_once)
    report = validate_preregistration(PREREGISTRATION_PATH, FREEZE_DIR, REPO_ROOT)
    result = report.get("CP-02")
    assert result.status is not CheckStatus.PASS
    assert result.status is CheckStatus.BLOCKED
    assert any("lock unreadable" in f for f in result.findings)


def test_a_blocked_cp02_makes_the_preregistration_unfreezable():
    """BLOCKED 不是「還沒產出」的同義詞；它必須擋住凍結。"""
    from pcmef.audit.result import CheckStatus as CS

    report = validate_preregistration(PREREGISTRATION_PATH, FREEZE_DIR, REPO_ROOT)
    # 基準線：正常情況下 CP-02 是 PASS，否則下面的推論沒有意義。
    assert report.get("CP-02").status is CS.PASS
    assert "CP-02" in REQUIRED_CHECKS
