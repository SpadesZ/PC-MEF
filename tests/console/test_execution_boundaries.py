# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證守衛看得到「寫到哪裡」、「誰開的」
#         與「科學什麼時候算完成」。全部在 tmp_path，不觸發任何真正的
#         科研寫入。
# 檔案路徑: tests/console/test_execution_boundaries.py
# 產生時間: 2026-09-25 21:00 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合第九輪 —— openat/dir_fd 的落點、hardlink 別名、
#           listener 與子行程的擁有權、Formal E2 科學完成點之後的
#           KeyboardInterrupt / SystemExit。
# 模組定位: test_execution_projection.py 的續作。前一輪守的是「路徑長
#           什麼樣子」；這一輪守的是「它實際指向哪裡」—— 相對於一個目錄
#           描述子的名字、與受保護檔案同一個 inode 的另一個名字、OS 挑的
#           port 號碼、以及越過科學完成點之後才落地的中斷。
# 主要責任:
#   1. dir_fd 寫入（open / unlink / rename / mkdir / link / symlink）被擋，
#      受保護內容不變
#   2. shutil.rmtree 在刪第一個檔案之前就被擋
#   3. O_RDONLY|O_TRUNC 算寫入
#   4. 既存 hardlink 別名被擋，一般檔案的代價不變
#   5. allowing(0) 以 socket 物件與行程樹驗收尾
#   6. 子行程洩漏歸到留下它的那個測試
#   7. 科學完成點之後的 KeyboardInterrupt / SystemExit 不改判，
#      之前的照常往外拋
# 維護提醒:
#   - **每一條攻擊測試都要斷言受保護的內容沒變。**「有沒有丟例外」與
#     「有沒有真的寫進去」是兩件事 —— round 9 實測到 rmtree 被「擋下」
#     時，底下的檔案早就刪光了。
#   - 不得為了讓 Windows 跑得過而拿掉 POSIX 專屬的測試；那一段以
#     skip 標明平台，並另有一條測試斷言 Windows 確實沒有 dir_fd。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 9。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_boundaries.py -v
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

HAS_DIR_FD = os.open in os.supports_dir_fd
needs_dir_fd = pytest.mark.skipif(
    not HAS_DIR_FD,
    reason="dir_fd is not supported on this platform (os.supports_dir_fd is "
           "empty on Windows); the openat path is unreachable here",
)


def _psutil_or_skip():
    return pytest.importorskip(
        "psutil", reason="ownership checks of processes need psutil"
    )


# ---------------------------------------------------------------------------
# 共用夾具
# ---------------------------------------------------------------------------


def _guard_module():
    import importlib.util

    location = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_repo_guard", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _session_guard(request):
    """pytest 自己載入的那一份 `tests/conftest.py`（真的掛上稽核事件的那份）。"""
    here = Path(__file__).resolve().parents[1] / "conftest.py"
    for plugin in request.config.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and Path(path).resolve() == here:
            return plugin
    raise AssertionError("the session conftest is not registered with pytest")


ORIGINAL = "ORIGINAL"


def _protected_tree(tmp_path: Path):
    """tmp 裡的一棵「受保護」樹，外加一個不受保護的鄰居。"""
    protected = tmp_path / "freeze"
    (protected / "runs").mkdir(parents=True)
    lock = protected / "runs" / "lock.json"
    lock.write_text(ORIGINAL, encoding="utf-8")
    (protected / "runs" / "sub").mkdir()
    (protected / "runs" / "sub" / "deep.json").write_text(ORIGINAL, encoding="utf-8")
    safe = tmp_path / "safe"
    safe.mkdir()
    return protected, lock, safe


def _state(root: Path) -> dict[str, str]:
    """一棵樹的完整內容：名字、種類、內容。**比對它，不是比對例外。**"""
    found = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[key] = f"<symlink {os.readlink(path)}>"
        elif path.is_dir():
            found[key] = "<dir>"
        else:
            found[key] = path.read_text(encoding="utf-8")
    return found


class _Switch:
    """一個稽核 hook，逐測試切換它守的那棵樹。沒有目標時什麼都不擋。"""

    def __init__(self) -> None:
        self.current = None

    def check_write(self, path, dir_fd=None) -> None:
        if self.current is not None:
            self.current.check_write(path, dir_fd=dir_fd)

    def check_tree_removal(self, path, dir_fd=None) -> None:
        if self.current is not None:
            self.current.check_tree_removal(path, dir_fd=dir_fd)


@pytest.fixture(scope="module")
def hooked():
    """整個模組只掛**一個** hook。

    audit hook 裝上去就拿不下來。每個測試各掛一個的話，之後整輪的
    每一次寫入都要多跑幾十個 hook —— 拖慢、也擾動之後所有測試的時序。
    """
    guard = _guard_module()
    switch = _Switch()
    guard._install_tripwire(switch)
    return guard, switch


@pytest.fixture()
def armed(tmp_path, hooked):
    """掛上稽核事件的 tripwire，只守 tmp 裡那一棵樹，測試結束就解除。"""
    guard, switch = hooked
    protected, lock, safe = _protected_tree(tmp_path)
    tripwire = guard.WriteTripwire([protected])
    before = _state(protected)
    switch.current = tripwire
    yield guard, tripwire, protected, lock, safe
    switch.current = None
    assert _state(protected) == before, (
        "the protected tree changed even though the guard was armed"
    )


# ---------------------------------------------------------------------------
# 1 dir_fd：落點相對於描述子，不是 cwd
# ---------------------------------------------------------------------------


def test_windows_really_has_no_dir_fd():
    """平台前提寫成斷言：Windows 上 dir_fd 整族都不存在。

    這一段的 POSIX 測試在 Windows 上 skip。若哪天 Windows 開始支援
    dir_fd，這條會先失敗，提醒那些 skip 已經不再成立。
    """
    if os.name != "nt":
        pytest.skip("this states a Windows premise")
    assert not os.supports_dir_fd
    with pytest.raises(NotImplementedError):
        os.open("x", os.O_RDONLY, dir_fd=0)


@needs_dir_fd
def test_os_open_relative_to_a_protected_dir_fd_is_blocked(armed):
    """`open` 稽核事件只有 (path, mode, flags)：`lock.json` 看起來在 cwd。

    round 9 之前這一行在 Linux 上直接把受保護的檔案截斷並覆寫。
    """
    guard, _tripwire, protected, _lock, _safe = armed
    directory = os.open(protected / "runs", os.O_RDONLY)
    try:
        with pytest.raises(guard.ProtectedPathWrite):
            os.open("lock.json", os.O_WRONLY | os.O_TRUNC, dir_fd=directory)
    finally:
        os.close(directory)


@needs_dir_fd
def test_an_opener_with_dir_fd_is_blocked(armed):
    """`open(..., opener=partial(os.open, dir_fd=fd))` 是 Python 版的 openat。"""
    guard, _tripwire, protected, _lock, _safe = armed
    directory = os.open(protected / "runs", os.O_RDONLY)
    try:
        with pytest.raises(guard.ProtectedPathWrite):
            with open("lock.json", "w", encoding="utf-8",
                      opener=functools.partial(os.open, dir_fd=directory)) as h:
                h.write("TAMPERED")
    finally:
        os.close(directory)


@needs_dir_fd
def test_dot_dot_from_an_unrelated_dir_fd_is_blocked(armed):
    """從一個乾淨目錄的描述子用 `..` 走進受保護樹。

    名字裡有 needle，但照 cwd 解析會落在別的地方 —— 只有把 `..`
    接在描述子的實際位置後面才判得對。
    """
    guard, _tripwire, _protected, _lock, safe = armed
    directory = os.open(safe, os.O_RDONLY)
    cwd = os.getcwd()
    os.chdir(os.path.abspath(os.sep))
    try:
        with pytest.raises(guard.ProtectedPathWrite):
            os.open("../freeze/runs/lock.json", os.O_WRONLY | os.O_TRUNC,
                    dir_fd=directory)
    finally:
        os.chdir(cwd)
        os.close(directory)


@needs_dir_fd
@pytest.mark.parametrize("operation", [
    "unlink", "rmdir", "mkdir", "rename_from", "rename_into", "replace_into",
    "link_into", "symlink_into",
])
def test_every_dir_fd_event_is_placed_before_it_is_judged(armed, operation):
    """帶 dir_fd 的稽核事件，名字一律接在描述子的位置後面再判。

    round 9 之前這八種在 Linux 上全部放行：事件裡的 dir_fd 被忽略，
    裸檔名照 cwd 解析。
    """
    guard, _tripwire, protected, _lock, safe = armed
    (safe / "src.txt").write_text("src", encoding="utf-8")
    inside = os.open(protected / "runs", os.O_RDONLY)
    outside = os.open(safe, os.O_RDONLY)
    calls = {
        "unlink": lambda: os.unlink("lock.json", dir_fd=inside),
        "rmdir": lambda: os.rmdir("sub", dir_fd=inside),
        "mkdir": lambda: os.mkdir("planted", dir_fd=inside),
        "rename_from": lambda: os.rename("lock.json", "moved.json",
                                         src_dir_fd=inside, dst_dir_fd=outside),
        "rename_into": lambda: os.rename("src.txt", "lock.json",
                                         src_dir_fd=outside, dst_dir_fd=inside),
        "replace_into": lambda: os.replace("src.txt", "lock.json",
                                           src_dir_fd=outside, dst_dir_fd=inside),
        "link_into": lambda: os.link("src.txt", "planted",
                                     src_dir_fd=outside, dst_dir_fd=inside),
        "symlink_into": lambda: os.symlink(str(safe / "src.txt"), "planted",
                                           dir_fd=inside),
    }
    try:
        with pytest.raises(guard.ProtectedPathWrite):
            calls[operation]()
    finally:
        os.close(inside)
        os.close(outside)


@needs_dir_fd
def test_dir_fd_writes_outside_the_protected_tree_still_work(armed):
    """對照組：描述子指向乾淨目錄時，寫入照常成功。"""
    _guard, tripwire, _protected, _lock, safe = armed
    directory = os.open(safe, os.O_RDONLY)
    try:
        handle = os.open("fine.txt", os.O_WRONLY | os.O_CREAT, dir_fd=directory)
        os.write(handle, b"ok")
        os.close(handle)
        os.mkdir("fine_dir", dir_fd=directory)
        os.unlink("fine.txt", dir_fd=directory)
    finally:
        os.close(directory)
    assert (safe / "fine_dir").is_dir()
    assert not tripwire.violations


@needs_dir_fd
def test_the_os_open_wrapper_is_transparent():
    """包裝之後，`os.open` 對「支不支援 dir_fd」的回答不得改變，也不得疊層。"""
    guard = _guard_module()
    guard._guard_dir_fd_opens(guard.WriteTripwire([]))
    first = os.open
    guard._guard_dir_fd_opens(guard.WriteTripwire([]))
    assert os.open is first, "a second install must reuse the wrapper"
    assert os.open in os.supports_dir_fd
    assert getattr(os.open, "__wrapped__", None) is not None
    assert not hasattr(os.open.__wrapped__, "_pcmef_tripwires"), (
        "the wrapper must wrap the real os.open, not another wrapper"
    )


def test_a_dir_fd_the_guard_cannot_place_is_refused(tmp_path, monkeypatch):
    """看不出描述子指向哪裡的平台：**不放行**。

    放行的意思是「猜它沒落在受保護的地方」，而這道守衛存在的理由正是
    不猜。這條在任何平台都跑：把「問 fd 在哪」的能力拿掉。
    """
    guard = _guard_module()
    monkeypatch.setattr(guard, "path_of_fd", lambda fd: None)
    tripwire = guard.WriteTripwire([tmp_path / "freeze"])
    with pytest.raises(guard.ProtectedPathWrite, match="dir_fd"):
        tripwire.check_write("anything.json", dir_fd=7)
    # 絕對路徑不相對於描述子，OS 也會忽略 dir_fd —— 照常判定。
    tripwire.check_write(str(tmp_path / "elsewhere.json"), dir_fd=7)


def test_ordinary_writes_never_ask_where_a_descriptor_points(tmp_path,
                                                             monkeypatch):
    """代價：沒有 dir_fd 的寫入一次都不去查描述子。"""
    guard = _guard_module()
    asked = {"n": 0}
    real = guard.path_of_fd

    def counting(fd):
        asked["n"] += 1
        return real(fd)

    monkeypatch.setattr(guard, "path_of_fd", counting)
    tripwire = guard.WriteTripwire([tmp_path / "freeze"])
    for index in range(50):
        tripwire.check_write(str(tmp_path / f"new{index}.json"))
    assert asked["n"] == 0


# ---------------------------------------------------------------------------
# 2 shutil.rmtree：在刪第一個檔案之前
# ---------------------------------------------------------------------------


def test_rmtree_of_a_protected_directory_deletes_nothing(armed):
    """「擋下了」不夠 —— **一個檔案都不得少。**

    POSIX 上 rmtree 以 dir_fd 逐一 unlink 裸檔名，最後才 rmdir 頂層。
    round 9 之前守衛只擋到最後那一個 rmdir：它回報擋下，而底下的檔案
    早就刪光了（Linux 實測）。夾具在結束時比對整棵樹。
    """
    guard, _tripwire, protected, _lock, _safe = armed
    with pytest.raises(guard.ProtectedPathWrite):
        shutil.rmtree(protected / "runs")


def test_rmtree_of_an_ancestor_is_refused_before_anything_goes(armed, tmp_path):
    """刪受保護根目錄的**祖先**，等於刪受保護的樹。鄰居也不得先被刪掉。"""
    guard, _tripwire, _protected, _lock, safe = armed
    (safe / "neighbour.txt").write_text("n", encoding="utf-8")
    # 比對完整的一句，不是單字：tmp 目錄的名字本身就含 "ancestor"，
    # 任何一個帶路徑的錯誤訊息都會讓 match="ancestor" 成立。
    with pytest.raises(guard.ProtectedPathWrite,
                       match="an ancestor of a protected tree"):
        shutil.rmtree(tmp_path)
    assert (safe / "neighbour.txt").exists(), (
        "the refusal must come before rmtree removes anything at all"
    )


def test_rmtree_of_an_unrelated_tree_still_works(armed):
    """對照組：刪乾淨的樹照常。"""
    _guard, tripwire, _protected, _lock, safe = armed
    (safe / "scratch" / "deep").mkdir(parents=True)
    (safe / "scratch" / "deep" / "x.txt").write_text("x", encoding="utf-8")
    shutil.rmtree(safe / "scratch")
    assert not (safe / "scratch").exists()
    assert not tripwire.violations


# ---------------------------------------------------------------------------
# 3 O_TRUNC
# ---------------------------------------------------------------------------


def test_a_read_only_open_with_o_trunc_is_a_write(armed):
    """Linux 上 `O_RDONLY | O_TRUNC` 把檔案截成空的（實測），而它看起來
    是一次唯讀開檔。守衛在 OS 之前判定，所以每個平台都擋得到。"""
    guard, _tripwire, _protected, lock, _safe = armed
    with pytest.raises(guard.ProtectedPathWrite):
        os.open(lock, os.O_RDONLY | os.O_TRUNC)


def test_a_plain_read_only_open_is_still_a_read(armed):
    """對照組：真正的唯讀照常放行 —— 讀取不得被擋。"""
    _guard, tripwire, _protected, lock, _safe = armed
    handle = os.open(lock, os.O_RDONLY)
    try:
        assert os.read(handle, 64) == ORIGINAL.encode()
    finally:
        os.close(handle)
    assert not tripwire.violations


# ---------------------------------------------------------------------------
# 4 hardlink：同一個檔案的另一個名字
# ---------------------------------------------------------------------------


def _hardlink_or_skip(target: Path, alias: Path) -> None:
    try:
        os.link(target, alias)
    except OSError as error:
        pytest.skip(f"this filesystem cannot create hardlinks: {error}")


def test_a_preexisting_hardlink_to_a_protected_file_is_blocked(tmp_path):
    """hardlink 不是 symlink、不是 reparse point，路徑解析從哪一邊出發都
    只回到它自己。round 9 之前 `open(alias, "w")` 在 Windows 與 Linux 上
    都直接改掉受保護的內容（實測）。"""
    guard = _guard_module()
    protected, lock, safe = _protected_tree(tmp_path)
    alias = safe / "alias.json"
    _hardlink_or_skip(lock, alias)
    assert not os.path.islink(alias), "premise: this is not a symlink"

    tripwire = guard.WriteTripwire([protected])
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(str(alias))
    assert lock.read_text(encoding="utf-8") == ORIGINAL


def test_the_audit_hook_blocks_a_write_through_a_hardlink(tmp_path, hooked):
    """端到端：真的 `open(alias, "w")` 與 `os.truncate(alias)`。

    hardlink 要在守衛生效**之前**建好：生效之後 `os.link` 本身就會
    被擋 —— 這條測的正是 session 開始前就已經在磁碟上的那一種。
    """
    guard, switch = hooked
    protected, lock, safe = _protected_tree(tmp_path)
    alias = safe / "alias.json"
    _hardlink_or_skip(lock, alias)
    switch.current = guard.WriteTripwire([protected])
    try:
        with pytest.raises(guard.ProtectedPathWrite):
            with open(alias, "w", encoding="utf-8") as handle:
                handle.write("TAMPERED")
        with pytest.raises(guard.ProtectedPathWrite):
            os.truncate(alias, 0)
    finally:
        switch.current = None
    assert lock.read_text(encoding="utf-8") == ORIGINAL


def test_a_hardlink_whose_name_mentions_a_root_is_still_blocked(tmp_path):
    """名字粗篩命中之後 resolve() 會說「不在受保護樹裡」—— 對 hardlink
    而言那句話是對的，也是不夠的。"""
    guard = _guard_module()
    protected, lock, safe = _protected_tree(tmp_path)
    (safe / "freeze_copy").mkdir()
    alias = safe / "freeze_copy" / "lock.json"
    _hardlink_or_skip(lock, alias)
    tripwire = guard.WriteTripwire([protected])
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(str(alias))


def test_ordinary_existing_files_never_build_the_inode_index(tmp_path):
    """代價：只有一個名字的檔案不可能是別人的別名，身分索引不得被建。"""
    guard = _guard_module()
    protected, _lock, safe = _protected_tree(tmp_path)
    tripwire = guard.WriteTripwire([protected])
    for index in range(20):
        existing = safe / f"existing{index}.json"
        existing.write_text("x", encoding="utf-8")
        tripwire.check_write(str(existing))
        tripwire.check_write(str(safe / f"missing{index}.json"))
    assert tripwire._inodes is None, "the inode index was built for nothing"
    assert not tripwire.violations


def test_the_inode_index_is_built_once(tmp_path, monkeypatch):
    guard = _guard_module()
    protected, lock, safe = _protected_tree(tmp_path)
    alias = safe / "alias.json"
    _hardlink_or_skip(lock, alias)
    walks = {"n": 0}
    real_walk = os.walk

    def counting(*args, **kwargs):
        walks["n"] += 1
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(guard.os, "walk", counting)
    tripwire = guard.WriteTripwire([protected])
    for _ in range(10):
        with pytest.raises(guard.ProtectedPathWrite):
            tripwire.check_write(str(alias))
    assert walks["n"] == 1


def test_an_unrelated_hardlink_pair_is_allowed(tmp_path):
    """對照組：兩個都在乾淨目錄裡的 hardlink，寫入照常。"""
    guard = _guard_module()
    protected, _lock, safe = _protected_tree(tmp_path)
    first = safe / "a.json"
    first.write_text("a", encoding="utf-8")
    _hardlink_or_skip(first, safe / "b.json")
    tripwire = guard.WriteTripwire([protected])
    tripwire.check_write(str(safe / "b.json"))
    assert not tripwire.violations


# ---------------------------------------------------------------------------
# 5 listener：擁有權，不是 port 號碼
# ---------------------------------------------------------------------------


def test_allowing_zero_verifies_the_socket_it_actually_opened(request):
    """port 0 由 OS 挑號。收尾看的是**那個 socket 物件**，所以號碼不重要。"""
    guard = _session_guard(request)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError) as caught:
            with guard.LISTENERS.allowing(0):
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
                port = sock.getsockname()[1]
        assert type(caught.value).__name__ == "RealListenerOpened"
        assert str(port) in str(caught.value)
        assert sock.fileno() == -1, (
            "a socket this process bound and left open is closed by the guard, "
            "so it cannot outlive the test"
        )
    finally:
        sock.close()


def test_allowing_zero_needs_no_machine_wide_inventory(request, monkeypatch):
    """收尾驗證不依賴 psutil：拿掉行程盤點之後，socket 物件仍然說了算。"""
    guard = _session_guard(request)
    monkeypatch.setattr(guard, "owned_listeners", lambda: None)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError):
            with guard.LISTENERS.allowing(0):
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
    finally:
        sock.close()

    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with guard.LISTENERS.allowing(0):
        closed.bind(("127.0.0.1", 0))
        closed.listen(1)
        closed.close()


def test_the_official_ui_port_cannot_even_be_declared(request):
    """在**進入**時就拒絕，而不是離開時碰巧看到 8790 在 LISTEN。

    8790 平常就被正式 UI 佔著；一個在離開時盤點 port 的實作會因此
    「剛好」丟出含 8790 的錯誤 —— 那是通過，但什麼都沒守。
    """
    guard = _session_guard(request)
    entered = False
    with pytest.raises(AssertionError,
                       match="no test may declare it") as caught:
        with guard.LISTENERS.allowing(guard.OFFICIAL_UI_PORT):
            entered = True
    assert type(caught.value).__name__ == "RealListenerOpened"
    assert not entered, "the declaration itself must be refused"


def test_a_duplicated_listener_is_caught_by_process_ownership(request):
    """close 了原本的物件、卻留著一份 dup 出來的 fd —— port 仍然被佔著。

    socket 物件說「已關」，只有行程層級的盤點看得到真相。
    """
    _psutil_or_skip()
    guard = _session_guard(request)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    duplicate = None
    try:
        with pytest.raises(AssertionError) as caught:
            with guard.LISTENERS.allowing(0):
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
                port = sock.getsockname()[1]
                duplicate = sock.dup()
                sock.close()
        assert f"port {port}" in str(caught.value)
    finally:
        sock.close()
        if duplicate is not None:
            duplicate.close()


_CHILD_LISTENER = textwrap.dedent("""
    import socket, sys, time
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    print(s.getsockname()[1], flush=True)
    time.sleep(60)
""")


def test_a_child_process_listener_declared_in_scope_must_be_gone(request):
    """子行程開的 server 稽核事件看不到；看得到的是 pytest 自己的行程樹。

    測試收掉**它自己生的**這個子行程；守衛只回報，不代為終止。
    """
    _psutil_or_skip()
    guard = _session_guard(request)
    child = None
    try:
        with pytest.raises(AssertionError) as caught:
            with guard.LISTENERS.allowing(0):
                child = subprocess.Popen(
                    [sys.executable, "-c", _CHILD_LISTENER],
                    stdout=subprocess.PIPE, text=True,
                )
                port = int(child.stdout.readline())
        assert f"pid {child.pid}" in str(caught.value)
        assert f"port {port}" in str(caught.value)
        assert child.poll() is None, "the guard must not terminate the child"
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=30)
            child.stdout.close()


def test_owned_listeners_are_only_ours(request):
    """8790 由正式 UI 佔著時，它不得出現在「測試擁有的」清單裡。"""
    psutil = _psutil_or_skip()
    guard = _session_guard(request)
    owned = guard.owned_listeners()
    assert owned is not None
    ours = {pid for pid, _port in owned}
    family = {psutil.Process().pid} | {
        p.pid for p in psutil.Process().children(recursive=True)
    }
    assert ours <= family, "only this process tree may appear"
    assert guard.OFFICIAL_UI_PORT not in {port for _pid, port in owned}


# ---------------------------------------------------------------------------
# 6 子行程：開的人要負責收
# ---------------------------------------------------------------------------

_SLEEPER = "import time; time.sleep(60)"


def test_a_child_left_running_is_reported_and_not_killed():
    _psutil_or_skip()
    guard = _guard_module()
    ledger = guard.ChildProcessLedger(grace=0.3)
    ledger.start()
    child = subprocess.Popen([sys.executable, "-c", _SLEEPER])
    try:
        ledger.note_spawn()
        leaked = ledger.settle("this test")
        assert any(f"pid {child.pid}" in note for note in leaked), leaked
        assert child.poll() is None, "the ledger reports; it does not kill"
        assert ledger.settle("again", always=True) == [], (
            "a leak is reported once"
        )
    finally:
        child.kill()
        child.wait(timeout=30)


def test_a_child_that_finishes_in_time_is_not_a_leak():
    _psutil_or_skip()
    guard = _guard_module()
    ledger = guard.ChildProcessLedger(grace=10.0)
    ledger.start()
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    ledger.note_spawn()
    try:
        assert ledger.settle("this test") == []
    finally:
        child.wait(timeout=30)


def test_no_spawn_means_no_process_listing(monkeypatch):
    """代價：這段期間沒有 spawn，就不去列行程樹。"""
    guard = _guard_module()
    listed = {"n": 0}
    monkeypatch.setattr(
        guard, "_family", lambda: listed.__setitem__("n", listed["n"] + 1)
    )
    ledger = guard.ChildProcessLedger()
    for _ in range(100):
        assert ledger.settle("quiet") == []
    assert listed["n"] == 0


def test_spawning_is_seen_by_the_session_ledger(request):
    """session 的帳本真的收到 spawn 的稽核事件。"""
    guard = _session_guard(request)
    before = guard.PROCESSES.spawns
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert guard.PROCESSES.spawns > before


# ---------------------------------------------------------------------------
# 7 科學完成點
# ---------------------------------------------------------------------------


def _formal_args(tmp_path, **overrides):
    args = argparse.Namespace(
        mode="dry-run", base=str(tmp_path / "base"), out=str(tmp_path / "out"),
        ds_dir=str(tmp_path / "ds"), freeze_dir=str(tmp_path / "freeze"),
        lineage_root=str(tmp_path / "freeze"), registry_dir=str(tmp_path / "reg"),
        vision_severity=None, tof_severity=None, agent_cache=None,
        allow_dirty=False, confirm="", run_id=None, resume=False,
        run_events=str(tmp_path / "run"),
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _document():
    return {
        "report_path": "r.json", "dry_run": True, "scientific_result": "n/a",
        "llm_arm_evaluated": False, "dataset": {"total_rows": 0},
        "routing": {"counts": {}, "escalated_cases": 0, "escalation_rate": 0.0},
        "skipped_escalated_cases": 0, "results": {},
        "worst_condition_macro_f1": {}, "worst_condition_at": {},
    }


def _terminal_events(events_dir: Path):
    from pcmef.platform.runs import STAGE_COMPLETED, STAGE_FAILED, read_events

    return [
        event.event for event in read_events(events_dir)[0]
        if event.event in (STAGE_COMPLETED, STAGE_FAILED)
    ]


@pytest.fixture()
def formal(tmp_path, monkeypatch):
    """pre-flight 與 executor 都換成樁；回傳「要讓科學怎麼結束」的開關。"""
    monkeypatch.setattr(
        "pcmef.cli._formal_preflight",
        lambda args: {"checks": [], "allowed": True, "blockers": [],
                      "lineage": {"resolved_freeze_dir": str(tmp_path / "freeze")}},
    )
    behaviour = {"science": lambda: _document()}
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        lambda *a, **k: behaviour["science"](),
    )
    handler = signal.getsignal(signal.SIGINT)
    yield behaviour
    assert signal.getsignal(signal.SIGINT) is handler, (
        "an in-process call must leave the SIGINT handler as it found it"
    )


def _finish(args) -> int:
    """跑一次 Formal，**越界之後漏出來的中斷算測試失敗，不算中斷測試。**

    沒有這一層的話，一次回歸會讓 KeyboardInterrupt 穿出測試，pytest
    把它當成使用者按了 Ctrl-C，整輪就此停下 —— 失敗被記成「沒跑完」。
    """
    from pcmef import cli

    try:
        return cli.cmd_formal_run_e2(args)
    except (KeyboardInterrupt, SystemExit) as error:
        pytest.fail(
            f"{type(error).__name__} escaped after the scientific result was "
            f"complete: {error!r}"
        )


class _Raising:
    """在第 n 次寫入時丟出指定的例外。科學已經回來了 —— 那正是要守的時刻。"""

    def __init__(self, error: BaseException, after: int = 0) -> None:
        self.error, self.after, self.writes = error, after, 0

    def write(self, text):
        self.writes += 1
        if self.writes > self.after:
            raise self.error
        return len(text)

    def flush(self):
        return None


def test_ctrl_c_during_the_summary_does_not_fail_a_finished_run(
    tmp_path, formal, monkeypatch
):
    """round 8 只接 Exception：這一個 KeyboardInterrupt 會穿到入口，寫下
    stage_failed、以非零結束 —— 而 report 與 claim 都已經完成。"""
    def science():
        monkeypatch.setattr(sys, "stdout", _Raising(KeyboardInterrupt(), after=2))
        return _document()

    formal["science"] = science
    assert _finish(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_system_exit_during_the_summary_does_not_fail_a_finished_run(
    tmp_path, formal, monkeypatch
):
    def science():
        monkeypatch.setattr(sys, "stdout", _Raising(SystemExit(3)))
        return _document()

    formal["science"] = science
    assert _finish(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_an_interrupt_while_warning_about_the_summary_is_still_absorbed(
    tmp_path, formal, monkeypatch
):
    """stdout 與 stderr 都在丟 KeyboardInterrupt：連警告都印不出來。"""
    def science():
        monkeypatch.setattr(sys, "stdout", _Raising(KeyboardInterrupt()))
        monkeypatch.setattr(sys, "stderr", _Raising(KeyboardInterrupt()))
        return _document()

    formal["science"] = science
    assert _finish(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_a_real_sigint_after_the_boundary_is_recorded_not_raised(
    tmp_path, formal, monkeypatch, capsys
):
    """真的送一個 SIGINT，而且在科學完成之後。它只被記下，不改判。"""
    class _SignalOnWrite:
        def __init__(self, inner):
            self.inner, self.sent = inner, False

        def write(self, text):
            if not self.sent:
                self.sent = True
                signal.raise_signal(signal.SIGINT)
            return self.inner.write(text)

        def flush(self):
            return self.inner.flush()

    def science():
        monkeypatch.setattr(sys, "stdout", _SignalOnWrite(sys.stdout))
        return _document()

    formal["science"] = science
    assert _finish(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]
    assert "interrupt arrived after the scientific result" in capsys.readouterr().err


@pytest.mark.parametrize("when", ["after_writing", "before_writing"])
def test_an_interrupt_inside_the_success_event_is_settled(
    tmp_path, formal, monkeypatch, when
):
    """中斷落在終局事件的寫入本身：寫完之後、或還沒寫。兩種都以一筆
    stage_completed 收場，而不是零筆或多一筆 stage_failed。"""
    from pcmef import cli

    real = cli._StageEvents.stage_completed
    fired = {"done": False}

    def interrupted(self, *args, **kwargs):
        if fired["done"]:
            return real(self, *args, **kwargs)
        fired["done"] = True
        if when == "after_writing":
            real(self, *args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli._StageEvents, "stage_completed", interrupted)
    assert _finish(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_ctrl_c_before_the_executor_returns_still_interrupts(tmp_path, formal):
    """越界之前完全不動：科學還在跑的時候，Ctrl-C 就是中斷。"""
    from pcmef import cli

    def science():
        raise KeyboardInterrupt()

    formal["science"] = science
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_formal_run_e2(_formal_args(tmp_path))
    assert _terminal_events(tmp_path / "run") == ["stage_failed"]


@pytest.mark.parametrize("error", [SystemExit(4), RuntimeError("statistics")])
def test_a_genuine_failure_before_the_return_is_not_swallowed(
    tmp_path, formal, error
):
    from pcmef import cli

    def science():
        raise error

    formal["science"] = science
    with pytest.raises(type(error)):
        cli.cmd_formal_run_e2(_formal_args(tmp_path))
    assert _terminal_events(tmp_path / "run") == ["stage_failed"]


_CHILD_FORMAL = textwrap.dedent("""
    import signal, sys
    sys.path.insert(0, {repo!r})
    from pcmef import cli
    import pcmef.experiments.e2_formal as e2

    WHEN = {when!r}
    cli._formal_preflight = lambda args: {{
        "checks": [], "allowed": True, "blockers": [],
        "lineage": {{"resolved_freeze_dir": {tmp!r}}},
    }}

    def science(*a, **k):
        if WHEN == "during-science":
            signal.raise_signal(signal.SIGINT)
            sum(range(100000))
        return {{
            "report_path": "r.json", "dry_run": True, "scientific_result": "n/a",
            "llm_arm_evaluated": False, "dataset": {{"total_rows": 0}},
            "routing": {{"counts": {{}}, "escalated_cases": 0,
                        "escalation_rate": 0.0}},
            "skipped_escalated_cases": 0, "results": {{}},
            "worst_condition_macro_f1": {{}}, "worst_condition_at": {{}},
        }}

    e2.run_formal_e2_full = science

    class Interrupting:
        def __init__(self, inner):
            self.inner, self.sent = inner, False
        def write(self, text):
            if WHEN == "summary" and not self.sent and "report" in text:
                self.sent = True
                signal.raise_signal(signal.SIGINT)
            return self.inner.write(text)
        def flush(self):
            return self.inner.flush()

    sys.stdout = Interrupting(sys.stdout)
    sys.argv = ["pcmef", "formal", "run-e2", "--mode", "dry-run",
                "--base", {tmp!r} + "/base", "--out", {tmp!r} + "/out",
                "--ds-dir", {tmp!r} + "/ds", "--registry-dir", {tmp!r} + "/reg",
                "--lineage-root", {tmp!r} + "/freeze", "--run-id", "x",
                "--run-events", {events!r}]
    code = cli.main()
    if WHEN == "after-main":
        signal.raise_signal(signal.SIGINT)
        sum(range(100000))
    raise SystemExit(code)
""")


@pytest.mark.parametrize("when, expected", [
    ("summary", "succeeded"),
    ("after-main", "succeeded"),
    ("during-science", "failed"),
])
def test_the_persisted_run_record_follows_the_scientific_boundary(
    tmp_path, when, expected
):
    """端到端：真的子行程、真的 SIGINT、console 依 exit code 寫下的 run.json。

    越界之後（摘要印到一半、甚至 main() 已經回來）的中斷不得把 run.json
    寫成 failed；越界之前的中斷必須照常是 failed。
    """
    from pcmef.console.runner import ConsoleRunner, RunSpec

    script = tmp_path / "child.py"

    class _FormalChild(ConsoleRunner):
        def _command(self, run_id, spec):
            script.write_text(_CHILD_FORMAL.format(
                repo=str(REPO), when=when, tmp=str(tmp_path).replace("\\", "/"),
                events=str(self.run_dir(run_id)),
            ), encoding="utf-8")
            return [sys.executable, "-u", str(script)]

    runner = _FormalChild(tmp_path / "runs")
    record = runner.start(RunSpec(kind="sim_smoke", params={}))
    final = runner.wait(record.run_id, timeout=120)

    log = runner.log_path(record.run_id).read_text(encoding="utf-8")
    assert final.status == expected, f"exit={final.exit_code}\n{log}"
    events = _terminal_events(runner.run_dir(record.run_id))
    assert events == (
        ["stage_completed"] if expected == "succeeded" else ["stage_failed"]
    ), log
    if expected == "succeeded":
        stored = json.loads(
            runner.record_path(record.run_id).read_text(encoding="utf-8")
        )
        assert stored["exit_code"] == 0
