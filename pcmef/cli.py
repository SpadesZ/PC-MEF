# PC-MEF Research System source maintenance contract
# 上下游: 由使用者終端機與 CI 呼叫；讀取 configs/ 下的 YAML 與 freeze/ 下的 lock 檔，
#         寫出人類可讀報告到 stdout 與選用的 log 檔；exit code 供 CI 判定。
# 檔案路徑: pcmef/cli.py
# 產生時間: 2026-08-26 09:50 +08:00
# 版本: v0.4.0
# 功能說明: 系統的命令列入口。提供查版本、檢視設定、列出所有待教授裁決的數值、
#           顯示每個凍結點的狀態、執行 M0 前研究資料盤點，
#           以及判定 Sigma scaling 與取樣間隔這兩處來源歧異。
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
#   9. main() 統一把 ConfigError / LockError / LegacyCSVError / FormalBlockingError
#      轉成 exit code
# 維護提醒:
#   - 不得接受 API key 作為命令列參數；secret 一律走環境變數或 secret vault，
#     命令列參數會留在 shell history 與 process list。
#   - 不得讓 formal 路徑依賴互動輸入或 CLI override。
#   - 不得讓 audit real-data 從 config 讀 sigma_status；M0 盤點正是產生該證據的
#     步驟，反向依賴會造成循環（見 cmd_audit_real_data 說明）。
#   - 新增子指令時同步更新 README 的常用指令表。
#   - 不得為 provenance resolve-sigma 增加「自動挑一個 register」的選項；
#     暫存器身分只能由採集腳本證據決定（NOTE-010）。
#   - v0.1.0 新增：version / config show / config check / locks status 四組指令。
#   - v0.2.0 新增：audit real-data（M0 盤點）。
#   - v0.3.0 新增：provenance resolve-sigma / audit-timing。
#   - v0.4.0 新增：sim smoke（子行程隔離，見 NOTE-012）。
# 驗證方式:
#   - py -3.10 -m pytest tests/test_cli.py -v
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
    from pcmef.adapters.legacy_csv import LegacyCSVAdapter, LegacyCSVConfig

    config = _load(args)
    nominal = config.get("real_anchors.nominal_logical_recordings", 0)

    adapter = LegacyCSVAdapter(
        LegacyCSVConfig(
            key_strategy=args.key_strategy,
            sigma_status=args.sigma_status,
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
        "sigma_status": args.sigma_status,
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

    from pcmef.adapters.legacy_kg import LegacyKGAdapter
    from pcmef.provenance.sigma import resolve_sigma

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
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = resolution.to_artifact()
    artifact["per_condition_observations"] = {
        condition: int(v.size) for condition, v in per_condition.items()
    }
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
    print(f"\nresolved divisor : {resolution.resolved_divisor}")
    print(
        "resolved register: "
        + (hex(resolution.resolved_register) if resolution.resolved_register else "—")
    )
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

    provenance = audit_timing(
        measured,
        documented_s=config.get("real_anchors.historical_sample_interval_s", None),
        script_nominal_s=config.get(
            "real_anchors.deployment_script_sample_interval_s", None
        ),
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "timing_provenance.json").write_text(
        json.dumps(provenance.to_artifact(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("interval evidence (only 'measured' may be used for a recording contract):")
    for item in provenance.evidence:
        print(f"  {item.level.value:<16} {item.value_s:<10.5f} {item.source}")
    if provenance.discrepancies:
        print("\ndiscrepancy vs measured:")
        for level, delta in sorted(provenance.discrepancies.items()):
            print(f"  {level:<16} {delta:+.1f}%")
    else:
        print("\nno measured evidence supplied; discrepancies cannot be computed")
    print(f"\nartifact: {(out_dir / 'timing_provenance.json').resolve()}")
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
    sigma_cmd.set_defaults(func=cmd_provenance_resolve_sigma)

    timing_cmd = prov_sub.add_parser(
        "audit-timing", help="SRC-D03：並列文件/腳本/實測三個取樣間隔"
    )
    timing_cmd.add_argument(
        "--recording", help="含時間欄的原始 recording CSV；缺此參數則無實測值"
    )
    timing_cmd.add_argument("--time-column", help="時間欄名稱（預設取最後一欄）")
    timing_cmd.add_argument("--out", default="provenance", help="artifact 輸出目錄")
    timing_cmd.set_defaults(func=cmd_provenance_audit_timing)

    locks_parser = subparsers.add_parser("locks", help="formal freeze 狀態")
    locks_sub = locks_parser.add_subparsers(dest="locks_command", required=True)
    status = locks_sub.add_parser("status", help="顯示每個 lock 的凍結狀態與前置條件")
    status.add_argument("--freeze-dir", default="freeze")
    status.set_defaults(func=cmd_locks_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(log_file=args.log_file)
    from pcmef.adapters.legacy_csv import LegacyCSVError

    try:
        return int(args.func(args))
    except (ConfigError, LockError, LegacyCSVError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except FormalBlockingError as error:
        print(f"formal-blocking: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
