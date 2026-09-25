# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 在 session 開始時自動載入，涵蓋 tests/ 底下每一個
#         測試。不被任何 production 模組匯入。
# 檔案路徑: tests/conftest.py
# 產生時間: 2026-09-07 11:05 +08:00
# 版本: v0.5.0
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
#   6. ListenerTripwire 攔下測試開出來的真實 TCP listener
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
# ------------------------------------------------------------

from __future__ import annotations

import contextlib
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


class ProtectedPathWrite(AssertionError):
    """測試試圖寫入受保護的位置。**當場擋下，不等到最後才比對。**"""


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

    def _final_component_is_an_alias(self, raw: str) -> bool:
        """這條路徑的**最後一段**本身是不是一個別名。

        解析所在目錄擋得住 `別名目錄/檔案`，擋不住 `目錄/別名檔案`：
        父層是一個再普通不過的位置，名字上也看不出什麼，而
        `open()` 會沿著最後那一段的連結寫進受保護的檔案裡。
        `safe/alias.json -> freeze/.../lock.json` 就是這條路。

        **不得用 `os.path.islink()` 單獨判定。** 它對 Windows 的
        junction 回傳 False，而 `resolve()` 照樣跟著走 —— 於是
        junction 變成一條「守衛看不見、作業系統卻認得」的繞道。
        這裡改看 reparse point 屬性，symlink 與 junction 一起涵蓋。

        用 `lstat()` 而不是 `resolve()`：前者一次 syscall 且**不跟著
        連結走**，後者要把每一段都走過。絕大多數寫入的目標根本還不
        存在，`lstat()` 當場 ENOENT 返回，代價就停在這裡。
        """
        try:
            info = os.lstat(raw)
        except (OSError, ValueError):
            # 還不存在的檔案不可能是別名 —— 建立它的是這次寫入本身。
            return False
        if stat.S_ISLNK(info.st_mode):
            return True
        # Windows：junction 與 directory symlink 都是 reparse point，
        # 但只有後者會讓 S_ISLNK 成立。
        if getattr(info, "st_reparse_tag", 0):
            return True
        attributes = getattr(info, "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    def _is_protected(self, path) -> bool:
        try:
            raw = os.fspath(path)
        except Exception:  # noqa: BLE001 - 不是路徑就不是我們管的
            return False
        if not isinstance(raw, str):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return False

        # 第一關：名字上就帶著受保護根目錄的名字。相對與絕對都算，
        # 不打任何 syscall。命中就直接做完整解析。
        lowered = raw.lower()
        if any(needle in lowered for needle in self._needles):
            try:
                return self._under_a_root(Path(raw).resolve())
            except Exception:  # noqa: BLE001
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
        if not self._final_component_is_an_alias(raw):
            return False
        try:
            return self._under_a_root(Path(raw).resolve())
        except Exception:  # noqa: BLE001
            return False

    def check_read(self, path) -> None:
        """讀取一律放行。這個方法存在是為了讓意圖寫在程式碼裡。"""

    def check_write(self, path) -> None:
        if not self._is_protected(path):
            return
        message = (
            f"a test tried to write into a protected path: {path}. "
            "Tests must write into tmp_path only — pass workspace_root= and "
            "PCMEF_FORMAL_OUT to create_app(). Writing and then deleting is "
            "still a write: anything reading concurrently sees it."
        )
        self.violations.append(str(path))
        raise ProtectedPathWrite(message)


#: 唯一的正式 localhost UI port。測試不得另外開別的。
OFFICIAL_UI_PORT = 8790


class RealListenerOpened(AssertionError):
    """測試開了一個真的 TCP listener。**當場擋下。**"""


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
    那一段由 session 前後的 listener 盤點負責（`listening_ports()`）。
    兩層的失敗模式不同，因此兩層都要。
    """

    def __init__(self) -> None:
        #: 明確開放的 port。空的 —— 預設一個都不准。
        self._allowed: set[int] = set()
        #: 全部綁定嘗試，包含被放行的。盤點用。
        self.attempts: list[str] = []
        #: 被擋下來的那些。即使測試自己吞掉例外也留著。
        self.violations: list[str] = []

    @contextlib.contextmanager
    def allowing(self, port: int):
        """明確開放一個 port，**且離開時必須已經收掉**。

        需要真的 listener 的測試走這裡，於是「哪一個測試開了什麼」
        寫在測試自己的程式碼裡，而不是靠事後 netstat 去猜。

        **`allowing(0)` 驗不到收尾。** port 0 是「讓 OS 挑一個」，
        而實際挑到的號碼只有綁完才知道，因此離開時無從確認它收掉了
        沒有 —— 這一種只由 session 結束時的整體盤點兜底。要讓收尾
        當場被檢查，就得用實際的 port 號碼進來。
        """
        self._allowed.add(port)
        try:
            yield
        finally:
            self._allowed.discard(port)
            still_open = [p for p in listening_ports() if p == port]
            if still_open:
                raise RealListenerOpened(
                    f"port {port} is still listening after the test that "
                    "opened it finished; a temporary listener must be closed "
                    "by the test that opened it"
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
        if port is not None and port in self._allowed:
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


def listening_ports() -> set[int]:
    """這台機器上正在 LISTEN 的 port。

    用來做 session 前後的盤點 —— 稽核事件只看得到本行程，子行程留下
    的 listener 只有這裡看得到。
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001 - 沒有 psutil 就退回「盤點不了」
        return set()
    found: set[int] = set()
    try:
        for connection in psutil.net_connections(kind="inet"):
            if connection.status == psutil.CONN_LISTEN and connection.laddr:
                found.add(connection.laddr.port)
    except Exception:  # noqa: BLE001 - 權限不足時不要拖垮測試
        return set()
    return found


def _install_tripwire(tripwire: WriteTripwire, listeners=None) -> None:
    """把守衛掛到 CPython 的稽核事件上。

    用 audit hook 而不是 monkeypatch `open`：真正的寫入會經過
    `os.mkdir`、`os.rename`、`os.replace`、`shutil`，而它們各自
    有不同的底層呼叫。稽核事件是這些路徑的共同瓶頸，因此一處掛上
    就涵蓋全部 —— monkeypatch 只會蓋到記得列出來的那幾個。

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
                    writing = bool(
                        flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)
                    )
                if writing:
                    tripwire.check_write(path)
            elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.truncate"):
                tripwire.check_write(args[0])
            elif event in ("os.rename", "os.replace", "os.link", "os.symlink"):
                for target in args[:2]:
                    tripwire.check_write(target)
            elif event == "socket.bind" and listeners is not None:
                listeners.check_bind(args[0], args[1])
        except (ProtectedPathWrite, RealListenerOpened):
            raise
        except Exception:  # noqa: BLE001 - 守衛自己壞掉不得拖垮測試
            return

    sys.addaudithook(hook)


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


@pytest.fixture(scope="session", autouse=True)
def no_test_leaves_a_listener_behind():
    """測試不得開出真的 TCP listener，也不得留下任何一個。

    兩層：稽核事件在綁定當下擋住本行程的嘗試，session 前後的盤點
    抓子行程留下來的。掃描原始碼的那一版兩種都抓不到。
    """
    before = listening_ports()
    yield
    if LISTENERS.violations:
        raise AssertionError(
            "the test session opened real TCP listeners: "
            f"{sorted(set(LISTENERS.violations))}"
        )
    leaked = sorted(listening_ports() - before)
    if leaked:
        raise AssertionError(
            "the test session left new listeners behind on this machine: "
            f"{leaked}. Tests must not pollute the local port state; "
            f"{OFFICIAL_UI_PORT} is the only official localhost UI port."
        )


@pytest.fixture(scope="session", autouse=True)
def repo_is_not_written_to():
    """測試**一次都不得寫入**受保護路徑，即使事後刪掉。

    這一層與下面的指紋比對互補：指紋回答「跑完之後有沒有變」，
    這裡回答「過程中有沒有碰過」。寫了又刪的那一種只有這裡抓得到。
    """
    _install_tripwire(TRIPWIRE, LISTENERS)
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
