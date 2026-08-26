# PC-MEF Research System source maintenance contract
# 上下游: 由 docker-compose.yml 建置；安裝 pyproject.toml 宣告的相依與 LLVM 執行期，
#         容器啟動後預設跑 pcmef.cli；data/ outputs/ freeze/ registry/ secrets/
#         全部以 volume 掛入，映像本身不含任何研究資料或憑證。
# 檔案路徑: Dockerfile
# 產生時間: 2026-08-27 11:00 +08:00
# 版本: v0.1.0
# 功能說明: 把這套系統連同 mitsuba/drjit 所需的 LLVM 執行期打包成可重現的環境，
#           讓交接的人不必自己在 Windows 上處理 LLVM-C.dll 那一串坑。
# 模組定位: 環境重現層。它「不是」部署映像 —— 這套系統是 local-first 研究平台，
#           容器的用途是讓環境可重現，不是對外提供服務。
# 主要責任:
#   1. 以 python:3.10-slim 為基底，與本機 py -3.10 對齊
#   2. 安裝 llvm 執行期，讓 drjit 的 llvm_ad_rgb variant 可用
#   3. 以 pip install -e ".[dev,simulation,admin,agents]" 安裝全部相依
#   4. 建立非 root 使用者，避免 volume 內的產物變成 root 所有
#   5. HEALTHCHECK 以 pcmef version 確認容器內的套件真的能匯入
# 維護提醒:
#   - 不得把 data/ 或 secrets/ COPY 進映像。前研究資料 159.5 MB 且不進版控，
#     憑證更不得進；兩者一律走 volume。
#   - 不得改用 python:3.10-alpine。numpy/scipy/mitsuba 在 musl 上沒有 wheel，
#     會退回原始碼編譯，建置時間從數分鐘變成數十分鐘。
#   - 不得省略 llvm 套件。drjit 執行期動態載入 LLVM，缺了會在
#     jitc_llvm_init() 失敗且 llvm_ad_rgb variant 無法使用（見 STATUS.md 接手指南）。
#   - 不得在容器內跑 formal run 就認為等同本機結果；formal 的環境雜湊
#     會把版本記進 manifest，換環境等於換 run。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - docker compose build
#   - docker compose run --rm pcmef pytest -q
#   - docker compose run --rm pcmef python -m pcmef.cli sim smoke
# ------------------------------------------------------------

FROM python:3.10-slim-bookworm

# drjit 在執行期動態載入 LLVM；缺了它 llvm_ad_rgb variant 無法使用。
# build-essential 供少數沒有 wheel 的套件回退編譯用。
RUN apt-get update && apt-get install -y --no-install-recommends \
        llvm-14-runtime \
        libllvm14 \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

# drjit 找的是 libLLVM.so，Debian 只給帶版號且在 multiarch 目錄下的檔名。
# 用 find 而非寫死路徑：版號與 multiarch 目錄都會隨基底映像變，
# 寫死的話升一次基底就會變成一個「指向不存在檔案」的 symlink，
# 而症狀是 jit_init_thread_state() 說找不到函式庫 —— 看起來像沒安裝。
RUN set -eux; \
    lib="$(find /usr/lib -name 'libLLVM-*.so' -o -name 'libLLVM-*.so.1' | sort | tail -1)"; \
    test -n "$lib"; \
    ln -sf "$lib" /usr/lib/libLLVM.so; \
    echo "linked $lib -> /usr/lib/libLLVM.so"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DRJIT_LIBLLVM_PATH=/usr/lib/libLLVM.so

WORKDIR /app

# 先只複製套件描述檔，讓相依層在原始碼變動時仍可重用快取。
COPY pyproject.toml README.md ./
COPY pcmef/__init__.py ./pcmef/__init__.py
RUN pip install --upgrade pip && \
    pip install -e ".[dev,simulation,admin,agents]" && \
    # 把模擬器釘到與本機完全相同的版本。pyproject 的 mitsuba>=3.5 / drjit>=0.4
    # 對開發夠用，但 transient 的數值輸出會餵進 E1 —— 容器與本機跑出不同版本，
    # 兩邊的結果就不可比，而 NOTE-012（DLL detach 崩潰）與 NOTE-013（時間窗截斷）
    # 都是在 3.8.0 / 1.3.1 上驗證的。要換版必須重驗那兩則。
    pip install --force-reinstall --no-deps \
        "mitsuba==3.8.0" "drjit==1.3.1" "mitransient==1.3.0"

COPY pcmef/ ./pcmef/
COPY tests/ ./tests/
COPY configs/ ./configs/
COPY schemas/ ./schemas/
COPY docs/ ./docs/
COPY STATUS.md ./

# 產物目錄由 volume 覆蓋，但先建好並交給非 root 使用者，
# 否則首次掛載時容器會以 root 身分寫入，之後在主機上改不動。
RUN mkdir -p data outputs freeze registry secrets provenance artifacts && \
    useradd --create-home --uid 1000 pcmef && \
    chown -R pcmef:pcmef /app
USER pcmef

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -m pcmef.cli version || exit 1

ENTRYPOINT ["python", "-m", "pcmef.cli"]
CMD ["version"]
