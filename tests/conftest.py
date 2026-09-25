# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 在 session 開始時自動載入，涵蓋 tests/ 底下每一個
#         測試。不被任何 production 模組匯入。
# 檔案路徑: tests/conftest.py
# 產生時間: 2026-09-07 11:05 +08:00
# 版本: v0.6.0
# 功能說明: 跑測試不得改到 repo 的科研輸出與專案 metadata —— 這裡在
#           session 前後各取一次指紋，變了就讓整輪失敗。
# 模組定位: Execution Layer Closure round 3 的第 6 項。
#           靜態掃描擋不住這一類污染：`tests/e2/test_artifact_isolation.py`
#           正當地拿 canonical 路徑做位址運算而從不寫入，而一個沿用
#           預設 `PCMEF_FORMAL_OUT` 的 fixture 會真的寫進去。能分辨
#           兩者的只有「跑完之後有沒有變」。
# 主要責任:
#   1. WATCHED_PATHS 列出不得被測試改動的位置
#   2. fingerprint() 以內容雜湊描述一組路徑的現況
#   3. repo_is_not_polluted 在 session 前後比對並在變動時失敗
#   4. PROTECTED_PATHS / WriteTripwire 在寫入當下就攔下
#   5. repo_is_not_written_to 以稽核事件掛上 tripwire
#   6. ListenerTripwire 攔下測試開出來的真實 TCP listener，並以擁有權
#      （綁定它的 socket 物件、pytest 自己的行程樹）確認宣告過的
#      listener 在離開時已經收掉
#   7. ChildProcessLedger 找出測試生出來、測試結束後仍活著的子行程
# 維護提醒:
#   - **不得把這個守衛改成只警告。** 污染是靜默的：outputs/ 不進版控，
#     所以除了這裡沒有任何東西會告訴你它被寫過。
#   - 不得為了讓某個測試通過而把路徑從 WATCHED_PATHS 移走。要改的是
#     那個測試，讓它寫進 tmp_path。
#   - 不得改成只比對 mtime。內容相同而 mtime 變了不算污染，內容變了
#     而 mtime 沒變才是真正危險的那一種。
#   - **不得只留指紋比對。** 寫進去再刪掉的話前後指紋一樣，而中間
#     那段時間確實存在一份假資料；抓得到它的只有寫入當下的攔截。
#   - 不得讓 tripwire 擋到讀取。tests/e2 正當地讀 canonical 路徑做
#     位址運算，擋到它就會有人把整個守衛關掉。
#   - **不得把粗篩改回比對絕對路徑前綴。** 相對路徑（最常見的寫法）
#     一個字都對不上，於是在 resolve() 之前就被放行 —— 整道守衛
#     等於只擋得住已經寫成絕對路徑的那一種。
#   - **不得用相對片段當快取的鍵。** `"."` 在 chdir 之後指的是別的
#     地方；沿用舊答案就是一條走得通的繞道。鍵一律先 abspath()。
#   - **不得只解析所在目錄就下判斷。** 最後一段自己可以是 symlink 或
#     junction；父層乾淨不代表寫入落點乾淨。判定別名時也不得只用
#     `os.path.islink()` —— junction 會讓它回傳 False。
#   - **不得忽略稽核事件裡的 dir_fd。** `os.remove(name, dir_fd=fd)` 的
#     `name` 相對於 fd 指向的目錄，不是 cwd；照 cwd 去判等於沒判。
#     看不出 fd 指向哪裡的平台一律 fail closed，不猜。
#   - **`open` 稽核事件不帶 dir_fd**，所以 `os.open(..., dir_fd=)` 只能
#     在 os.open 本身攔（`_guard_dir_fd_opens`）。不得拿掉那一層，也不得
#     把它推廣成 monkeypatch 一般寫入 —— 其餘寫入仍由稽核事件負責。
#   - 不得把 O_TRUNC 從 WRITE_FLAGS 拿掉：Linux 上 `O_RDONLY | O_TRUNC`
#     會把檔案截成空的，而它看起來是一次唯讀開檔。
#   - **不得用路徑判定 hardlink。** 它沒有 target 可以解析，兩個名字
#     地位完全相同；判準是 (st_dev, st_ino)，而且只在目標已存在、
#     st_nlink > 1 時才查。受保護檔案的身分索引第一次需要時才建。
#   - **不得改回以全機 port 盤點判定 listener 洩漏。** 全機盤點會把
#     別的程式（瀏覽器、Docker、8790 上的正式 UI）開的 port 算到測試
#     頭上，而 port 0 的測試根本說不出自己開了哪一個。判準是擁有權。
#   - 子行程洩漏只回報、**不代為終止**：那個行程可能正在寫檔，而終止
#     它的判斷屬於操作者。本守衛也不需要、不嘗試管理員權限。
#   - v0.6.0 修正：稽核事件的 dir_fd 攤成實際落點；`os.open` 的 dir_fd
#     寫入改由包裝攔下；`shutil.rmtree` 在刪第一個檔案之前判定；
#     O_TRUNC 算寫入；hardlink 以 inode 身分判定；`allowing()` 以
#     socket 物件與行程樹確認收尾（含 port 0）；新增子行程洩漏檢查。
#     對應 round 9 的第 1~3 項。
#   - v0.5.0 修正：最後一段是別名時也解析並判定，涵蓋 symlink 與
#     Windows junction。對應 round 8 的第 1 項。
#   - v0.4.0 修正：目錄快取的鍵改為絕對路徑，切換 cwd 之後不再沿用
#     舊解析。對應 round 7 的第 1 項。
#   - v0.3.0 修正：粗篩改比對根目錄名稱，讓相對路徑、`..` 與 symlink
#     真的走到 resolve()。對應 round 5 的第 3 項。
#   - v0.2.0 新增：WriteTripwire 以稽核事件攔下寫入，對應 round 4 的
#     第 4 項（寫了又刪同樣算污染）。
#   - v0.1.0 新增：首版，對應 round 3 的第 6 項。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_hardening.py -v
#   - py -3.10 -m pytest tests/console/test_execution_boundaries.py -v
# ------------------------------------------------------------

from __future__ import annotations

import contextlib
import functools
import hashlib
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 跑測試不得改動的位置。
#:
#: 兩者都被真的咬過：canonical Final E2 目錄收到過一個 fixture 寫的
#: 假 report；`projects/pcmef-thesis/project.json` 被遷移的能力補寫
#: 改過，而那是一個進版控的檔案。
WATCHED_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "outputs" / "perception" / "e2_final",
    REPO_ROOT / "projects" / "pcmef-thesis" / "project.json",
    REPO_ROOT / "freeze",
)

#: 測試**一次都不得寫入**的位置。比 WATCHED_PATHS 更嚴格。
#:
#: 前後指紋比對抓不到「寫進去、跑完自己刪掉」：兩端一樣，中間卻確實
#: 存在過一份假資料，而任何併行的讀取都會看到它。這一組因此改用
#: 寫入當下就攔下來的方式。
#:
#: `projects` 收整個目錄而不是單一檔案：新增一個專案目錄同樣是在
#: 真實 workspace 上動手。
PROTECTED_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "outputs" / "perception" / "e2_final",
    REPO_ROOT / "freeze",
    REPO_ROOT / "projects",
)



#: 開檔旗標裡代表「會改到內容」的那些。
#:
#: **O_TRUNC 單獨也算。** Linux 上 `O_RDONLY | O_TRUNC` 會把檔案截成空的
#: —— POSIX 說結果未定義，Linux 照做 —— 而先前的判斷只看 WRONLY /
#: RDWR / CREAT / APPEND，於是一次看起來唯讀的開檔就能清空受保護的檔案。
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _fd_path_reader():
    """這個平台上「一個 fd 指向哪裡」要怎麼問。問不了就回 None。

    只在載入時決定一次：寫入判定跑在每一次開檔上，不能每次都去試
    `/proc` 在不在、`fcntl` 能不能匯入。
      - Linux：`/proc/self/fd/N` 是指向實際位置的符號連結。
      - macOS：`fcntl(fd, F_GETPATH)`。
      - Windows：沒有 dir_fd（`os.supports_dir_fd` 是空集合），不需要。
    """
    if os.path.isdir("/proc/self/fd"):
        return lambda fd: os.readlink(f"/proc/self/fd/{fd}")
    try:
        import fcntl
    except ImportError:
        return None
    request = getattr(fcntl, "F_GETPATH", None)
    if request is None:
        return None

    def read(fd):
        raw = fcntl.fcntl(fd, request, bytes(1024))
        return os.fsdecode(raw.split(b"\0", 1)[0])

    return read


_READ_FD_PATH = _fd_path_reader()


def path_of_fd(fd) -> str | None:
    """一個 file descriptor 指向的絕對路徑。看不到就回 None。

    `*_at` 系列（`dir_fd=`）的路徑相對於一個**目錄描述子**，不是 cwd。
    守衛要判斷落點，就得先知道那個描述子指向哪裡。回 None 時由呼叫端
    決定要不要 fail closed —— 這裡不猜。
    """
    if _READ_FD_PATH is None or not isinstance(fd, int) or isinstance(fd, bool):
        return None
    if fd < 0:
        return None
    try:
        target = _READ_FD_PATH(fd)
    except (OSError, ValueError):
        return None
    # 目錄被刪掉之後 /proc 會補上這個尾巴；路徑本身仍是它原本的位置。
    if target.endswith(" (deleted)"):
        target = target[: -len(" (deleted)")]
    return target if os.path.isabs(target) else None


class ProtectedPathWrite(AssertionError):
    """測試試圖寫入受保護的位置。**當場擋下，不等到最後才比對。**"""


class _Unplaceable(Exception):
    """寫入相對於一個看不出指向哪裡的 dir_fd。"""


class WriteTripwire:
    """記錄並拒絕對受保護路徑的寫入。

    只擋寫入。讀取必須照常 —— `tests/e2/test_artifact_isolation.py`
    正當地讀 canonical 路徑做位址運算，擋到它就會有人把整個守衛關掉。
    """

    def __init__(self, protected) -> None:
        self._roots = [Path(p).resolve() for p in protected]
        # 粗篩比對的是每個受保護根目錄的**最後一段名字**，不是它的
        # 絕對路徑前綴。
        #
        # 先前用絕對前綴，於是 `outputs/perception/e2_final/x.json`
        # 這種相對寫法一個字都對不上，在 resolve() 之前就被放行 ——
        # 而相對路徑正是最常見的寫法。
        #
        # 名字比對會有誤判（`freeze_candidate` 也含 "freeze"），
        # 那沒關係：粗篩只負責決定要不要付 resolve() 的代價，
        # 真正的判定在下面。**寧可多解析幾次，不可少擋一次。**
        self._needles = tuple(
            root.name.lower() for root in self._roots if root.name
        )
        #: 目錄 → 解析後的真實位置。**每個目錄只解析一次。**
        #:
        #: symlink 別名的名字裡可以一個 needle 都沒有（`shortcut`
        #: 指向 `freeze`），所以光靠名字比對看不穿它，而唯一看得穿的
        #: 是 resolve()。無條件解析每一次 open 會讓整輪測試慢到有人
        #: 想關掉守衛；改成解析**目錄**並記住答案之後，代價變成
        #: 「每個不同目錄一次 syscall」，而一輪測試的目錄數是幾百，
        #: 寫入次數是幾十萬。
        self._parent_cache: dict[str, Path | None] = {}
        #: 受保護檔案的 (st_dev, st_ino)。**第一次真的需要時才建。**
        #:
        #: 只有「寫入目標已存在、而且 st_nlink > 1」才需要它，而那在
        #: 一輪測試裡幾乎不會發生 —— 絕大多數寫入的目標還不存在，其餘
        #: 的絕大多數只有一個名字。因此多數 session 從頭到尾不會建它。
        self._inodes: frozenset[tuple[int, int]] | None = None
        #: 曾經嘗試過的寫入。即使被擋下也留著 —— 「試過」本身就是
        #: 要修的東西。
        self.violations: list[str] = []

    def _under_a_root(self, resolved: Path) -> bool:
        return any(
            resolved == root or root in resolved.parents for root in self._roots
        )

    def _resolved_parent(self, raw: str) -> Path | None:
        """這條路徑的所在目錄，攤平 symlink 與 `..` 之後。有快取。

        **快取的鍵必須是絕對路徑。** 先前用的是
        `os.path.dirname(raw) or "."`，於是一個裸檔名永遠落在鍵 `"."`
        上 —— 而 `"."` 在 `chdir()` 之後指的是別的地方。在普通目錄下
        寫過一次之後，守衛記住的是舊 cwd 的答案；接著切進受保護目錄
        再用裸檔名寫，它會沿用那個舊答案並放行。

        `abspath()` 只做字面拼接與正規化（相對路徑時取一次 cwd），
        不走 symlink —— 真正的攤平仍然由下面的 `resolve()` 負責。
        因此鍵綁定了「當下的 cwd」，而每個實際目錄仍然只解析一次。
        """
        try:
            key = os.path.abspath(os.path.dirname(raw) or os.curdir)
        except Exception:  # noqa: BLE001 - 取不到 cwd 就不要猜
            return None
        if key in self._parent_cache:
            return self._parent_cache[key]
        try:
            resolved = Path(key).resolve()
        except Exception:  # noqa: BLE001
            resolved = None
        self._parent_cache[key] = resolved
        return resolved

    @staticmethod
    def _lstat(raw: str):
        """不跟著連結走的 stat。目標還不存在就回 None。

        一次 syscall。絕大多數寫入的目標還不存在（建立它的就是這次
        寫入），代價就停在這裡的 ENOENT。
        """
        try:
            return os.lstat(raw)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _is_an_alias(info) -> bool:
        """這個 stat 結果是不是一個別名（symlink 或 Windows reparse point）。

        **不得用 `os.path.islink()` 單獨判定。** 它對 Windows 的
        junction 回傳 False，而 `resolve()` 照樣跟著走 —— 於是
        junction 變成一條「守衛看不見、作業系統卻認得」的繞道。
        這裡改看 reparse point 屬性，symlink 與 junction 一起涵蓋。
        """
        if stat.S_ISLNK(info.st_mode):
            return True
        # Windows：junction 與 directory symlink 都是 reparse point，
        # 但只有後者會讓 S_ISLNK 成立。
        if getattr(info, "st_reparse_tag", 0):
            return True
        attributes = getattr(info, "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    def _protected_inodes(self) -> frozenset[tuple[int, int]]:
        """受保護的每一個一般檔案的 (st_dev, st_ino)。建一次，之後沿用。

        用 lstat 而不是 scandir 的快取結果：Windows 上 `DirEntry.stat()`
        的 st_ino 與 st_dev 是 0。
        """
        if self._inodes is None:
            found: set[tuple[int, int]] = set()
            for root in self._roots:
                for directory, _subdirs, files in os.walk(root):
                    for name in files:
                        info = self._lstat(os.path.join(directory, name))
                        if info is not None and stat.S_ISREG(info.st_mode) \
                                and info.st_ino:
                            found.add((info.st_dev, info.st_ino))
            self._inodes = frozenset(found)
        return self._inodes

    def _shares_a_protected_inode(self, info) -> bool:
        """這個已存在的檔案是不是某個受保護檔案的**另一個名字**。

        hardlink 沒有 target、沒有 reparse point，兩個名字地位完全相同；
        路徑解析從任何一邊出發都只會回到它自己。唯一的共同點是身分。

        `st_nlink < 2` 當場返回：只有一個名字的檔案不可能是別人的別名，
        而那是幾乎所有已存在檔案的情況 —— 身分索引因此幾乎不會被建。
        """
        if info is None or not stat.S_ISREG(info.st_mode):
            return False
        if info.st_nlink < 2 or not info.st_ino:
            return False
        return (info.st_dev, info.st_ino) in self._protected_inodes()

    def _resolves_into_a_root(self, raw: str) -> bool:
        try:
            return self._under_a_root(Path(raw).resolve())
        except Exception:  # noqa: BLE001
            return False

    def _is_protected(self, raw: str) -> bool:
        # 第一關：名字上就帶著受保護根目錄的名字。相對與絕對都算，
        # 不打任何 syscall。命中就直接做完整解析。
        lowered = raw.lower()
        if any(needle in lowered for needle in self._needles):
            if self._resolves_into_a_root(raw):
                return True
            # resolve() 已經走過每一段連結，看不穿的只剩 hardlink。
            try:
                return self._shares_a_protected_inode(os.stat(raw))
            except (OSError, ValueError):
                return False

        # 第二關：名字什麼都沒說 —— symlink 別名走的就是這一條。
        # 解析所在目錄（有快取）再判一次。少了這一關，一個叫
        # `shortcut` 的別名就能整個繞過守衛。
        parent = self._resolved_parent(raw)
        if parent is None:
            return False
        if self._under_a_root(parent):
            return True

        # 第三關：父層乾淨，但**最後一段自己是別名**。
        #
        # 少了這一關，`safe/alias.json -> freeze/.../lock.json` 一路
        # 通行：名字裡沒有 needle，父層 `safe/` 解析出來也不受保護，
        # 而 `open(alias, "w")` 會沿著連結寫進 freeze/ 裡的那個檔案。
        # 守衛擋的是「寫到哪裡」，不是「路徑長什麼樣子」。
        info = self._lstat(raw)
        if info is None:
            return False
        if self._is_an_alias(info):
            if self._resolves_into_a_root(raw):
                return True
            # 別名的另一頭本身也可能是一個 hardlink。
            try:
                return self._shares_a_protected_inode(os.stat(raw))
            except (OSError, ValueError):
                return False

        # 第四關：最後一段是一般檔案，但它與某個受保護檔案是**同一個
        # 檔案**。`os.link()` 在 session 內會被擋，漏的是 session 開始前
        # 就已經在磁碟上的 hardlink —— 上一段的 lstat 直接拿來用，
        # 不多打 syscall。
        return self._shares_a_protected_inode(info)

    @staticmethod
    def _as_text(path) -> str | None:
        try:
            raw = os.fspath(path)
        except Exception:  # noqa: BLE001 - 不是路徑就不是我們管的
            return None
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        return raw if isinstance(raw, str) else None

    def _landing(self, path, dir_fd=None) -> str | None:
        """這次寫入實際落在哪裡。

        - 整數：一個已經開好的描述子（`os.truncate(fd)`、`open(fd)`）。
          它在開啟時就判定過；看得到落點就再判一次，看不到就不猜。
        - 相對路徑加 dir_fd：相對於那個描述子指向的目錄，**不是 cwd**。
          看不出指向哪裡就丟 `_Unplaceable`，由呼叫端 fail closed。
        - 其餘：路徑本身。絕對路徑時 OS 也會忽略 dir_fd。
        """
        if isinstance(path, int) and not isinstance(path, bool):
            return path_of_fd(path)
        raw = self._as_text(path)
        if raw is None:
            return None
        if dir_fd is None or os.path.isabs(raw):
            return raw
        base = path_of_fd(dir_fd)
        if base is None:
            raise _Unplaceable(dir_fd)
        return os.path.join(base, raw)

    def _refuse(self, where: str, reason: str) -> None:
        self.violations.append(where)
        raise ProtectedPathWrite(
            f"a test tried to write into a protected path: {where}. {reason}"
            "Tests must write into tmp_path only — pass workspace_root= and "
            "PCMEF_FORMAL_OUT to create_app(). Writing and then deleting is "
            "still a write: anything reading concurrently sees it."
        )

    def check_read(self, path) -> None:
        """讀取一律放行。這個方法存在是為了讓意圖寫在程式碼裡。"""

    def check_write(self, path, dir_fd=None) -> None:
        try:
            where = self._landing(path, dir_fd)
        except _Unplaceable:
            # **看不出落點就不放行。** 放行的意思是「猜它沒落在受保護
            # 的地方」，而這道守衛存在的理由正是不猜。
            self._refuse(
                f"{path} (relative to dir_fd={dir_fd})",
                "The guard cannot see which directory that descriptor refers "
                "to on this platform, and a write it cannot place is refused "
                "rather than waved through. ",
            )
        if where is None or not self._is_protected(where):
            return
        via = f" (dir_fd={dir_fd})" if dir_fd is not None else ""
        self._refuse(f"{where}{via}", "")

    def check_tree_removal(self, path, dir_fd=None) -> None:
        """`shutil.rmtree` 整棵刪除。**在刪第一個檔案之前**就判定。

        POSIX 上 rmtree 走 fd-based 的路：先 `os.open` 頂層目錄，再以
        `dir_fd=` 一個一個 unlink 裸檔名，最後才 rmdir 頂層。只看每一次
        unlink 的字面路徑，守衛看到的是一串裸檔名；等到最後那個 rmdir
        被擋下，底下的檔案早就刪光了 —— 而守衛回報的是「擋下了」。

        刪的是受保護根目錄的**祖先**也算：整棵刪掉 `outputs/` 就包含
        `outputs/perception/e2_final`。
        """
        self.check_write(path, dir_fd=dir_fd)
        where = self._landing(path, dir_fd)
        if where is None:
            return
        try:
            resolved = Path(where).resolve()
        except Exception:  # noqa: BLE001
            return
        if any(resolved in root.parents for root in self._roots):
            self._refuse(
                where, "It is an ancestor of a protected tree, so removing it "
                "removes the protected tree too. ",
            )


def _dir_fd_at(args, index):
    """稽核事件參數裡的 dir_fd。沒給時 CPython 傳 -1，這裡轉成 None。"""
    if index is None or index >= len(args):
        return None
    value = args[index]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


#: 會改動檔案系統的稽核事件 → 每個寫入目標的（參數位置, 對應 dir_fd 的
#: 參數位置）。dir_fd 位置是 None 的目標不相對於任何目錄描述子。
#:
#: `os.replace` 發的是 `os.rename` 事件；`os.symlink` 的第一個參數是
#: 連結的內容，只有第二個才相對於 dir_fd。
WRITE_EVENTS: dict[str, tuple[tuple[int, int | None], ...]] = {
    "os.mkdir": ((0, 2),),
    "os.remove": ((0, 1),),
    "os.rmdir": ((0, 1),),
    "os.truncate": ((0, None),),
    "os.rename": ((0, 2), (1, 3)),
    "os.replace": ((0, 2), (1, 3)),
    "os.link": ((0, 2), (1, 3)),
    "os.symlink": ((0, None), (1, 2)),
}


def _guard_dir_fd_opens(tripwire: WriteTripwire) -> None:
    """讓 `os.open(name, flags, dir_fd=fd)` 的寫入也經過守衛。

    **`open` 稽核事件的參數只有 `(path, mode, flags)`，沒有 dir_fd。**
    於是 `os.open("lock.json", O_WRONLY, dir_fd=<freeze 裡某個目錄>)`
    在稽核事件裡長得跟「在 cwd 開一個叫 lock.json 的檔」一模一樣，
    守衛照 cwd 判完放行，而寫入落在 freeze/ 裡（已在 Linux 實測）。
    `open(name, "w", opener=partial(os.open, dir_fd=fd))` 是同一條路。

    唯一讀得到那個參數的地方是 os.open 本身，所以這裡只包它、只在
    **有 dir_fd 而且是寫入**時多做一次判定；其餘一律原樣轉交，其餘的
    寫入也仍然由稽核事件負責，不因此改成 monkeypatch。

    - 包裝只裝一次，之後的 tripwire 登記到同一份清單（`_guard_module()`
      會另外載入這個檔案，不得一層一層疊上去）。
    - 包裝加進 `os.supports_dir_fd`：有些程式先問「os.open 支不支援
      dir_fd」再決定走哪條路，包起來之後答案不得變。
    - 在這之前就把 `os.open` 存成別名的程式碼（`from os import open`）
      看不到包裝；那一段由 session 前後的指紋比對兜底。
    - Windows 沒有 dir_fd（`os.supports_dir_fd` 是空集合），不裝。
    """
    current = os.open
    if current not in os.supports_dir_fd:
        return
    registry = getattr(current, "_pcmef_tripwires", None)
    if registry is None:
        original = current
        registry = []

        def open(path, flags, mode=0o777, *, dir_fd=None):  # noqa: A001
            if dir_fd is not None and isinstance(flags, int) \
                    and flags & WRITE_FLAGS:
                for guard in tuple(registry):
                    guard.check_write(path, dir_fd=dir_fd)
            return original(path, flags, mode, dir_fd=dir_fd)

        functools.update_wrapper(open, original)
        open._pcmef_tripwires = registry
        os.supports_dir_fd.add(open)
        os.open = open
        posix = sys.modules.get("posix")
        if posix is not None and getattr(posix, "open", None) is original:
            posix.open = open
    if tripwire not in registry:
        registry.append(tripwire)


#: 唯一的正式 localhost UI port。測試不得另外開別的。
OFFICIAL_UI_PORT = 8790


class RealListenerOpened(AssertionError):
    """測試開了一個真的 TCP listener。**當場擋下。**"""


def _psutil():
    try:
        import psutil
    except Exception:  # noqa: BLE001 - 沒有 psutil 就退回「盤點不了」
        return None
    return psutil


def listening_ports() -> set[int]:
    """這台機器上正在 LISTEN 的 port。**只供盤點與報告，不當判準。**

    全機盤點回答的是「機器上有什麼」，不是「測試留下了什麼」：它會把
    瀏覽器、Docker、8790 上的正式 UI 開的 port 一起算進來。判定洩漏
    用的是 `owned_listeners()`。
    """
    psutil = _psutil()
    if psutil is None:
        return set()
    found: set[int] = set()
    try:
        for connection in psutil.net_connections(kind="inet"):
            if connection.status == psutil.CONN_LISTEN and connection.laddr:
                found.add(connection.laddr.port)
    except Exception:  # noqa: BLE001 - 權限不足時不要拖垮測試
        return set()
    return found


def _family():
    """這個 pytest 行程與它還活著的子孫。沒有 psutil 就回 None。"""
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        me = psutil.Process()
        return [me, *me.children(recursive=True)]
    except Exception:  # noqa: BLE001
        return None


def owned_listeners() -> set[tuple[int, int]] | None:
    """這個 pytest 行程與它的子孫正在 LISTEN 的 (pid, port)。

    **只看自己的。** 開的人要負責收，所以判準是擁有權：別的程式開的
    port 與測試無關，不得算到測試頭上。查自己與自己子行程的連線不需要
    管理員權限。

    沒有 psutil 就回 None —— 「盤點不了」與「盤點過、沒有」不是同一件事。
    """
    psutil = _psutil()
    family = _family()
    if psutil is None or family is None:
        return None
    found: set[tuple[int, int]] = set()
    for process in family:
        try:
            listing = getattr(process, "net_connections", None) \
                or process.connections
            for connection in listing(kind="inet"):
                if connection.status == psutil.CONN_LISTEN and connection.laddr:
                    found.add((process.pid, connection.laddr.port))
        except Exception:  # noqa: BLE001 - 子行程剛好結束之類
            continue
    return found


def _still_open(sock) -> bool:
    try:
        return sock.fileno() != -1
    except Exception:  # noqa: BLE001
        return False


class _DeclaredListeners:
    """一次 `allowing()` 宣告的範圍。在裡面綁定的 socket 都記在這裡。"""

    def __init__(self, port: int) -> None:
        self.port = port
        #: 在這個範圍內綁定的 socket 物件本身。**判定收尾看的是它們，
        #: 不是 port 號碼** —— port 0 的號碼要綁完才知道。
        self.sockets: list = []
        #: 進入時 pytest 行程樹已經在 LISTEN 的 (pid, port)。
        self.baseline = owned_listeners()

    def left_open(self) -> list[str]:
        found: list[str] = []
        ours: set[int] = set()
        for sock in self.sockets:
            if not _still_open(sock):
                continue
            try:
                address = sock.getsockname()
                ours.add(address[1])
            except (OSError, IndexError, TypeError):
                address = "?"
            found.append(f"socket bound to {address!r} in this process")
        # 子行程開的、或 dup/detach 之後換了一個物件還開著的 —— 只有行程
        # 層級的盤點看得到。
        if self.baseline is not None:
            now = owned_listeners() or set()
            for pid, port in sorted(now - self.baseline):
                if pid == os.getpid() and port in ours:
                    continue
                found.append(f"pid {pid} still listening on port {port}")
        return found

    def close_leftovers(self) -> None:
        """收掉這個範圍在本行程裡留下的 socket。**只收自己綁的。**"""
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass


class ListenerTripwire:
    """測試不得在本機開出真的 TCP listener。

    先前這條規則是用正規式掃描原始碼來守的，而掃描原始碼守不住任何
    東西：`getattr(socket, "bind")`、包一層 helper、從 library 裡繞
    出去 —— 三種寫法都掃不到，而三種都會真的佔住一個 port。掃描能
    回答的只有「有沒有人把這幾個字面值寫出來」。

    這裡改成在**綁定當下**攔截。Flask 的 `test_client` 完全不碰
    socket（它直接走 WSGI），因此一個字都不用改就照樣通過 —— 這正是
    測試本來就該用的方式。

    **涵蓋範圍只到本行程。** 子行程自己開的 listener 稽核事件看不到，
    那一段由 `owned_listeners()` 以行程樹兜底 —— 看的是 pytest 自己
    生出來的行程，不是整台機器。
    """

    def __init__(self) -> None:
        #: 目前有效的宣告，最內層在最後。空的 —— 預設一個都不准。
        self._scopes: list[_DeclaredListeners] = []
        #: 全部綁定嘗試，包含被放行的。盤點用。
        self.attempts: list[str] = []
        #: 被擋下來的那些。即使測試自己吞掉例外也留著。
        self.violations: list[str] = []

    @contextlib.contextmanager
    def allowing(self, port: int):
        """明確開放一個 port，**且離開時必須已經收掉**。

        需要真的 listener 的測試走這裡，於是「哪一個測試開了什麼」
        寫在測試自己的程式碼裡，而不是靠事後 netstat 去猜。

        離開時檢查的是**擁有權**，不是 port 號碼：
          1. 在範圍內綁定的每一個 socket 物件都必須已經 close。
             `allowing(0)` 因此也驗得到 —— 先前它只能拿 0 去對 netstat，
             而 OS 實際挑的號碼只有綁完才知道。
          2. pytest 行程樹在範圍內新開、到離開時還在 LISTEN 的，同樣
             算沒收（子行程開的 server、dup 出來的 fd）。需要 psutil。

        沒收掉的 socket 由這裡代為 close 再失敗，免得它佔著 port 活過
        這個測試；子行程只回報，不代為終止。
        """
        if port == OFFICIAL_UI_PORT:
            raise RealListenerOpened(
                f"{OFFICIAL_UI_PORT} is the only official localhost UI port; "
                "no test may declare it, let alone bind it."
            )
        scope = _DeclaredListeners(port)
        self._scopes.append(scope)
        try:
            yield scope
        except BaseException:
            # 本體已經在失敗了：收掉留下的 socket，但不拿收尾的問題
            # 蓋掉原本的例外。
            self._scopes.remove(scope)
            scope.close_leftovers()
            raise
        self._scopes.remove(scope)
        leftovers = scope.left_open()
        scope.close_leftovers()
        if leftovers:
            raise RealListenerOpened(
                "a declared temporary listener is still open after the code "
                f"that opened it finished: {leftovers}. A temporary listener "
                "must be closed by the code that opened it."
            )

    def check_bind(self, sock, address) -> None:
        family = getattr(sock, "family", None)
        kind = getattr(sock, "type", None)
        if family not in (socket.AF_INET, socket.AF_INET6):
            return
        # UDP 不是 listener。記下來，不擋。
        if kind != socket.SOCK_STREAM:
            return
        port = None
        if isinstance(address, tuple) and len(address) >= 2:
            if isinstance(address[1], int):
                port = address[1]
        where = f"{address!r}"
        self.attempts.append(where)
        if port is not None and port != OFFICIAL_UI_PORT:
            for scope in reversed(self._scopes):
                if scope.port == port:
                    scope.sockets.append(sock)
                    return
        self.violations.append(where)
        raise RealListenerOpened(
            f"a test tried to bind a real TCP socket to {where}. "
            f"{OFFICIAL_UI_PORT} is the only official localhost UI port, and "
            "tests must not occupy it or any other. Use Flask's test_client "
            "(it speaks WSGI directly and binds nothing). If a real listener "
            "is genuinely required, take it through LISTENERS.allowing(port) "
            "so that it is declared and torn down."
        )


#: 會生出子行程的稽核事件。
SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.posix_spawn", "os.spawn", "os.system",
    "os.startfile", "os.fork", "os.forkpty",
})


class ChildProcessLedger:
    """pytest 自己生出來、在測試結束之後還活著的子行程。

    listener 只是症狀之一；背後的問題是「一個沒有人在管的行程」——
    它可能還在算、還在寫檔、還佔著 port。稽核事件只到本行程，所以
    這裡看的是 pytest 的行程樹：**開的人要負責收。**

    代價控制：只有在這段期間真的 spawn 過（稽核事件記一筆）才去列
    行程樹，一輪幾千個測試裡只有十幾個會付這個錢。

    **只回報，不代為終止。** 那個行程可能正在寫檔，何時、如何終止
    它是操作者的判斷。也不需要管理員權限：列的是自己的子孫。

    已知涵蓋範圍：子行程在測試結束前就把孫行程分離出去、自己先結束
    的話，孫行程不再掛在 pytest 底下，行程樹看不到它。
    """

    def __init__(self, grace: float = 5.0) -> None:
        #: 自上次結算以來的 spawn 次數。由稽核事件累加。
        self.spawns = 0
        #: session 開始時就已經在的子孫 —— 不是這一輪生的。
        self.baseline: set[int] = set()
        #: 已經回報過的洩漏，pid → 說明。每個只回報一次。
        self.leaked: dict[int, str] = {}
        #: 給剛送出終止、還在收尾的行程一點時間，而不是當場判洩漏。
        self.grace = grace

    @property
    def available(self) -> bool:
        return _psutil() is not None

    def note_spawn(self) -> None:
        self.spawns += 1

    def _alive_descendants(self):
        family = _family()
        if not family:
            return []
        psutil = _psutil()
        alive = []
        for process in family[1:]:
            try:
                if process.status() == psutil.STATUS_ZOMBIE:
                    continue   # 已經死了，只是還沒被 wait
            except Exception:  # noqa: BLE001 - 查不到就是已經不在了
                continue
            alive.append(process)
        return alive

    def start(self) -> None:
        self.baseline = {p.pid for p in self._alive_descendants()}
        self.spawns = 0

    def settle(self, where: str, *, always: bool = False) -> list[str]:
        """結算：這段期間生出來、到現在還活著的子行程。"""
        if not always and not self.spawns:
            return []
        self.spawns = 0
        candidates = [
            p for p in self._alive_descendants()
            if p.pid not in self.baseline and p.pid not in self.leaked
        ]
        if not candidates:
            return []
        psutil = _psutil()
        try:
            _gone, survivors = psutil.wait_procs(candidates, timeout=self.grace)
        except Exception:  # noqa: BLE001
            survivors = candidates
        found = []
        for process in survivors:
            try:
                command = " ".join(process.cmdline())[:160]
            except Exception:  # noqa: BLE001
                command = "?"
            note = f"pid {process.pid} ({command}) — left running by {where}"
            self.leaked[process.pid] = note
            found.append(note)
        return found


def _install_tripwire(tripwire: WriteTripwire, listeners=None,
                      processes=None) -> None:
    """把守衛掛到 CPython 的稽核事件上。

    用 audit hook 而不是 monkeypatch `open`：真正的寫入會經過
    `os.mkdir`、`os.rename`、`os.replace`、`shutil`，而它們各自
    有不同的底層呼叫。稽核事件是這些路徑的共同瓶頸，因此一處掛上
    就涵蓋全部 —— monkeypatch 只會蓋到記得列出來的那幾個。

    唯一的例外是 `os.open` 的 dir_fd：那個參數不在稽核事件裡，只能在
    os.open 本身讀到（`_guard_dir_fd_opens`）。

    **裝上去就拿不下來**（CPython 的設計），所以它只在 pytest
    session 內生效，而 session 本來就是我們要管的範圍。
    """
    write_modes = ("w", "a", "x", "+")

    def hook(event: str, args) -> None:
        try:
            if event == "open":
                path, mode, flags = args[0], args[1], args[2]
                if path is None:
                    return
                writing = False
                if isinstance(mode, str):
                    writing = any(m in mode for m in write_modes)
                elif isinstance(flags, int):
                    writing = bool(flags & WRITE_FLAGS)
                if writing:
                    tripwire.check_write(path)
            elif event == "shutil.rmtree":
                # 3.11 起多一個 dir_fd 參數；3.10 只有 path。
                tripwire.check_tree_removal(args[0], _dir_fd_at(args, 1))
            elif event in WRITE_EVENTS:
                for where, fd_at in WRITE_EVENTS[event]:
                    if where < len(args):
                        tripwire.check_write(
                            args[where], dir_fd=_dir_fd_at(args, fd_at)
                        )
            elif event == "socket.bind" and listeners is not None:
                listeners.check_bind(args[0], args[1])
            elif event in SPAWN_EVENTS and processes is not None:
                processes.note_spawn()
        except (ProtectedPathWrite, RealListenerOpened):
            raise
        except Exception:  # noqa: BLE001 - 守衛自己壞掉不得拖垮測試
            return

    sys.addaudithook(hook)
    _guard_dir_fd_opens(tripwire)


def fingerprint(paths) -> str:
    """一組路徑的內容指紋。不存在的路徑也算進去（消失同樣是變動）。

    比對內容而不是 mtime：內容相同而 mtime 變了不算污染，內容變了
    而 mtime 沒變才是最難發現的那一種。
    """
    digest = hashlib.sha256()
    for root in sorted(Path(p) for p in paths):
        if root.is_dir():
            members = sorted(p for p in root.rglob("*") if p.is_file())
        elif root.is_file():
            members = [root]
        else:
            digest.update(f"{root.as_posix()}\0<missing>\0".encode("utf-8"))
            continue
        for member in members:
            digest.update(member.as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(hashlib.sha256(member.read_bytes()).digest())
            except OSError as error:
                digest.update(f"<unreadable:{error.errno}>".encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


#: session 內共用的一份守衛。測試可以讀它的 violations。
TRIPWIRE = WriteTripwire(PROTECTED_PATHS)

#: session 內共用的 listener 守衛。需要真 listener 的測試用
#: `LISTENERS.allowing(port)` 明確宣告。
LISTENERS = ListenerTripwire()

#: session 內共用的子行程帳本。
PROCESSES = ChildProcessLedger()


@pytest.fixture(scope="session", autouse=True)
def no_test_leaves_a_listener_behind():
    """測試不得開出真的 TCP listener，也不得留下任何一個。

    兩層：稽核事件在綁定當下擋住本行程的嘗試；session 前後比對
    **pytest 行程樹自己**的 listener，抓子行程留下來的。

    先前第二層比的是整台機器的 listening ports，於是任何無關的程式在
    這段期間開了一個 port，都會被算成測試的洩漏；而它同時又看不出
    port 0 的測試開了哪一個。擁有權同時解決這兩件事。
    """
    before = owned_listeners()
    yield
    if LISTENERS.violations:
        raise AssertionError(
            "the test session opened real TCP listeners: "
            f"{sorted(set(LISTENERS.violations))}"
        )
    if before is None:
        # 沒有 psutil：只剩綁定當下的攔截與 allowing() 的 socket 檢查。
        return
    leaked = sorted((owned_listeners() or set()) - before)
    if leaked:
        raise AssertionError(
            "the test session left listeners behind in its own process tree "
            f"(pid, port): {leaked}. Tests must not pollute the local port "
            f"state; {OFFICIAL_UI_PORT} is the only official localhost UI port."
        )


@pytest.fixture(scope="session", autouse=True)
def no_session_leaves_a_process_behind():
    """整輪結束時，pytest 生出來的子行程必須都已經結束。

    逐測試的檢查只在「那個測試 spawn 過」時才跑；這裡兜底 session
    範圍的夾具與其他沒被歸到某個測試的 spawn。
    """
    PROCESSES.start()
    yield
    leaked = PROCESSES.settle("the test session", always=True)
    if leaked:
        raise AssertionError(
            "the test session left child processes running: "
            f"{leaked}. They were not terminated by this guard."
        )


@pytest.fixture(autouse=True)
def no_test_leaves_a_process_behind(request):
    """測試生出來的子行程，測試結束時必須已經結束。

    失敗落在**那一個測試**的 teardown 上：洩漏要能指名是誰留下的，
    而 session 結尾的一句「有東西還在跑」幫不上任何忙。
    """
    yield
    leaked = PROCESSES.settle(request.node.nodeid)
    if leaked:
        raise AssertionError(
            f"this test left child processes running: {leaked}. "
            "A test must wait for (or terminate) every process it starts; "
            "this guard reports them and does not terminate them."
        )


@pytest.fixture(scope="session", autouse=True)
def repo_is_not_written_to():
    """測試**一次都不得寫入**受保護路徑，即使事後刪掉。

    這一層與下面的指紋比對互補：指紋回答「跑完之後有沒有變」，
    這裡回答「過程中有沒有碰過」。寫了又刪的那一種只有這裡抓得到。
    """
    _install_tripwire(TRIPWIRE, LISTENERS, PROCESSES)
    yield
    if TRIPWIRE.violations:
        raise AssertionError(
            "the test session wrote into protected paths: "
            f"{sorted(set(TRIPWIRE.violations))}"
        )


@pytest.fixture(scope="session", autouse=True)
def repo_is_not_polluted():
    """整輪測試不得改到 repo 的科研輸出與專案 metadata。

    autouse + session scope：這不是某個測試要記得啟用的東西。會忘記
    啟用的守衛，正好會在寫出污染的那個測試上被忘記。

    留著這一層而不是被上面的 tripwire 取代：稽核事件涵蓋不到的路徑
    （例如經由 C 擴充模組直接寫檔）仍然會被指紋抓到。兩者的失敗模式
    不同，因此兩者都要。
    """
    # 逐路徑記錄，才能在失敗時指出**是哪一個**被動到。整組一個雜湊
    # 只能說「有東西變了」，而那句話幫不上任何忙。
    before = {path: fingerprint([path]) for path in WATCHED_PATHS}
    yield
    changed = [
        path.as_posix()
        for path in WATCHED_PATHS
        if fingerprint([path]) != before[path]
    ]
    if changed:
        raise AssertionError(
            "the test session modified the repository's scientific tree or "
            "project metadata. Tests must write into tmp_path only — pass "
            "workspace_root= and PCMEF_FORMAL_OUT to create_app(). "
            f"changed: {changed}"
        )
