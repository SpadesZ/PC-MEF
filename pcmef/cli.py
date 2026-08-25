# PC-MEF Research System source maintenance contract
# 上下游: 由使用者終端機與 CI 呼叫；讀取 configs/ 下的 YAML 與 freeze/ 下的 lock 檔，
#         寫出人類可讀報告到 stdout 與選用的 log 檔；exit code 供 CI 判定。
# 檔案路徑: pcmef/cli.py
# 產生時間: 2026-08-26 01:40 +08:00
# 版本: v0.2.0
# 功能說明: 系統的命令列入口。提供查版本、檢視設定、列出所有待教授裁決的數值、
#           顯示每個凍結點的狀態與卡在誰身上，以及執行 M0 前研究資料盤點。
# 模組定位: 所有 formal run 的唯一進入點。它「不是」互動式工具 —— formal 路徑
#           不得依賴任何鍵盤輸入，才能在無人值守的環境重現。
# 主要責任:
#   1. build_parser() 定義全域參數與子指令樹
#   2. cmd_config_show() 顯示設定來源、hash 與待裁決數量
#   3. cmd_config_check() 逐項列出待裁決數值與其出處
#   4. cmd_audit_real_data() 執行 M0 盤點並產出四份 artifact 與五層計數
#   5. cmd_locks_status() 顯示每個 lock 為 frozen / pending / BLOCKED
#   6. main() 統一把 ConfigError / LockError / LegacyCSVError / FormalBlockingError
#      轉成 exit code
# 維護提醒:
#   - 不得接受 API key 作為命令列參數；secret 一律走環境變數或 secret vault，
#     命令列參數會留在 shell history 與 process list。
#   - 不得讓 formal 路徑依賴互動輸入或 CLI override。
#   - 不得讓 audit real-data 從 config 讀 sigma_status；M0 盤點正是產生該證據的
#     步驟，反向依賴會造成循環（見 cmd_audit_real_data 說明）。
#   - 新增子指令時同步更新 README 的常用指令表。
#   - v0.1.0 新增：version / config show / config check / locks status 四組指令。
#   - v0.2.0 新增：audit real-data（M0 盤點）。
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
