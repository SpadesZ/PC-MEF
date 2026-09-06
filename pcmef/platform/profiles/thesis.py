# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.profiles.registry 在遷移 Thesis Project 時呼叫；
#         產出的 ResearchDesign 由 console 的 Research Design 頁渲染。
#         **唯讀常數，不讀 freeze、不算科學值。**
# 檔案路徑: pcmef/platform/profiles/thesis.py
# 產生時間: 2026-09-06 20:25 +08:00
# 版本: v0.1.0
# 功能說明: 把 PC-MEF_實驗計畫_v1.2.1 逐節映射成 Thesis Profile 的
#           結構化 Research Design。
# 模組定位: 平台化 Phase 3。這裡是計畫書與平台之間唯一的映射點 ——
#           畫面上看到的研究設計全部出自本檔，因此每一欄都標了
#           計畫書章節；欄位若另有可執行真相（freeze/ 的 lock），
#           一併標出，讓「計畫書怎麼寫」與「執行時讀哪一份」分得開。
# 主要責任:
#   1. THESIS_DESIGN 提供完整的 v1.2.1 研究設計
#   2. 每一欄標註 source（計畫書章節）
#   3. 決策相關欄位標註 authoritative（對應 lock）
# 維護提醒:
#   - **不得在此重新解釋或補充實驗計畫。** 計畫書沒寫的研究主張，
#     平台不得代為發明；SAI v0.6.0 是平台化設計背景，不是研究來源。
#   - 不得讓任何 executor 讀本檔取門檻或 class order。那些的可執行
#     真相在 freeze/ 的 lock；本檔是敘述層，兩者混用會產生第二份真相。
#   - 更新計畫書版本時必須同步 SOURCE_DOCUMENT 與各欄 source，
#     不得只改內容而留著舊章節號 —— 那會讓稽核指向不存在的段落。
#   - v0.1.0 新增：首版，對應 PC-MEF_實驗計畫_v1.2.1。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_research_design.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pcmef.platform.profiles.design import ResearchDesign, build_section

__all__ = ["SOURCE_DOCUMENT", "THESIS_TITLE", "THESIS_DESIGN"]

SOURCE_DOCUMENT = "PC-MEF_實驗計畫_v1.2.1"

THESIS_TITLE = (
    "結合物理校準模擬與大型語言模型輔助多模態融合之管內液態狀態辨識"
)

THESIS_DESIGN = ResearchDesign(
    source_document=SOURCE_DOCUMENT,
    title=THESIS_TITLE,
    sections=(
        build_section(
            "objective", "研究目的 Objective",
            "建立合成資料與真實 ToF 量測之間的物理可信度，並評估 PC-MEF "
            "在感測品質下降與跨模態分歧條件下的辨識穩健性。",
            [
                ("英文標題",
                 "Intra-Pipe Liquid-State Identification Integrating "
                 "Physics-Calibrated Simulation and Large Language "
                 "Model-Assisted Multimodal Fusion", "封面"),
                ("研究對象", "管內液態狀態辨識（四類）", "§1"),
                ("前研究", "蔡連興〈管內液態辨識系統之設計與實作〉，"
                 "提供幾何條件、四類 ToF 分布與偏移量測作為真實資料錨點", "§2.1"),
            ],
        ),
        build_section(
            "research_questions", "研究問題 Research Questions",
            "兩個可檢驗問題，各自對應一個實驗。",
            [
                ("RQ1", "物理校準後的模擬 ToF 是否更接近真實量測？"
                 "（E1 物理校準驗證）", "§1"),
                ("RQ1 主要指標", "Normalized Wasserstein distance；"
                 "距離偏移趨勢；95% CI", "§1"),
                ("RQ2", "Selective PC-MEF 是否提升困難感測條件下的辨識穩健性？"
                 "（E2 多模態穩健性）", "§1"),
                ("RQ2 主要指標", "Worst-condition Macro-F1；"
                 "overall / per-condition Macro-F1；paired 95% CI",
                 "§1", "statistics_config"),
            ],
        ),
        build_section(
            "modalities", "感測模態 Modalities / Sensors",
            "同一合成場景同步產生 RGB 與 ToF 證據，形成可控制的 "
            "paired multimodal benchmark。",
            [
                ("ToF 感測器", "VL53L0X（time-of-flight ranging sensor）", "§2.1"),
                ("ToF 通道", "Distance、Signal Rate、Ambient Rate、Sigma-like"
                 "（四通道，順序固定）", "§3.2", "formal_config"),
                ("ToF recording 形狀", "500 measurement points × 4 channels",
                 "§1.1", "formal_config"),
                ("ToF 取樣率", "約 12.195 Hz", "§2.1"),
                ("影像模態", "一般相機 RGB 影像", "§2.1"),
                ("感測幾何", "sensor-bottle 約 5 cm；瓶徑約 5.7 cm；"
                 "壁厚約 0.2 cm", "§2.1"),
                ("距離錨點", "Empty 約 100 mm；Water 約 114 mm；"
                 "Bubbly 約 105–106 mm；Misty 約 55–85 mm", "§2.1"),
                ("偏移錨點", "baseline 約 98.61 mm；±0.1 cm 測試約 "
                 "91.05 / 88.76 mm", "§2.1"),
            ],
        ),
        build_section(
            "class_space", "標籤空間 Class Space",
            "四類標籤固定，各比較方法共用相同 class order。",
            [
                ("四類", "Empty、Water-filled、Bubbly、Misty", "§1.1",
                 "formal_config",
                 "class order 固定；各方法共用相同 class order、"
                 "E2 scenario IDs 與 condition 定義"),
                ("類別數", 4, "§1.1", "formal_config"),
            ],
        ),
        build_section(
            "conditions", "測試條件 Benchmark Conditions",
            "四種 benchmark condition，劣化強度由路由驗證集事先選定並固定。",
            [
                ("Clean", "量測名目條件下的基準表現；nominal paired RGB-ToF",
                 "§4.2", "conflict_operational"),
                ("Vision-degraded", "觀察影像品質下降時的路由與融合行為；"
                 "defocus / visual degradation", "§4.2", "conflict_operational"),
                ("ToF-degraded", "觀察 ToF 品質下降時的路由與融合行為；"
                 "signal attenuation / ambient interference",
                 "§4.2", "conflict_operational"),
                ("Conflict-Stress", "增加兩模態產生分歧的案例比例；"
                 "modality-specific perturbation combination",
                 "§4.2", "conflict_operational"),
                ("劣化強度", "由路由驗證集事先選定並固定（frozen severity）",
                 "§4.2", "conflict_operational"),
            ],
        ),
        build_section(
            "data_roles", "資料角色 Data Roles",
            "研究資料依物理場景群組分離；感知模型訓練、路由驗證、"
            "開發期測試與 Final E2 各自使用獨立的 scene-family 範圍。",
            [
                ("真實資料", "前研究 ToF：四類各 140 筆 recording；"
                 "影像：四類各 300 張，共 1,200 張", "§2.1"),
                ("模擬器", "Mitsuba 3 + mitransient；transient light transport "
                 "與 volumetric transient path tracing", "§3.1"),
                ("模擬時間尺度", "optical time 描述單次 acquisition 內的光傳播；"
                 "measurement time 描述連續 acquisition 形成的量測時間軸", "§3.1"),
                ("合成資料角色", "同一物理場景同步生成 RGB 與 ToF 證據，"
                 "形成 paired multimodal benchmark", "§2.2"),
                ("資料分離原則", "感知模型訓練、路由驗證、開發期測試與 "
                 "Final E2 各自使用獨立的 scene-family 範圍",
                 "§5.1", "validation_pool",
                 "family-disjoint；Final E2 使用保留場景組形成一次性 benchmark"),
            ],
        ),
        build_section(
            "experiments", "實驗設計 Experiments",
            "兩項核心實驗形成驗證鏈：E1 物理保真度，E2 穩健性。",
            [
                ("E1 角色", "物理校準驗證：比較初始物理模擬與校準後模擬"
                 "到保留真實 ToF 的距離", "§4.1"),
                ("E1 實驗單位", "one real 500×4 recording / "
                 "one matched synthetic recording", "§4.1"),
                ("E1 比較", "Wasserstein(initial, real) vs. "
                 "Wasserstein(calibrated, real)", "§4.1"),
                ("E2 角色", "多模態穩健性驗證：四種條件下比較單模態、"
                 "固定融合、可靠度路由與 Full PC-MEF", "§4.2"),
                ("E2 場景設計", "family-disjoint physical scene families；"
                 "每類 24 base scenarios，共 96 base scenarios",
                 "§4.2", "e2_sample_size"),
                ("E2 案例數", "96 base scenarios × 4 conditions = 384 cases",
                 "§4.2", "e2_sample_size"),
                ("E2 realization", "每個 family 具有 3 個 realization",
                 "§1.1", "e2_sample_size"),
                ("比較組別", "G1 Vision-only、G2 ToF-only、G3 Fixed Fusion、"
                 "G4 Reliability Routing、G5 Full PC-MEF", "§4.2"),
                ("Operational Conflict 子群", "兩模態 top-1 類別差異或 D ≥ δ",
                 "§4.2", "conflict_operational"),
            ],
        ),
        build_section(
            "calibration", "物理校準 Calibration",
            "以 calibration real data 定義 feature scale，再比較初始與校準後"
            "模擬到保留真實資料的距離。",
            [
                ("真實資料切分", "70% calibration / 30% 保留真實資料；"
                 "class-stratified；seed 固定", "§3.2", "real_split_policy"),
                ("樣本數", "四類各 140 筆均通過資料檢核時："
                 "calibration 392、保留真實資料 168", "§3.2", "real_split_policy"),
                ("Distance 映射", "transient peak / energy-center 對應飛行時間，"
                 "再加入 geometry offset", "§3.2"),
                ("Signal Rate 映射", "主要回波時間窗積分能量的尺度映射", "§3.2"),
                ("Ambient Rate 映射", "背景光與背景時間窗能量映射", "§3.2"),
                ("Sigma-like 映射", "peak width、SNR、multipath spread "
                 "組合後尺度", "§3.2", None,
                 "前研究以 Sigma 作為 ToF 第四項量測特徵；"
                 "合成資料以 Sigma-like 表示量測散離程度的代理量"),
                ("主要指標", "Normalized Wasserstein distance，依 "
                 "feature × class 計算；以 calibration-only pooled IQR 正規化",
                 "§3.2", "metric_config"),
                ("E1 判定", "校準後相較 initial simulation 呈現整體接近度改善，"
                 "並維持各 feature/class 與距離擾動趨勢的一致性",
                 "§3.2", "e1_scientific_rule"),
            ],
        ),
        build_section(
            "perception", "感知模型 Perception",
            "兩個輕量感知模型，皆輸出四類完整分布。",
            [
                ("Vision 模型", "三層卷積 small CNN", "§3.3"),
                ("ToF 模型", "沿 measurement-time 軸運算的 1D CNN", "§3.3"),
                ("輸出", "Empty / Water-filled / Bubbly / Misty 四類完整分布",
                 "§3.3", "formal_config"),
                ("機率校準", "以乾淨路由驗證集進行 temperature scaling："
                 "p_m(y|x) = softmax(z_m / T_m)", "§3.3 式(1)",
                 "perception_condition_policy"),
            ],
        ),
        build_section(
            "reliability", "感測可靠度 Reliability",
            "同時使用「感測品質」與「模型分布差異」兩類資訊。",
            [
                ("Q_V 影像品質", "影像 Laplacian 高頻能量，以平均亮度尺度"
                 "正規化後取 log", "§3.3", "reliability_final"),
                ("Q_T ToF 品質",
                 "log(1 + max(median Signal, 0) / max(median Ambient, ε))",
                 "§3.3", "reliability_final"),
                ("D 跨模態分歧", "Vision 與 ToF 四類機率分布的 Total Variation "
                 "distance：D(p_V,p_T) = ½ Σ|p_V(c) − p_T(c)|",
                 "§3.3 式(2)", "reliability_final",
                 "D 範圍 0–1；越接近 0 表示兩模態分布越接近"),
                ("可靠度分數",
                 "q_m = σ((Q_m − a_m) / b_m) · (1 − D/2)",
                 "§3.4 式(3)", "reliability_final"),
                ("a_m", "乾淨路由驗證集品質分布的第 5 百分位",
                 "§3.4", "reliability_final"),
                ("b_m", "同一分布的四分位距（IQR）", "§3.4", "reliability_final"),
            ],
        ),
        build_section(
            "routing", "選擇性路由 Selective Routing",
            "依可靠度與跨模態分歧選擇傳統決策路徑或進入證據仲裁。",
            [
                ("路由門檻", "固定為 0.5", "§3.4", "gate"),
                ("Trust Vision", "q_v ≥ 0.5 且 q_t < 0.5 → p_v", "§3.4", "gate"),
                ("Trust ToF", "q_t ≥ 0.5 且 q_v < 0.5 → p_t", "§3.4", "gate"),
                ("Fixed Fusion", "q_v ≥ 0.5、q_t ≥ 0.5，且 D ≤ δ → p_fixed",
                 "§3.4", "gate"),
                ("Agent Arbitration", "雙側品質偏弱，或雙側可靠且 D > δ → s_A",
                 "§3.4", "gate"),
                ("固定融合式", "p_fixed = 0.5 p_V + 0.5 p_T", "§3.4 式(4)", "gate"),
                ("最終決策",
                 "F(x) = p_{r(x)}(x) 若 r(x) ∈ R_trad；"
                 "F(x) = s_A(x) 若 r(x) = A；R_trad = {V, T, F}",
                 "§3.5 式(6)", "gate"),
            ],
        ),
        build_section(
            "agents", "Multi-Agent 證據整合 Agents",
            "四個角色依序完成「觀察摘要 → 兩個模態專家 → 證據仲裁」，"
            "僅處理 reliability routing 選出的困難案例。",
            [
                ("觀察角色 Observation",
                 "輸入 RGB、ToF per-channel summary、原始感測品質 Q；"
                 "輸出中性觀察摘要 Observation Brief", "§3.5", "agent_schema"),
                ("物理角色 Physics",
                 "輸入 Observation Brief、ToF 分布 p_t、q_t、Q_t 與 ToF 時序摘要；"
                 "輸出 ToF / physics proposal", "§3.5", "agent_schema"),
                ("視覺語意角色 Visual-Semantic",
                 "輸入 Observation Brief、RGB、Vision 分布 p_v、q_v、Q_v；"
                 "輸出 RGB / visual-semantic proposal", "§3.5", "agent_schema"),
                ("仲裁角色 Arbitration",
                 "輸入 Observation Brief、兩份 proposal、p_v/p_t、q_v/q_t、D "
                 "與模型輸出猶豫度；輸出四類 raw class support a_A",
                 "§3.5", "agent_schema"),
                ("ToF 摘要內容",
                 "四通道各自的 mean、std、median、p10、p90 與 "
                 "temporal-difference std，加上 signal-to-ambient ratio "
                 "與 valid sample ratio；每通道保留原本的物理單位與語意",
                 "§3.5", "agent_schema"),
                ("支持度正規化",
                 "s_A(c) = (a_A(c) + ε_s) / Σ_j (a_A(j) + ε_s)，"
                 "僅當 Σ a_A(j) > 0 時進入正規化",
                 "§3.5 式(5)", "agent_schema",
                 "總和為 0 代表仲裁輸出缺乏有效證據，系統重新取得仲裁結果"),
                ("證據隔離", "各角色只收到自己該收到的證據；"
                 "inference 端不得看到 ground truth",
                 "§3.5", "inference_firewall"),
            ],
        ),
        build_section(
            "primary_endpoint", "主要判準 Primary Endpoint",
            "以最弱條件下的 Macro-F1 作為主要穩健性指標。",
            [
                ("Primary endpoint", "Worst-condition Macro-F1"
                 "（四條件中最低的 Macro-F1）", "§5", "statistics_config"),
                ("次要指標", "整體 Accuracy / Macro-F1；分條件 Macro-F1",
                 "§5", "statistics_config"),
                ("Escalation Rate", "進入 Multi-Agent arbitration 的案例比例",
                 "§5", "statistics_config"),
                ("配對效果", "Full PC-MEF 相對 G1–G4 的 paired performance "
                 "difference：Paired Effect Δ + 95% CI", "§5", "statistics_config"),
                ("錯誤結構", "Confusion Matrix / 各類召回率", "§5"),
            ],
        ),
        build_section(
            "statistics", "統計 Statistics",
            "以物理場景群組為叢集單位的配對 bootstrap。",
            [
                ("重抽樣單位", "physical scene family（叢集）",
                 "§5", "statistics_config"),
                ("分層", "依類別分層", "§5", "statistics_config"),
                ("叢集內容", "每次抽到一個場景群組時，同時帶入該群組的 "
                 "3 個 realization × 4 種測試條件", "§5", "statistics_config"),
                ("配對比較", "各方法在同一次重抽樣使用相同場景群組，"
                 "形成配對比較", "§5", "statistics_config"),
                ("重抽樣次數", "固定 10,000 次", "§5", "statistics_config"),
                ("信賴區間", "95% CI", "§5", "statistics_config"),
                ("E1 bootstrap", "10,000 replicates；95% CI；matched evaluation",
                 "§3.2", "statistics_config"),
            ],
        ),
        build_section(
            "claim_boundary", "結論範圍 Claim Boundary",
            "本研究可主張與不可主張的範圍。",
            [
                ("E1 結論範圍", "VL53L0X-like ToF sensor-surrogate fidelity",
                 "§4.1", "claim_boundary"),
                ("RGB 的角色", "RGB 在 E2 中作為與 ToF 同場景同步生成的視覺證據；"
                 "E1 的結論範圍不涵蓋 RGB", "§2.2", "claim_boundary"),
                ("Final E2 性質", "使用保留場景組形成一次性 benchmark",
                 "§5.1", "e2_sample_size",
                 "families 36–43 為保留分割，未生成前不得讀取或預覽"),
            ],
        ),
    ),
)
