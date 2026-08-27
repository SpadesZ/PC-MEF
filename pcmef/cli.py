# PC-MEF Research System source maintenance contract
# 上下游: 由使用者終端機與 CI 呼叫；讀取 configs/ 下的 YAML 與 freeze/ 下的 lock 檔，
#         寫出人類可讀報告到 stdout 與選用的 log 檔；exit code 供 CI 判定。
# 檔案路徑: pcmef/cli.py
# 產生時間: 2026-08-26 09:50 +08:00
# 版本: v0.7.0
# 功能說明: 系統的命令列入口。提供查版本、檢視設定、列出所有待教授裁決的數值、
#           顯示每個凍結點的狀態、執行 M0 前研究資料盤點、判定 Sigma scaling 與
#           取樣間隔這兩處來源歧異，以及管理 LLM 連線／模型驗證／角色綁定與快照。
# 模組定位: 所有 formal run 的唯一進入點。它「不是」互動式工具 —— formal 路徑
#           不得依賴任何鍵盤輸入，才能在無人值守的環境重現。
# 主要責任:
#   1. build_parser() 定義全域參數與子指令樹
#   2. cmd_config_show() 顯示設定來源、hash 與待裁決數量
#   3. cmd_config_check() 逐項列出待裁決數值與其出處
#   4. cmd_audit_real_data() 執行 M0 盤點並產出四份 artifact 與五層計數
#   5. cmd_provenance_resolve_sigma() 產出 E1-G08 的 Sigma 證據 artifact
#   6. cmd_provenance_audit_timing() 並列三個取樣間隔來源並量化偏差
#   7. cmd_sim_smoke() 於子行程跑模擬並以 manifest 判定成敗（NOTE-012）
#   8. cmd_locks_status() 顯示每個 lock 為 frozen / pending / BLOCKED
#   9. cmd_llm_*() 走 AdminService 完成連線／取模型／驗能力／改綁的全流程
#  10. cmd_llm_snapshot() 解析 draft 成 lock candidate，前提齊備才允許 --freeze
#  11. cmd_admin_serve() 啟動本機管理頁面，非 loopback 由 auth 層擋下
#  12. main() 統一把 ConfigError / LockError / LegacyCSVError / FormalBlockingError
#      轉成 exit code
# 維護提醒:
#   - 不得接受 API key 作為命令列參數；secret 一律走環境變數或 secret vault，
#     命令列參數會留在 shell history 與 process list。
#   - 不得讓 formal 路徑依賴互動輸入或 CLI override。
#   - audit real-data 只有在 config 已把 sigma_status 凍結時才可讀它；
#     未凍結時必須由 CLI 旗標提供，否則會形成「M0 需要它、而它來自 M0」的循環。
#   - 新增子指令時同步更新 README 的常用指令表。
#   - 不得為 provenance resolve-sigma 增加「自動挑一個 register」的選項；
#     暫存器身分只能由採集腳本證據決定（NOTE-010）。
#   - 不得讓 llm snapshot 在有 blocking 前提時仍寫出 lock；那等於用 CLI
#     繞過 freeze，與 UI 繞過 freeze 是同一件事（§52 結語）。
#   - 不得為 llm connection add 增加接受 API key 明文的參數；命令列參數會
#     留在 shell history 與 process list。
#   - v0.1.0 新增：version / config show / config check / locks status 四組指令。
#   - v0.2.0 新增：audit real-data（M0 盤點）。
#   - v0.3.0 新增：provenance resolve-sigma / audit-timing。
#   - v0.4.0 新增：sim smoke（子行程隔離，見 NOTE-012）。
#   - v0.5.0 新增：audit real-data --source-format edge-impulse；nominal 計數與
#     已凍結的 sigma_status 皆由 config 帶入。
#   - v0.6.0 新增：llm connection/model/binding/snapshot 與 admin serve。
#     snapshot 只在 --freeze 且無 blocking 前提時才寫 lock（NOTE-020）。
# 驗證方式:
#   - py -3.10 -m pytest tests/test_cli.py -v
#   - py -3.10 -m pytest tests/llm_admin/test_cli_llm_flow.py -v
#   - py -3.10 -m pcmef.cli config check
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from pcmef import __version__
from pcmef.core.config import ConfigError, FormalBlockingError, load_config
from pcmef.core.locks import LOCK_SPECS, LockError, LockStore
from pcmef.core.logging_setup import setup_logging

DEFAULT_CONFIG = Path("configs/base.yaml")


def _parse_overrides(pairs: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ConfigError(f"--set expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        overrides[key.strip()] = value
    return overrides


def _load(args: argparse.Namespace):
    paths = [Path(p) for p in (args.config or [DEFAULT_CONFIG])]
    return load_config(paths, overrides=_parse_overrides(args.set), formal=args.formal)


# ---------------------------------------------------------------------------
# 子指令
# ---------------------------------------------------------------------------


def cmd_version(_: argparse.Namespace) -> int:
    print(f"pcmef {__version__}")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    config = _load(args)
    print(f"sources: {', '.join(config.sources)}")
    if args.key:
        print(f"{args.key} = {config.get(args.key)!r}")
        return 0
    pending = config.unresolved()
    print(f"formal mode: {config.formal}")
    print(f"config hash: {config.config_hash()}")
    print(f"values awaiting advisor approval: {len(pending)}")
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    """列出所有待裁決數值。--formal 時只要還有殘留就以非零 exit code 結束。"""
    try:
        config = _load(args)
    except FormalBlockingError as error:
        print(str(error), file=sys.stderr)
        return 2

    pending = sorted(config.unresolved(), key=lambda item: item.key)
    if not pending:
        print("All config values are resolved.")
        return 0

    print(f"{len(pending)} config value(s) await advisor approval:\n")
    for item in pending:
        print(f"  {item.key}")
        if item.source:
            print(f"      source: {item.source}")
        if item.reason:
            print(f"      reason: {item.reason}")
    print(
        "\nThese must not be defaulted by the implementation "
        "(SRC-PLAN Appendix A / SRC-SAI Appendix F; see NOTES.md NOTE-005)."
    )
    return 0


def cmd_audit_real_data(args: argparse.Namespace) -> int:
    """M0 盤點：掃描前研究 CSV，產出 inventory / alignment / exclusion ledger / report。

    sigma_status 刻意由 CLI 參數提供而非讀 config：M0 盤點正是用來產生
    Sigma register 證據的步驟，若它反過來要求 config 先填好 sigma 狀態，
    就成了循環相依。config 裡的 sigma_provenance 是 E1 階段的凍結決策，
    與這裡的工作狀態分屬兩件事。
    """
    config = _load(args)
    nominal = config.get("real_anchors.nominal_logical_recordings", 0)

    # sigma_status 預設沿用 CLI 旗標；但若 config 已把它凍結為 RESOLVED，
    # 讀取那個「已完成的決策」不構成循環——循環是「M0 需要它、而它來自 M0」，
    # 決策產出並凍結之後，再讀它只是引用既有結論。
    sigma_status = args.sigma_status
    if sigma_status == "UNRESOLVED":
        frozen = config.get("sigma_provenance.status", None)
        if frozen in ("RESOLVED", "UNRESOLVED"):
            sigma_status = frozen

    if args.source_format == "edge-impulse":
        from pcmef.adapters.edge_impulse import EdgeImpulseAdapter

        adapter = EdgeImpulseAdapter(
            sigma_status=sigma_status,
            nominal_logical_recordings=int(nominal),
            nominal_source=", ".join(config.sources),
        )
    else:
        from pcmef.adapters.legacy_csv import LegacyCSVAdapter, LegacyCSVConfig

        adapter = LegacyCSVAdapter(
            LegacyCSVConfig(
                key_strategy=args.key_strategy,
                sigma_status=sigma_status,
                nominal_logical_recordings=int(nominal),
                nominal_source=", ".join(config.sources),
                formal=args.formal,
            )
        )

    inventory = adapter.build_inventory(args.source)
    report = adapter.audit_alignment(inventory)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_csv(
        out_dir / "source_inventory.csv",
        [item.to_row() for item in inventory],
    )
    _write_csv(
        out_dir / "measurement_alignment.csv",
        [row for m in report.aligned for row in m.to_alignment_rows()],
    )
    _write_csv(
        out_dir / "exclusion_ledger.csv",
        [entry.to_row() for entry in report.exclusions],
    )

    counts = report.counts
    audit_report = {
        "source_root": report.source_root,
        "key_strategy": report.key_strategy,
        "sigma_status": sigma_status,
        "nominal_source": report.nominal_source,
        "counts": {
            "nominal_logical_recordings": counts.nominal_logical_recordings,
            "physical_source_files": counts.physical_source_files,
            "canonical_recordings": counts.canonical_recordings,
            "valid_recordings": counts.valid_recordings,
            "e1_eligible_recordings": counts.e1_eligible_recordings,
        },
        "aligned_by_class": report.by_class(),
        "exclusions_by_reason": report.exclusion_summary(),
        "config_hash": config.config_hash(),
    }
    (out_dir / "audit_report.json").write_text(
        json.dumps(audit_report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"source root : {report.source_root}")
    print(f"key strategy: {report.key_strategy}")
    print("\ncounts (SRC-SAI §7.8 five-layer ledger)")
    print(f"  nominal_logical_recordings : {counts.nominal_logical_recordings}"
          f"   [{report.nominal_source}]")
    print(f"  physical_source_files      : {counts.physical_source_files}")
    print(f"  canonical_recordings       : {counts.canonical_recordings}")
    print(f"  valid_recordings           : {counts.valid_recordings}")
    print(f"  e1_eligible_recordings     : {counts.e1_eligible_recordings}")
    print(f"\naligned by class: {report.by_class()}")
    summary = report.exclusion_summary()
    if summary:
        print("\nexclusions:")
        for reason, count in summary.items():
            print(f"  {reason:<32} {count}")
    else:
        print("\nexclusions: none")
    print(f"\nartifacts written to {out_dir.resolve()}")

    if counts.e1_eligible_recordings == 0:
        print(
            "\nnote: e1_eligible_recordings is 0. Four-feature E1 stays blocked by "
            "E1-G08 until the Sigma register provenance is resolved "
            "(see docs/NOTES.md NOTE-010).",
            file=sys.stderr,
        )
    return 0


def cmd_e1_metrics_evidence(args: argparse.Namespace) -> int:
    """E1-G06：跑度量單元測試並產出 tests/e1_metrics.xml。

    §12 把 G06 的證據定義為 `tests/e1_metrics.xml` —— 也就是一份 JUnit 報告。
    把它做成指令而非要人記得加 --junitxml，是因為 gate 的證據不該取決於
    某個人當下有沒有打對參數。
    """
    import subprocess

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "pytest", args.tests_dir, "-q",
        f"--junitxml={target}",
    ]
    completed = subprocess.run(command, check=False)
    if not target.exists():
        print(
            f"error: pytest produced no report at {target}", file=sys.stderr
        )
        return 1
    print(f"E1-G06 evidence: {target.resolve()}")
    if completed.returncode != 0:
        print(
            "note: the metric tests FAILED; the artifact exists but E1-G06 must "
            "not be treated as satisfied (see `pcmef audit e1-gates`).",
            file=sys.stderr,
        )
    return completed.returncode


def _emit_audit(report, out_dir: str | None, filename: str, required=None) -> int:
    """共用：印出稽核報告、選擇性落盤、依 required 決定 exit code。"""
    print(f"audit: {report.name}")
    for line in report.lines():
        print(line)

    counts = report.counts()
    print(
        f"\nPASS {counts['PASS']}  FAIL {counts['FAIL']}  "
        f"NOT_PRODUCED {counts['NOT_PRODUCED']}  BLOCKED {counts['BLOCKED']}"
    )

    if out_dir:
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        path = target / filename
        path.write_text(
            json.dumps(report.to_artifact(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"artifact: {path.resolve()}")

    if counts["NOT_PRODUCED"] and required is None:
        print(
            "\nnote: NOT_PRODUCED 代表證據尚未產出，不是失敗。稽核器刻意先於"
            "被稽核的產物存在，才不會在事後被寫成剛好符合已產出的結果。",
            file=sys.stderr,
        )

    if required is not None:
        unmet = report.unmet(required)
        if unmet:
            print(f"\n{len(unmet)} required check(s) not PASS:", file=sys.stderr)
            for result in unmet:
                print(
                    f"  - {result.identifier} [{result.status.value}] "
                    f"{result.requirement}",
                    file=sys.stderr,
                )
    return report.exit_code(required)


def cmd_audit_e1_gates(args: argparse.Namespace) -> int:
    """Batch 7：逐項檢查 E1 開跑前的十二個 gate。

    不帶 --require 時只是盤點，尚未產出的 gate 回報 NOT_PRODUCED 而非失敗；
    帶 --require 時，被指名的 gate 只要不是 PASS 就以非零 exit code 中斷。
    """
    from pcmef.audit.e1_gates import AuditPaths, audit_e1_gates, parse_gate_range

    report = audit_e1_gates(
        AuditPaths(
            inventory=Path(args.inventory), splits=Path(args.splits),
            simulation=Path(args.simulation), surrogate=Path(args.surrogate),
            provenance=Path(args.provenance), freeze=Path(args.freeze_dir),
            tests=Path(args.tests),
        )
    )
    required = parse_gate_range(args.require) if args.require else None
    return _emit_audit(report, args.out, "e1_gate_audit.json", required)


def cmd_audit_heldout_firewall(args: argparse.Namespace) -> int:
    """Batch 7：Appendix B 中與 real split 相關的洩漏防線。"""
    from pcmef.audit.firewall import SplitAuditPaths, audit_heldout_firewall

    report = audit_heldout_firewall(
        SplitAuditPaths(splits=args.splits, freeze=args.freeze_dir, outputs=args.outputs)
    )
    return _emit_audit(report, args.out, "heldout_firewall_audit.json")


def cmd_audit_real_split_policy(args: argparse.Namespace) -> int:
    """Batch 7：Appendix H1 Real Split Policy Contract。"""
    from pcmef.audit.firewall import SplitAuditPaths, audit_real_split_policy

    report = audit_real_split_policy(
        SplitAuditPaths(splits=args.splits, freeze=args.freeze_dir, outputs=args.outputs)
    )
    return _emit_audit(report, args.out, "real_split_policy_audit.json")


def cmd_sim_smoke(args: argparse.Namespace) -> int:
    """M1：跑最小模擬場景並產出 E1-G03 的 smoke manifest。

    NOTE(NOTE-012): 實際算圖在子行程執行，父行程不載入 mitsuba。
    drjit/mitsuba 在連續算多場景後會於 Windows DLL detach 階段崩潰，
    讓已完成的 run 回報成失敗；隔離到子行程後，父行程改以 manifest 判定成敗，
    子行程的 raw exit code 仍完整回報，不被藏起來。
    """
    if not args.in_worker:
        return _run_sim_worker(args)
    import yaml

    from pcmef.simulation.controller import ScenarioStatus, SimulationController
    from pcmef.simulation.scenario import (
        Geometry,
        Lighting,
        ScenarioConfig,
        ScenarioConfigError,
    )

    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    block = raw.get("simulation", raw)
    shared = {
        "geometry": Geometry(**(block.get("geometry") or {})),
        "lighting": Lighting(**(block.get("lighting") or {})),
        "spp": int(block.get("spp", 16)),
        "resolution": tuple(block.get("resolution", (64, 64))),
    }
    variant = args.variant or block.get("variant", "llvm_ad_rgb")
    temporal_bins = int(args.temporal_bins or block.get("temporal_bins", 256))

    controller = SimulationController(variant)
    runs = []
    for index, entry in enumerate(block.get("scenarios", []), start=1):
        try:
            config = ScenarioConfig(
                class_label=str(entry["class_label"]),
                seed=int(entry["seed"]),
                medium_parameters=dict(entry.get("medium") or {}),
                formal=args.formal,
                **shared,
            )
        except ScenarioConfigError as error:
            print(f"error: scenario {index}: {error}", file=sys.stderr)
            return 2
        scenario_id = f"smoke_{config.medium_preset.value}_{config.seed:04d}"
        print(f"rendering {scenario_id} ...", flush=True)
        runs.append(
            controller.run_scenario(scenario_id, config, args.out, temporal_bins)
        )

    manifest_path = controller.write_manifest(runs, args.out)

    print()
    for run in runs:
        mark = "ok " if run.status == ScenarioStatus.OK else "FAIL"
        detail = ""
        if run.transient is not None:
            detail = (
                f"transient {tuple(run.transient.shape)} "
                f"bin={run.transient.bin_width_s:.3e}s "
                f"energy={run.transient.extra['total_energy']:.1f}"
            )
        elif run.error:
            detail = run.error
        print(f"  [{mark}] {run.scenario_id:<28} {detail}")

    failed = sum(1 for r in runs if r.status == ScenarioStatus.FAILED)
    print(f"\nmanifest: {manifest_path.resolve()}")
    if failed:
        print(f"{failed} scenario(s) FAILED; see stderr.txt in each folder", file=sys.stderr)
        return 1
    return 0


def _build_real_split(args: argparse.Namespace):
    """共用：盤點 -> 依政策規劃 real split。回傳 (config, report, plan)。"""
    from pcmef.core.splits import GroupRule, RealSplitPolicy, plan_real_split

    config = _load(args)
    if args.source_format == "edge-impulse":
        from pcmef.adapters.edge_impulse import EdgeImpulseAdapter

        adapter = EdgeImpulseAdapter(
            sigma_status=config.get("sigma_provenance.status", "UNRESOLVED"),
            nominal_logical_recordings=int(
                config.get("real_anchors.nominal_logical_recordings", 0)
            ),
            nominal_source=", ".join(config.sources),
        )
    else:
        from pcmef.adapters.legacy_csv import LegacyCSVAdapter, LegacyCSVConfig

        adapter = LegacyCSVAdapter(
            LegacyCSVConfig(sigma_status=config.get("sigma_provenance.status", "UNRESOLVED"))
        )

    report = adapter.audit_alignment(adapter.build_inventory(args.source))
    if report.counts.e1_eligible_recordings == 0:
        print(
            "error: no e1-eligible recordings; the split must not be planned on "
            "data that cannot enter E1 (see E1-G08)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # 以 class 分組收集 e1-eligible 的 recording ID。
    eligible: dict[str, list[str]] = {}
    for measurement in report.aligned:
        eligible.setdefault(measurement.class_label, []).append(
            f"{measurement.condition}/measurement_{measurement.measurement_key}"
        )

    allocation = config.get("real_split_policy.allocation")
    policy = RealSplitPolicy(
        calibration_ratio=float(allocation["calibration"]),
        heldout_ratio=float(allocation["heldout_real"]),
        minimum_per_class=int(config.get("real_split_policy.minimum_per_class")),
        seed=int(config.get("real_split_policy.seed")),
        group_rule=GroupRule(config.get("real_split_policy.resolved_group_rule")),
        group_rule_evidence=str(config.get("real_split_policy.group_rule_evidence")),
    )
    return config, report, plan_real_split(eligible, policy)


def cmd_split_plan_real(args: argparse.Namespace) -> int:
    """E1-G02：規劃 real split 並寫出 split_registry.json。"""
    _, _, plan = _build_real_split(args)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    registry = plan.to_registry()
    (out_dir / "split_registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"group rule : {registry['group_rule']}")
    print(f"seed       : {registry['seed']}")
    print("\n%-14s %10s %13s %13s" % ("class", "eligible", "calibration", "heldout_real"))
    for cls, counts in registry["counts"].items():
        print(
            "%-14s %10d %13d %13d"
            % (cls, counts["eligible"], counts["calibration"], counts["heldout_real"])
        )
    totals = registry["totals"]
    print(
        "%-14s %10d %13d %13d"
        % ("TOTAL", totals["eligible"], totals["calibration"], totals["heldout_real"])
    )
    print(f"\nid collisions       : {registry['collision_count']}")
    print(f"heldout access count: {registry['heldout_access_count']}")
    print(f"\nartifact: {(out_dir / 'split_registry.json').resolve()}")
    return 0


def cmd_freeze_real_split_policy(args: argparse.Namespace) -> int:
    """E1-G09：把 real split policy 凍結成不可變更的 lock。"""
    _, _, plan = _build_real_split(args)
    store = LockStore(args.freeze_dir)
    path = store.write("real_split_policy", plan.to_lock_payload())

    payload = store.load("real_split_policy")
    print("real_split_policy.lock frozen")
    print(f"  path                 : {path.resolve()}")
    print(f"  payload hash         : {store.load_hash('real_split_policy')}")
    print(f"  eligible_set_hash    : {payload['eligible_set_hash'][:16]}")
    print(f"  calibration_set_hash : {payload['calibration_set_hash'][:16]}")
    print(f"  heldout_real_set_hash: {payload['heldout_real_set_hash'][:16]}")
    print(f"  totals               : {payload['totals']}")
    print(f"  redraw policy        : {payload['redraw_policy']}")
    print(
        "\nheld-out is now sealed: it may only be opened once, for E1 final "
        "evaluation. Re-running this command with different data or seed will be "
        "refused (locks are immutable)."
    )
    return 0


def cmd_surrogate_smoke(args: argparse.Namespace) -> int:
    """E1-G04：驗證 surrogate 能由真實 transient 產出四特徵且無 NaN/Inf。

    產出 surrogate_smoke.csv：每個場景一列，含四特徵的均值與標準差、
    物理量與 recording 形狀，供 E1-G04 判定。
    """
    import numpy as np

    from pcmef.core.constants import TOF_SCHEMA
    from pcmef.surrogate.calibration import PLACEHOLDER_SMOKE_CALIBRATION
    from pcmef.surrogate.single_acquisition import SensorSurrogate
    from pcmef.surrogate.temporal_model import TemporalModel

    sim_root = Path(args.simulation_out)
    scenarios = (
        sorted(p for p in sim_root.iterdir() if p.is_dir()) if sim_root.is_dir() else []
    )
    if not scenarios:
        print(
            f"error: no simulation scenarios under {sim_root}; run `sim smoke` first",
            file=sys.stderr,
        )
        return 1

    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    model = TemporalModel(surrogate)
    rows: list[dict[str, object]] = []
    non_finite = 0

    for scenario in scenarios:
        transient_path = scenario / "transient.npy"
        axis_path = scenario / "transient_time.npy"
        if not (transient_path.exists() and axis_path.exists()):
            continue
        transient = np.load(transient_path)
        axis = np.load(axis_path)

        observables = surrogate.observe(transient, axis)
        recording = model.generate_recording(
            transient, axis,
            sample_interval_s=args.sample_interval_s,
            sample_interval_source=args.sample_interval_source,
            seed=args.seed, n_samples=args.n_samples,
        )
        finite = bool(np.all(np.isfinite(recording.values)))
        if not finite:
            non_finite += 1

        row: dict[str, object] = {
            "scenario_id": scenario.name,
            "n_samples": recording.n_samples,
            "duration_s": round(recording.duration_s, 4),
            "fwhm_s": observables.fwhm_s,
            "snr": observables.snr,
            "multipath_prominence": observables.multipath_prominence,
        }
        for index, metric in enumerate(TOF_SCHEMA):
            row[f"{metric}_mean"] = float(recording.values[:, index].mean())
            row[f"{metric}_sd"] = float(recording.values[:, index].std())
        row["all_finite"] = finite
        rows.append(row)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "surrogate_smoke.csv", rows)

    print(f"scenarios: {len(rows)}   non-finite: {non_finite}")
    for row in rows:
        summary = " ".join(
            f"{m.split('_')[0]}={row[f'{m}_mean']:.4g}" for m in TOF_SCHEMA
        )
        print(
            "  [%s] %-24s %s"
            % ("ok " if row["all_finite"] else "FAIL", row["scenario_id"], summary)
        )
    print(f"\nartifact: {(out_dir / 'surrogate_smoke.csv').resolve()}")
    print(
        "\nnote: calibration scales are placeholders; these values are structural "
        "evidence for E1-G04 only and carry no fidelity claim (E1-G12).",
        file=sys.stderr,
    )
    return 1 if non_finite else 0


def _run_sim_worker(args: argparse.Namespace) -> int:
    """在子行程執行模擬，並以 manifest 而非子行程 exit code 判定成敗。

    子行程的 exit code 仍會被檢查與回報：若它非零但 manifest 顯示全部成功，
    視為已知的 teardown 崩潰並明確標示；若 manifest 本身缺失或有 FAILED，
    則照實回報失敗。「忽略 exit code」與「隱瞞 exit code」是兩回事。
    """
    import subprocess

    manifest_path = Path(args.out) / "simulation_smoke_manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()

    command = [
        sys.executable, "-m", "pcmef.cli", "sim", "smoke",
        "--config", str(args.config),
        "--out", str(args.out),
        "--in-worker",
    ]
    if args.variant:
        command += ["--variant", args.variant]
    if args.temporal_bins:
        command += ["--temporal-bins", str(args.temporal_bins)]
    if args.formal:
        command.insert(3, "--formal")

    completed = subprocess.run(command, check=False)
    worker_code = completed.returncode

    if not manifest_path.exists():
        if worker_code != 0:
            # worker 在產出 manifest 之前就失敗了，它的 exit code 帶有意義
            # （例如 2 = formal-blocking 的設定問題），必須原樣傳遞而非壓平成 1，
            # 否則呼叫端分不出「設定不合法」與「算圖失敗」。
            return worker_code
        print("error: worker exited cleanly but wrote no manifest", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    counts = manifest.get("counts", {})
    failed = int(counts.get("failed", 0))

    if worker_code != 0:
        print(
            f"\nnote: the render worker exited with {worker_code} after all artifacts "
            "were written. This is the known drjit/mitsuba DLL-detach crash "
            "(see docs/NOTES.md NOTE-012); success is judged from the manifest.",
            file=sys.stderr,
        )
    if failed:
        print(f"error: {failed} scenario(s) FAILED", file=sys.stderr)
        return 1
    return 0


def cmd_provenance_resolve_sigma(args: argparse.Namespace) -> int:
    """SRC-D01/D02：以真實 Sigma 觀測值判定 scaling，並記錄 register 是否仍未決。

    刻意不提供「指定 register」以外的捷徑：暫存器身分無法由數值反推，
    唯一能解決它的是產生這批資料的採集腳本（NOTE-010）。
    """
    import numpy as np

    from pcmef.core.constants import (
        SIGMA_RAW_SCALE_DIVISOR,
        SIGMA_REGISTER_CANDIDATES,
        tof_index,
    )
    from pcmef.provenance.sigma import resolve_sigma, rule_out_range_register

    ruled_out: dict[int, str] = {}
    exclusion: dict[str, object] | None = None
    per_condition: dict[str, object] = {}

    if args.paired_source:
        # NOTE(NOTE-010): register 由**排除法**解出，而排除法需要同一列的
        # sigma 與 distance 配對。彙總格式已把時間軸摺成窗口統計量，
        # 兩個 metric 的列不再對應同一個時刻，因此只有逐 recording 的
        # Edge Impulse export 做得到這件事。
        from pcmef.adapters.edge_impulse import EdgeImpulseAdapter

        adapter = EdgeImpulseAdapter(sigma_status="UNRESOLVED")
        inventory = adapter.build_inventory(args.paired_source)
        stacked = adapter.stacked_values(inventory)
        values = stacked[:, tof_index("sigma_like")]
        distances = stacked[:, tof_index("distance_mm")]

        # NOTE(NOTE-028): 這個檢定證成的是 **channel semantics**
        # 「sigma 欄不是測距值」，而不是任何暫存器位址。AMD-001 之前它被錯用來
        # 「排除 0x1E 進而推得 0x18」—— 但排除「欄位等於測距值」不等於指認位址，
        # 且候選集並不窮盡。位址一律留給 rank-2 的採集程式碼。
        exclusion = rule_out_range_register(
            values, distances, SIGMA_RAW_SCALE_DIVISOR
        )
    else:
        from pcmef.adapters.legacy_kg import LegacyKGAdapter

        adapter = LegacyKGAdapter()
        inventory = adapter.build_inventory(args.kg_root)
        per_condition = adapter.metric_values(inventory, "sigma_like", "Mean")
        if not per_condition:
            print("error: no Sigma matrices found", file=sys.stderr)
            return 1
        values = np.concatenate(list(per_condition.values()))

    resolution = resolve_sigma(
        values,
        acquisition_register=(
            int(args.acquisition_register, 16) if args.acquisition_register else None
        ),
        acquisition_evidence=args.acquisition_evidence or "",
        ruled_out_registers=ruled_out or None,
        export_decimals=args.export_decimals,
        range_register_test=exclusion,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = resolution.to_artifact()
    artifact["per_condition_observations"] = {
        condition: int(v.size) for condition, v in per_condition.items()
    }
    if exclusion is not None:
        artifact["paired_source"] = str(args.paired_source)
    (out_dir / "sigma_resolution.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"status: {resolution.status}")
    print(
        f"observations: n={resolution.n_observations} "
        f"range=[{resolution.observed_min:.4f}, {resolution.observed_max:.4f}]"
    )
    print("\nscaling hypotheses:")
    for hypothesis in resolution.scaling_hypotheses:
        mark = "OK " if hypothesis.plausible else "no "
        print(
            f"  [{mark}] /{hypothesis.divisor:<10g} implied raw "
            f"[{hypothesis.implied_raw_min:.1f}, {hypothesis.implied_raw_max:.1f}] "
            f"= {hypothesis.range_fraction:.2%} of full scale"
        )
        print(f"        {hypothesis.note}")
    print(f"\nfacets (E1-G08 {resolution.to_artifact()['contract_version']}):")
    required = set(resolution.to_artifact()["required_facets"])
    for name, facet in sorted(resolution.facets.items()):
        need = "required" if name in required else "informational"
        value = "" if facet.value is None else f" = {facet.value}"
        print(f"  [{facet.status:14}] {name:28}{value}   ({need}, rank {facet.evidence_rank})")
    if resolution.blocking_reasons:
        print("\nblocking (E1-G08):")
        for reason in resolution.blocking_reasons:
            print(f"  - {reason}")
    print(f"\nartifact: {(out_dir / 'sigma_resolution.json').resolve()}")
    return 0


def cmd_provenance_audit_timing(args: argparse.Namespace) -> int:
    """SRC-D03：並列文件值、腳本設定值與實測值，量化彼此偏差。"""
    import pandas as pd

    from pcmef.provenance.timing import audit_timing, measure_interval

    config = _load(args)
    measured = None
    if args.recording:
        frame = pd.read_csv(args.recording)
        column = args.time_column or frame.columns[-1]
        measured = measure_interval(
            frame[column].to_numpy(dtype=float),
            source=f"{Path(args.recording).name}:{column}",
        )

    # NOTE(NOTE-028): dataset 自帶的 interval_ms 是 rank-1 provenance，
    # 也是偏差計算的 baseline。--dataset-interval-ms 未給時退回 config 的
    # edge_impulse_frequency_hz，兩者皆無才沒有 rank-1 證據。
    dataset_primary_s = None
    dataset_source = "edge_impulse_export:payload.interval_ms"
    if getattr(args, "dataset_interval_ms", None) is not None:
        dataset_primary_s = float(args.dataset_interval_ms) / 1000.0
    else:
        hz = config.get("real_anchors.edge_impulse_frequency_hz", None)
        if hz:
            dataset_primary_s = 1.0 / float(hz)
            dataset_source = "configs/base.yaml:real_anchors.edge_impulse_frequency_hz"

    provenance = audit_timing(
        measured,
        documented_s=config.get("real_anchors.historical_sample_interval_s", None),
        script_nominal_s=config.get(
            "real_anchors.deployment_script_sample_interval_s", None
        ),
        dataset_primary_s=dataset_primary_s,
        dataset_primary_source=dataset_source,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "timing_provenance.json").write_text(
        json.dumps(provenance.to_artifact(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    baseline_level = provenance.baseline.level.value if provenance.baseline else "—"
    print(f"interval evidence (baseline = {baseline_level}):")
    for item in provenance.evidence:
        mark = "*" if provenance.baseline and item.level is provenance.baseline.level else " "
        print(f" {mark}{item.level.value:<16} {item.value_s:<12.8f} {item.source}")
    if provenance.discrepancies:
        print(f"\ndiscrepancy vs {baseline_level}:")
        for level, delta in sorted(provenance.discrepancies.items()):
            print(f"  {level:<16} {delta:+.1f}%")
    else:
        print("\nno baseline evidence supplied; discrepancies cannot be computed")
    print(f"\nartifact: {(out_dir / 'timing_provenance.json').resolve()}")
    return 0


# ---------------------------------------------------------------------------
# LLM admin（§32 CLI 契約的 LLM Admin / Provider Registry 段）
# ---------------------------------------------------------------------------


def _admin_service(args: argparse.Namespace):
    """組出與 Admin UI 完全相同的服務層實例。

    CLI 與 UI 共用 AdminService 而非各寫一份：兩套寫入路徑必然行為分歧，
    而分歧的那一套通常是忘了做能力檢查的那一套。
    """
    from pcmef.admin.services import AdminService
    from pcmef.llm.registry import LLMRegistry
    from pcmef.secrets.vault import SecretVault

    return AdminService(
        registry=LLMRegistry(args.registry_db),
        vault=SecretVault(args.vault),
        config=_load(args),
        freeze_dir=args.freeze_dir,
        artifact_root=args.artifact_root,
    )


def _resolve_model_profile(service, connection_id: str, model_id: str) -> str:
    """把 provider 的 model id 換成內部 model_profile_id。

    找不到時丟 RegistryError 而非 SystemExit：SystemExit 會直接穿過
    main() 的例外轉換表，讓這一種失敗的輸出格式與其他失敗不一致。
    """
    from pcmef.llm.registry import RegistryError

    available = [m.model_id for m in service.registry.list_models(connection_id)]
    for model in service.registry.list_models(connection_id):
        if model.model_id == model_id:
            return model.model_profile_id
    raise RegistryError(
        f"connection {connection_id} has no model {model_id!r}; known models: "
        f"{available or 'none — run `llm connection fetch-models` first'}"
    )


def cmd_llm_connection_add(args: argparse.Namespace) -> int:
    """建立 connection profile。刻意只收 --secret-ref，不收 API key。

    NOTE(NOTE-019): 命令列參數會留在 shell history 與 process list，
    因此 CLI 這條路徑一律要求先把 key 放進環境變數或 vault，再以參考指過去。
    需要輸入 key 本身時請用 Admin UI 的密碼欄位。
    """
    service = _admin_service(args)
    view = service.add_connection(
        name=args.name, provider=args.provider, secret_ref=args.secret_ref,
        base_url=args.base_url or "", timeout_sec=args.timeout,
        notes=args.notes or "", enabled=not args.disabled,
    )
    print(f"connection created: {view.name}")
    print(f"  connection_id : {view.connection_id}")
    print(f"  provider      : {view.provider}")
    print(f"  secret_ref    : {view.secret_ref}")
    print(f"  credential    : {view.secret_fingerprint}")
    print(f"  status        : {view.status}")
    return 0


def cmd_llm_connection_list(args: argparse.Namespace) -> int:
    service = _admin_service(args)
    views = service.connection_views()
    if not views:
        print("no connections")
        return 0
    for view in views:
        flag = "enabled" if view.enabled else "DISABLED"
        print(f"{view.connection_id}  {view.name}")
        print(
            f"    provider={view.provider}  status={view.status}  {flag}  "
            f"credential={view.secret_fingerprint}"
        )
        for model in view.models:
            verified = ",".join(model.verified) or "-"
            print(f"    - {model.model_id:<28} verified={verified}")
    return 0


def cmd_llm_connection_fetch_models(args: argparse.Namespace) -> int:
    service = _admin_service(args)
    models = service.fetch_models(args.connection)
    print(f"{len(models)} model(s) from connection {args.connection}")
    for model in models:
        declared = ",".join(model.declared) or "-"
        print(f"  {model.model_id:<28} declared={declared}")
    print(
        "\nnote: declared capabilities are provider metadata only. "
        "Run `llm model verify` before binding (SRC-SAI §44).",
        file=sys.stderr,
    )
    return 0


def cmd_llm_model_verify(args: argparse.Namespace) -> int:
    from pcmef.agents.provider import Capability

    service = _admin_service(args)
    profile_id = _resolve_model_profile(service, args.connection, args.model)
    capabilities = (
        [Capability.parse(name) for name in args.capabilities.split(",")]
        if args.capabilities
        else None
    )
    outcome = service.verify_model(profile_id, capabilities)

    print(f"model {outcome.model_id} ({profile_id})")
    for line in outcome.summary():
        print(f"  {line}")
    print("\nverified: " + (", ".join(c.value for c in outcome.verified) or "none"))
    for path in outcome.artifact_paths:
        print(f"  artifact: {path}")
    return 0 if not outcome.failed() else 1


def cmd_llm_connection_select_model(args: argparse.Namespace) -> int:
    """LAVA 流程第二步：選定這條線路要用的模型。"""
    service = _admin_service(args)
    profile_id = _resolve_model_profile(service, args.connection, args.model)
    view = service.select_model(args.connection, profile_id)
    print(f"{view.name}: 已選定 {view.selected_model_id}  ({view.lifecycle})")
    print("next: `llm connection test` 驗證能力，通過後才能 lock")
    return 0


def cmd_llm_connection_test(args: argparse.Namespace) -> int:
    """LAVA 流程第三步：對選定模型跑 chat + structured_json + vision 三項 probe。"""
    from pcmef.llm.capabilities import roles_blocked, roles_servable

    service = _admin_service(args)
    outcome = service.test_connection(args.connection)
    for line in outcome.summary():
        print(f"  {line}")
    current = next(
        c for c in service.connection_views() if c.connection_id == args.connection
    )
    print(f"\nlifecycle: {current.lifecycle}")

    servable = roles_servable(outcome.verified)
    blocked = roles_blocked(outcome.verified)
    print(f"servable roles ({len(servable)}/4): {', '.join(servable) or 'none'}")
    for task_code, missing in sorted(blocked.items()):
        print(f"  - {task_code} 缺 {[c.value for c in missing]}")

    if not servable:
        # 一個角色都服務不了才算失敗。缺 vision 不是失敗，只是少兩個角色。
        print(
            "\ntest failed: this model cannot serve any agent role. Every role "
            "needs at least chat + structured_json (SRC-SAI §45).",
            file=sys.stderr,
        )
        return 1
    print("\nnext: `llm connection lock` 鎖定後才能綁定到 task")
    return 0


def cmd_llm_connection_lock(args: argparse.Namespace) -> int:
    """LAVA 流程第四步：鎖定線路。只有鎖定的線路能被綁到 task。"""
    service = _admin_service(args)
    view = (
        service.unlock_connection(args.connection)
        if args.unlock
        else service.lock_connection(args.connection)
    )
    print(f"{view.name}: {view.lifecycle}")
    return 0


def cmd_llm_binding_lock(args: argparse.Namespace) -> int:
    """鎖定／解鎖 draft binding。這是 draft 層的確認，不是 formal identity。"""
    service = _admin_service(args)
    binding = service.set_binding_locked(args.task_code, not args.unlock)
    state = "locked" if binding.is_locked else "unlocked"
    print(f"{binding.task_code}: draft binding {state}")
    print(
        "note: this is a draft-level confirmation. The only scientifically "
        "binding freeze is freeze/llm_runtime.lock.json (SRC-SAI §52)."
    )
    return 0


def cmd_llm_binding_set(args: argparse.Namespace) -> int:
    service = _admin_service(args)
    profile_id = _resolve_model_profile(service, args.connection, args.model)
    binding = service.bind_task(
        task_code=args.task_code, model_profile_id=profile_id,
        actor=args.actor, reason=args.reason or "",
    )
    print(f"{binding.task_code} -> {args.model}  (draft v{binding.binding_version})")
    print(
        "\nthis updated the draft registry only; no lock was written "
        "(SRC-SAI §42 block C)."
    )
    return 0


def cmd_llm_binding_list(args: argparse.Namespace) -> int:
    service = _admin_service(args)
    for view in service.binding_views():
        required = ",".join(view.required)
        print(f"{view.task_code:<24} {view.status:<9} {view.current_label}")
        print(f"    required={required}  options={len(view.options)}")
    return 0


def cmd_llm_binding_audit(args: argparse.Namespace) -> int:
    service = _admin_service(args)
    rows = service.registry.binding_audit_log()
    if not rows:
        print("no binding changes recorded")
        return 0
    for row in rows:
        before = row["before_hash"][:16] or "(none)"
        print(
            f"{row['changed_at']}  {row['task_code']:<24} "
            f"{before} -> {row['after_hash'][:16]}"
        )
        if row["actor"] or row["reason"]:
            print(f"    actor={row['actor'] or '-'}  reason={row['reason'] or '-'}")
    return 0


def cmd_llm_snapshot(args: argparse.Namespace) -> int:
    """解析 draft binding 成 lock candidate 並算出雜湊。

    NOTE(NOTE-020): §32 的 CLI 草稿把本指令寫成直接輸出到
    freeze/llm_runtime.lock.json，但 §50 定義它是「resolve draft -> lock
    candidate；returns hash；does not auto-start Formal」。兩者衝突時以 §50
    與 §52 結語為準：算出候選與雜湊是一回事，寫進 immutable lock 是另一回事，
    後者需要 --freeze 且所有前提齊備。
    """
    from pcmef.core.locks import LockStore
    from pcmef.llm.snapshot import build_runtime_snapshot, freeze_runtime_snapshot

    service = _admin_service(args)
    snapshot = build_runtime_snapshot(
        service.registry, service.config,
        schemas_dir=args.schemas_dir, prompts_dir=args.prompts_dir,
    )
    path = snapshot.write(args.out)

    print(f"candidate hash : {snapshot.candidate_hash()}")
    print(f"runtime config : {snapshot.runtime_config_hash[:16]}")
    print(f"bound tasks    : {len(snapshot.bindings)}/4")
    for task_code, binding in sorted(snapshot.bindings.items()):
        print(
            f"  {task_code:<24} {binding['provider']}/{binding['model_id']} "
            f"@{binding['provider_revision'] or '?'}"
        )
    print(f"artifact       : {path.resolve()}")

    if not snapshot.freezable:
        print("\nNOT freezable — outstanding prerequisites:", file=sys.stderr)
        for reason in snapshot.blocking_reasons:
            print(f"  - {reason}", file=sys.stderr)
        if args.freeze:
            print(
                "\nrefusing to freeze; resolve the above first "
                "(values marked !required need an advisor decision, "
                "see NOTES.md NOTE-005).",
                file=sys.stderr,
            )
            return 2
        return 0

    print("\nfreezable: all prerequisites met")
    if not args.freeze:
        print("re-run with --freeze to write freeze/llm_runtime.lock.json")
        return 0

    lock_path = freeze_runtime_snapshot(LockStore(args.freeze_dir), snapshot)
    store = LockStore(args.freeze_dir)
    print(f"llm_runtime.lock frozen: {lock_path.resolve()}")
    print(f"  payload hash: {store.load_hash('llm_runtime')}")
    print(
        "\nfrom now on the formal runner resolves bindings from this lock only; "
        "editing the live SQLite binding table cannot change an existing run "
        "(SRC-SAI Appendix J1)."
    )
    return 0


def cmd_llm_cache_audit(args: argparse.Namespace) -> int:
    """§32：列出 content-addressed agent cache 的內容與費用帳。

    --formal 時另外檢查每個 cache 目錄的六份 artifact 是否齊全：
    半套的目錄在 resolve() 時會被判為未命中而重新呼叫 provider，
    對 formal run 而言那代表一次不該發生的重複呼叫，必須先被看見。
    """
    from pcmef.agents.cache import AGENT_ARTIFACT_NAMES
    from pcmef.llm.registry import LLMRegistry

    registry = LLMRegistry(args.registry_db)
    entries = registry.cache_entries()
    root = Path(args.cache_root)

    print(f"cache root : {root.resolve()}")
    print(f"index rows : {len(entries)}")
    if not entries:
        print("\nno cached agent artifacts yet")
        return 0

    incomplete: list[str] = []
    total_tokens = 0
    for entry in entries:
        folder = Path(entry["artifact_path"])
        missing = [
            name for name in AGENT_ARTIFACT_NAMES
            if not (folder / f"{name}.json").exists()
        ]
        total_tokens += int(entry["token_usage"])
        mark = "ok " if not missing else "INCOMPLETE"
        print(
            f"  [{mark}] {entry['cache_key'][:16]}  tokens={entry['token_usage']:<8}"
            f" latency={entry['latency_ms']}ms"
        )
        if missing:
            incomplete.append(f"{entry['cache_key'][:16]}: missing {missing}")

    print(f"\ntotal tokens: {total_tokens}")
    if incomplete and args.formal:
        print("\nincomplete cache entries (formal):", file=sys.stderr)
        for line in incomplete:
            print(f"  - {line}", file=sys.stderr)
        return 1
    return 0


def cmd_admin_serve(args: argparse.Namespace) -> int:
    """啟動本機 Admin 頁面。非 loopback 綁定會被 assert_network_policy 擋下。"""
    from pcmef.admin.app import serve

    print(f"admin console: http://{args.host}:{args.port}/admin/llm-setup")
    if args.enable_ui_bind:
        print("UI binding: ENABLED (draft registry only; locks stay CLI-only)")
    else:
        print("UI binding: disabled — use `pcmef llm binding set` (SRC-SAI §52 step 5)")
    serve(
        host=args.host, port=args.port, bind_enabled=args.enable_ui_bind,
        service=_admin_service(args),
    )
    return 0


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """寫出 audit artifact。空結果仍要留檔，「零筆排除」本身也是稽核結論。"""
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def cmd_locks_status(args: argparse.Namespace) -> int:
    store = LockStore(args.freeze_dir)
    print(f"freeze directory: {Path(args.freeze_dir).resolve()}\n")
    exit_code = 0
    for name, spec in LOCK_SPECS.items():
        if not store.exists(name):
            blocked = [dep for dep in spec.requires if not store.exists(dep)]
            state = "BLOCKED" if blocked else "pending"
            detail = f" (waiting on {', '.join(blocked)})" if blocked else ""
            print(f"  [{state:>7}] {name}{detail}")
            continue
        try:
            store.load(name)
            print(f"  [ frozen] {name}  {store.load_hash(name)[:16]}")
        except LockError as error:
            print(f"  [ BROKEN] {name}: {error}")
            exit_code = 3
    return exit_code


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_llm_store_arguments(parser: argparse.ArgumentParser) -> None:
    """LLM admin 子指令共用的儲存位置參數。

    每個子指令各自宣告而非放在頂層：頂層參數必須寫在子指令之前，
    而 `pcmef --registry-db x llm binding set …` 這種順序沒有人記得住。
    """
    parser.add_argument("--registry-db", default="registry/llm_admin.db")
    parser.add_argument("--vault", default="secrets/vault.json")
    parser.add_argument("--freeze-dir", default="freeze")
    parser.add_argument(
        "--artifact-root", default="artifacts/llm_verification",
        help="capability probe 證據的落盤位置；同內容的 artifact 不會被覆寫",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcmef", description="PC-MEF Research System CLI"
    )
    parser.add_argument(
        "--config",
        action="append",
        help="設定檔路徑，可重複；由低優先序排到高（預設 configs/base.yaml）",
    )
    parser.add_argument(
        "--set",
        action="append",
        help="覆蓋單一設定值，格式 key.path=value；--formal 模式下禁止使用",
    )
    parser.add_argument(
        "--formal",
        action="store_true",
        help="formal 模式：未核定數值一律拒絕啟動，且不接受 CLI 覆蓋",
    )
    parser.add_argument("--log-file", help="除 stderr 外另寫入的 log 檔路徑")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version", help="顯示版本").set_defaults(func=cmd_version)

    config_parser = subparsers.add_parser("config", help="設定檢視與檢查")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    show = config_sub.add_parser("show", help="顯示設定摘要或單一值")
    show.add_argument("--key", help="以點號路徑顯示單一設定值")
    show.set_defaults(func=cmd_config_show)
    check = config_sub.add_parser("check", help="列出所有待教授裁決的數值")
    check.set_defaults(func=cmd_config_check)

    audit_parser = subparsers.add_parser("audit", help="資料盤點與稽核")
    audit_sub = audit_parser.add_subparsers(dest="audit_command", required=True)
    real_data = audit_sub.add_parser(
        "real-data", help="M0：盤點前研究 CSV 並產出五層計數與排除帳"
    )
    real_data.add_argument("--source", required=True, help="原始資料根目錄（唯讀）")
    real_data.add_argument("--out", default="data/inventory", help="盤點產物輸出目錄")
    real_data.add_argument(
        "--source-format",
        default="legacy-csv",
        choices=["legacy-csv", "edge-impulse"],
        help="來源格式：legacy-csv 為逐 metric 分檔的原始 CSV；"
             "edge-impulse 為 Edge Impulse dataset export 的 JSON",
    )
    real_data.add_argument(
        "--key-strategy",
        default="trailing_integer",
        choices=["trailing_integer", "full_stem", "stem_without_metric"],
        help="從檔名取出測量編號的策略；四個 metric 必須都能取出同一組編號",
    )
    real_data.add_argument(
        "--sigma-status",
        default="UNRESOLVED",
        choices=["UNRESOLVED", "RESOLVED"],
        help="Sigma register provenance 的當前狀態；RESOLVED 才允許 e1-eligible",
    )
    real_data.set_defaults(func=cmd_audit_real_data)

    e1_gates = audit_sub.add_parser(
        "e1-gates", help="Batch 7：逐項檢查 E1 開跑前的十二個 gate"
    )
    e1_gates.add_argument(
        "--require",
        help="要求這些 gate 必須 PASS，否則非零 exit code；"
             "格式 G01:G12 或 G01,G04。不指定時只盤點。",
    )
    e1_gates.add_argument("--inventory", default="data/inventory")
    e1_gates.add_argument("--splits", default="data/splits")
    e1_gates.add_argument("--simulation", default="outputs/simulation")
    e1_gates.add_argument("--surrogate", default="outputs/surrogate")
    e1_gates.add_argument("--provenance", default="provenance")
    e1_gates.add_argument("--tests", default="tests")
    e1_gates.add_argument("--freeze-dir", default="freeze")
    e1_gates.add_argument("--out", help="另把報告寫成 JSON 的目錄")
    e1_gates.set_defaults(func=cmd_audit_e1_gates)

    firewall = audit_sub.add_parser(
        "heldout-firewall", help="Batch 7：held-out 洩漏防線（Appendix B）"
    )
    split_policy = audit_sub.add_parser(
        "real-split-policy", help="Batch 7：real split policy 契約（Appendix H1）"
    )
    for parser_ in (firewall, split_policy):
        parser_.add_argument("--splits", default="data/splits")
        parser_.add_argument("--freeze-dir", default="freeze")
        parser_.add_argument("--outputs", default="outputs")
        parser_.add_argument("--out", help="另把報告寫成 JSON 的目錄")
    firewall.set_defaults(func=cmd_audit_heldout_firewall)
    split_policy.set_defaults(func=cmd_audit_real_split_policy)

    sim_parser = subparsers.add_parser("sim", help="模擬")
    sim_sub = sim_parser.add_subparsers(dest="sim_command", required=True)
    smoke = sim_sub.add_parser(
        "smoke", help="M1：跑最小場景並產出 E1-G03 smoke manifest"
    )
    smoke.add_argument("--config", default="configs/simulation/smoke.yaml")
    smoke.add_argument("--out", default="outputs/simulation")
    smoke.add_argument("--variant", help="mitsuba variant（預設取設定檔）")
    smoke.add_argument("--temporal-bins", type=int, help="transient 時間軸 bin 數")
    smoke.add_argument(
        "--in-worker", action="store_true",
        help=argparse.SUPPRESS,  # 內部旗標：標示此行程即為算圖 worker
    )
    smoke.set_defaults(func=cmd_sim_smoke)

    split_parser = subparsers.add_parser("split", help="資料切分")
    split_sub = split_parser.add_subparsers(dest="split_command", required=True)
    plan_real = split_sub.add_parser(
        "plan-real", help="E1-G02：規劃 real split 並產出 split_registry.json"
    )
    freeze_real = split_sub.add_parser(
        "freeze-real", help="E1-G09：凍結 real_split_policy.lock（不可重抽）"
    )
    for parser_ in (plan_real, freeze_real):
        parser_.add_argument("--source", required=True, help="原始資料根目錄（唯讀）")
        parser_.add_argument(
            "--source-format", default="edge-impulse",
            choices=["legacy-csv", "edge-impulse"],
        )
    plan_real.add_argument("--out", default="data/splits")
    plan_real.set_defaults(func=cmd_split_plan_real)
    freeze_real.add_argument("--freeze-dir", default="freeze")
    freeze_real.set_defaults(func=cmd_freeze_real_split_policy)

    e1_parser = subparsers.add_parser("e1", help="E1 fidelity 實驗")
    e1_sub = e1_parser.add_subparsers(dest="e1_command", required=True)
    e1_evidence = e1_sub.add_parser(
        "metrics-evidence", help="E1-G06：跑度量單元測試並產出 JUnit 報告"
    )
    e1_evidence.add_argument("--tests-dir", default="tests/e1")
    e1_evidence.add_argument("--out", default="tests/e1_metrics.xml")
    e1_evidence.set_defaults(func=cmd_e1_metrics_evidence)

    sur_parser = subparsers.add_parser("surrogate", help="感測器替身")
    sur_sub = sur_parser.add_subparsers(dest="surrogate_command", required=True)
    sur_smoke = sur_sub.add_parser(
        "smoke", help="E1-G04：由真實 transient 產出四特徵並檢查無 NaN/Inf"
    )
    sur_smoke.add_argument("--simulation-out", default="outputs/simulation")
    sur_smoke.add_argument("--out", default="outputs/surrogate")
    sur_smoke.add_argument("--n-samples", type=int, default=500)
    sur_smoke.add_argument("--seed", type=int, default=1042)
    sur_smoke.add_argument(
        "--sample-interval-s", type=float, default=0.08200001312,
        help="量測取樣間隔；預設為 Edge Impulse export 記錄的實際值",
    )
    sur_smoke.add_argument(
        "--sample-interval-source", default="edge_impulse_export:interval_ms",
        help="上述間隔的出處；不得留空",
    )
    sur_smoke.set_defaults(func=cmd_surrogate_smoke)

    prov_parser = subparsers.add_parser("provenance", help="來源歧異的證據判定")
    prov_sub = prov_parser.add_subparsers(dest="provenance_command", required=True)

    sigma_cmd = prov_sub.add_parser(
        "resolve-sigma", help="SRC-D01/D02：判定 Sigma scaling 與 register 狀態"
    )
    sigma_cmd.add_argument(
        "--kg-root", default="data/raw_real/tof_aggregated/KG_all",
        help="含 KG_<class>_<Metric>_<Stat>.csv 的目錄",
    )
    sigma_cmd.add_argument("--out", default="provenance", help="artifact 輸出目錄")
    sigma_cmd.add_argument(
        "--acquisition-register",
        help="產生此資料集的採集腳本所用的暫存器（十六進位，例如 0x1E）；"
             "只有附上 --acquisition-evidence 才會被採信",
    )
    sigma_cmd.add_argument(
        "--acquisition-evidence",
        help="上述暫存器的出處，例如 collect_dataset.py:31",
    )
    sigma_cmd.add_argument(
        "--export-decimals", type=int,
        help="來源保留的小數位數。divisor 由量化步長檢定判定，而該檢定需要知道"
             "觀測值本身被四捨五入到幾位 —— 不知道就無法區分「量化痕跡」與"
             "「匯出精度」。Edge Impulse export 為 4（NOTE-011）",
    )
    sigma_cmd.add_argument(
        "--paired-source",
        help="逐 recording 的 Edge Impulse export 根目錄。提供後才會執行"
             "range register 排除檢驗 —— 該檢驗需要同一列的 sigma 與 distance "
             "配對，彙總格式做不到（NOTE-010）",
    )
    sigma_cmd.set_defaults(func=cmd_provenance_resolve_sigma)

    timing_cmd = prov_sub.add_parser(
        "audit-timing", help="SRC-D03：並列文件/腳本/實測三個取樣間隔"
    )
    timing_cmd.add_argument(
        "--recording", help="含時間欄的原始 recording CSV；缺此參數則無實測值"
    )
    timing_cmd.add_argument("--time-column", help="時間欄名稱（預設取最後一欄）")
    timing_cmd.add_argument(
        "--dataset-interval-ms", type=float, default=None,
        help="dataset 自帶的 interval_ms（rank-1 provenance，偏差 baseline）",
    )
    timing_cmd.add_argument("--out", default="provenance", help="artifact 輸出目錄")
    timing_cmd.set_defaults(func=cmd_provenance_audit_timing)

    # -- LLM admin ---------------------------------------------------------
    llm_parser = subparsers.add_parser("llm", help="LLM 連線、模型與角色綁定")
    llm_sub = llm_parser.add_subparsers(dest="llm_command", required=True)

    conn_parser = llm_sub.add_parser("connection", help="provider 連線")
    conn_sub = conn_parser.add_subparsers(dest="connection_command", required=True)
    conn_add = conn_sub.add_parser("add", help="建立 connection profile")
    conn_add.add_argument("--provider", required=True)
    conn_add.add_argument("--name", required=True, help="管理者可讀別名")
    conn_add.add_argument(
        "--secret-ref", required=True,
        help="憑證參考，例如 env:GEMINI_API_KEY 或 vault:<uuid>；"
             "不接受 API key 本身（會留在 shell history 與 process list）",
    )
    conn_add.add_argument("--base-url", help="留空則用 provider 預設端點")
    conn_add.add_argument("--timeout", type=int, default=30)
    conn_add.add_argument("--notes", default="")
    conn_add.add_argument("--disabled", action="store_true")
    conn_add.set_defaults(func=cmd_llm_connection_add)

    conn_list = conn_sub.add_parser("list", help="列出 connection 與其模型")
    conn_list.set_defaults(func=cmd_llm_connection_list)

    conn_fetch = conn_sub.add_parser("fetch-models", help="向 provider 取得模型清單")
    conn_fetch.add_argument("--connection", required=True)
    conn_fetch.set_defaults(func=cmd_llm_connection_fetch_models)

    conn_select = conn_sub.add_parser(
        "select-model", help="選定這條線路要用的模型（LAVA 流程第二步）"
    )
    conn_select.add_argument("--connection", required=True)
    conn_select.add_argument("--model", required=True, help="provider 的 model id")
    conn_select.set_defaults(func=cmd_llm_connection_select_model)

    conn_test = conn_sub.add_parser(
        "test", help="對選定模型跑三項 capability probe（LAVA 流程第三步）"
    )
    conn_test.add_argument("--connection", required=True)
    conn_test.set_defaults(func=cmd_llm_connection_test)

    conn_lock = conn_sub.add_parser(
        "lock", help="鎖定線路；只有鎖定的線路能綁到 task（LAVA 流程第四步）"
    )
    conn_lock.add_argument("--connection", required=True)
    conn_lock.add_argument("--unlock", action="store_true", help="改為解鎖")
    conn_lock.set_defaults(func=cmd_llm_connection_lock)

    model_parser = llm_sub.add_parser("model", help="模型能力驗證")
    model_sub = model_parser.add_subparsers(dest="model_command", required=True)
    model_verify = model_sub.add_parser("verify", help="執行 capability probe")
    model_verify.add_argument("--connection", required=True)
    model_verify.add_argument("--model", required=True, help="provider 的 model id")
    model_verify.add_argument(
        "--capabilities",
        help="逗號分隔；預設為 Thesis Core 必要的 chat,structured_json,vision",
    )
    model_verify.set_defaults(func=cmd_llm_model_verify)

    binding_parser = llm_sub.add_parser("binding", help="Agent 角色綁定（draft）")
    binding_sub = binding_parser.add_subparsers(dest="binding_command", required=True)
    binding_set = binding_sub.add_parser("set", help="改綁一個 task 的 draft binding")
    binding_set.add_argument("task_code")
    binding_set.add_argument("--connection", required=True)
    binding_set.add_argument("--model", required=True)
    binding_set.add_argument("--actor", default="cli")
    binding_set.add_argument("--reason", default="")
    binding_set.set_defaults(func=cmd_llm_binding_set)
    binding_list = binding_sub.add_parser("list", help="列出四個 task 的 draft binding")
    binding_list.set_defaults(func=cmd_llm_binding_list)
    binding_audit = binding_sub.add_parser("audit", help="列出改綁歷程")
    binding_audit.set_defaults(func=cmd_llm_binding_audit)
    binding_lock = binding_sub.add_parser(
        "lock", help="鎖定 draft binding（draft 層確認，非 formal identity）"
    )
    binding_lock.add_argument("task_code")
    binding_lock.add_argument("--unlock", action="store_true")
    binding_lock.set_defaults(func=cmd_llm_binding_lock)

    snapshot_cmd = llm_sub.add_parser(
        "snapshot", help="解析 draft binding 成 llm_runtime lock candidate"
    )
    snapshot_cmd.add_argument("--out", default="outputs/llm", help="候選快照輸出目錄")
    snapshot_cmd.add_argument("--schemas-dir", default="schemas")
    snapshot_cmd.add_argument("--prompts-dir", default="configs/agents/prompts")
    snapshot_cmd.add_argument(
        "--freeze", action="store_true",
        help="前提齊備時寫入 freeze/llm_runtime.lock.json（不可覆寫）",
    )
    snapshot_cmd.set_defaults(func=cmd_llm_snapshot)

    cache_parser = llm_sub.add_parser("cache", help="Agent artifact cache")
    cache_sub = cache_parser.add_subparsers(dest="cache_command", required=True)
    cache_audit = cache_sub.add_parser("audit", help="列出快取內容與費用帳")
    cache_audit.add_argument("--cache-root", default="artifacts/agents")
    cache_audit.set_defaults(func=cmd_llm_cache_audit)

    for llm_command in (
        conn_add, conn_list, conn_fetch, conn_select, conn_test, conn_lock,
        model_verify, binding_set, binding_list, binding_audit, binding_lock,
        snapshot_cmd, cache_audit,
    ):
        _add_llm_store_arguments(llm_command)

    # -- Admin UI ----------------------------------------------------------
    admin_parser = subparsers.add_parser("admin", help="本機管理頁面")
    admin_sub = admin_parser.add_subparsers(dest="admin_command", required=True)
    serve_cmd = admin_sub.add_parser("serve", help="啟動 Admin LLM Setup 頁面")
    serve_cmd.add_argument(
        "--host", default="127.0.0.1",
        help="預設只綁本機；非 loopback 需 admin token 與 TLS（SRC-SAI §46）",
    )
    serve_cmd.add_argument("--port", type=int, default=8787)
    serve_cmd.add_argument(
        "--enable-ui-bind", action="store_true",
        help="開放 UI 的 Bind 按鈕；仍只寫 draft registry，lock 一律走 CLI",
    )
    _add_llm_store_arguments(serve_cmd)
    serve_cmd.set_defaults(func=cmd_admin_serve)

    locks_parser = subparsers.add_parser("locks", help="formal freeze 狀態")
    locks_sub = locks_parser.add_subparsers(dest="locks_command", required=True)
    status = locks_sub.add_parser("status", help="顯示每個 lock 的凍結狀態與前置條件")
    status.add_argument("--freeze-dir", default="freeze")
    status.set_defaults(func=cmd_locks_status)

    return parser


def _make_output_encoding_forgiving() -> None:
    """讓無法編碼的字元退化成替代字元，而不是讓整個指令崩潰。

    Windows 繁中主控台預設是 cp950，它沒有 U+2205（空集合）這類符號。
    一份跑到一半才崩掉的稽核報告，比一份有幾個問號的報告糟得多 ——
    前者會讓人以為是稽核器壞了，而真正的結論根本沒印出來。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                # 被重導向到不支援 reconfigure 的物件時忽略即可。
                pass


def main(argv: list[str] | None = None) -> int:
    _make_output_encoding_forgiving()
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(log_file=args.log_file)
    from pcmef.adapters.legacy_csv import LegacyCSVError
    from pcmef.admin.services import AdminServiceError
    from pcmef.agents.provider import ProviderError
    from pcmef.llm.registry import DependencyError, RegistryError
    from pcmef.llm.snapshot import SnapshotError
    from pcmef.secrets.vault import SecretError

    try:
        return int(args.func(args))
    except DependencyError as error:
        # §46：仍有 active binding 的對象不可刪除，CLI 端以非零 exit code 呈現，
        # 並照樣列出相依 task —— 與 HTTP 端的 409 是同一條契約的兩種外觀。
        print(f"error: {error}", file=sys.stderr)
        print(f"dependent tasks: {', '.join(error.tasks)}", file=sys.stderr)
        return 1
    except (
        ConfigError, LockError, LegacyCSVError, RegistryError,
        AdminServiceError, SnapshotError, SecretError, ProviderError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except FormalBlockingError as error:
        print(f"formal-blocking: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
