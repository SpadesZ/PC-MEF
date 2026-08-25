# 檔案路徑: pcmef/cli.py
# 模組定位: 所有 formal run 的唯一命令列進入點。
# 功能說明: 提供 version / config show / config check / locks status 子指令，後續批次在此擴充 audit 與 experiment。
# 主要責任: 實現 FR-019「所有 formal run 可無 UI 透過 CLI 執行」與 FR-043 CLI parity。
# 呼叫來源: 使用者終端機、CI、以及 tests/cli。
# 輸入契約: argparse 參數；--set 覆蓋在 --formal 模式下會被拒絕。
# 輸出契約: 人類可讀的 stdout 報告；非零 exit code 代表 formal-blocking 或 lock 違規。
# 安全邊界: 不接受 API key 作為命令列參數，secret 一律走環境變數或 secret vault。
# 維護提醒: 新增子指令時同步更新 README 的 CLI 對照表，並確認 formal 路徑不依賴任何互動輸入。
# 版本: v0.1.0 / 2026-08-25
# ----------------------------------------------------------------------------------------------------

from __future__ import annotations

import argparse
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
    try:
        return int(args.func(args))
    except (ConfigError, LockError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except FormalBlockingError as error:
        print(f"formal-blocking: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
