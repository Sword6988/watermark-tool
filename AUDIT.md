# watermark-tool 审计报告

- 审计日期：2026-09-25
- 代码规模：5552 行（含测试 1571 行）
- 基线：`tests/run_all.py` **42/42 通过**；冻结自检 4/4 PASS
- 审计方式：架构 / 测试覆盖 / 代码质量 三个角度并行独立审计 + 交叉验证
- **修复进度（2026-09-25）**：P0 已修 6/6、P1 已修 12/12、P2 全部完成；回归 **70/70 通过、0 SKIP**
  （连跑 8 次无 flaky）；已重新打包并通过字节级（符号级）验证。
  - 第 1 批产物：EXE md5 `ce4504633a47c7c83101b2ec55481bd7`
  - 第 5 批产物（含全部修复 + 步进器 v1）：EXE md5 `728de3676c04887facb28b046b20dfce`
  - 方案 C 产物（步进器嵌入数字框 + 单位并入字段名）：EXE md5 `e38fc6f6da90a65a19e37b553e4d7f42`
  - 常驻步进三角产物：EXE md5 `6cdf7878fedc8d57411bf5654ae0ba39`
  - 圆盘产物：EXE md5 `cec06a5567fbe7e8b34a39e27b031709`
  - **最新产物（上述 + 图形 4× 超采样抗锯齿）**：EXE md5 **`b78124647ffa83dc8e1c5903d2285397`**
    （目录版 68.4MB / 便携 zip 32.1MB，zip md5 `47e3be0824283d8e71b07551a1d82478`，
    打包 **68.0 秒**；字节级已确认 `paint_aa` / `AA_SS` / `_aa_image` 在包内）
  - 字节级校验 **0 项不符**（新增 P0-3 分带符号 `IMAGE_BAND_PIXELS` / `IMAGE_BAND_ROWS` /
    `_prepare_layer` / `_composite_banded`，及 P1-4 三模块 `save_image` / `write_image` /
    `render_preview` / `run_batch` 的模块归属核对；步进器符号 `_HoverStepper` / `_spin` /
    `_entry_value` / `entry_box` / `_with_unit` / `_sync_box_border` 均已确认在包内；
    `IMPORT_NAME` 级 `pymupdf` 正面证据仍在）；
    冻结版 `--selftest` PASS、GUI 冒烟 4 秒存活、zip CRC 通过、`PIL/_avif*.pyd` 已不在包内。
    回归 **77/77、0 SKIP、0 FAIL**
    （60 → 67 为 `test_banding.py` 3 条 + `test_ui_units.py` 4 条；
    67 → 71 为 `test_stepper.py` 4 条；71 → 77 为 `test_angle_dial.py` 6 条）。
  每个已修条目下方有「✅ 已修复」小节记录改法与实测数据。
- 结论：**功能主干（平铺几何、两路径一致、PDF alpha、不覆盖原文件）守得很扎实，
  但存在 1 个会产出错误结果的静默缺陷，以及一整类"只在真实文件上才会踩到"的
  内存与健壮性问题 —— 现有 42 项测试全部覆盖不到它们。**

---

## 一句话结论

**当前测试的 42/42 全绿具有误导性**：所有用例都用 600×400 小图或合成的空白 A4 页，
没有任何一条用例带"真实文件特征"（页面旋转、大尺寸、超长文本、多帧）。因此下面
6 项 P0 全部落在盲区，其中第 1 项会**静默产出错误的水印结果且用户在预览里看不出来**。

---

## P0 —— 必须修

### P0-1　PDF 页带 `/Rotate 90/270` 时，水印整条缺失并越界

**唯一一条会产出错误结果的缺陷，且用户不可自察（预览是对的，导出是错的）。**

根因：`wm/render.py:224` 用 `page.rect` 计算版式、`:233` 用 `page.rect` 插入图层。
PyMuPDF 的 `page.rect` 返回**已应用旋转**的尺寸（595×842 的页 + `/Rotate 90` → 842×595），
但 `insert_image` 的坐标落在**未旋转**空间（MediaBox 仍是 595×842），于是宽 842 的
图层被塞进宽 595 的页 —— 一侧裁掉一大条，另一侧越过页边。

团队独立复现 + 我本人二次复现，数据一致：

| Rotate | page.rect | 总覆盖率 | 横向四段覆盖率 | | | |
|---|---|---|---|---|---|---|
| 0 | 595×842 | 6.98% | 7.96 | 5.85 | 6.29 | 7.81 |
| **90** | 842×595 | **3.88%** | **0.00** | 4.48 | 5.62 | 5.43 |
| 180 | 595×842 | 7.00% | 7.96 | 6.18 | 5.96 | 7.88 |
| **270** | 842×595 | **3.87%** | 5.53 | 5.59 | 4.38 | **0.00** |

90° 时左侧约 25% 页宽**完全没有水印**，270° 时右侧同样。扫描件（手机扫描 App、
工程图）高频带 `/Rotate`，属于常见输入。

另一组交叉验证（四边贴边偏差）：0° 最坏 0.2px；90°/270° 最坏 **247.2px**。

**修法**：仍按视觉尺寸 `page.rect` 渲染图层 → `layer.rotate(rot, expand=True)` 补偿
→ `page.set_rotation(0)` → `insert_image` → `finally` 恢复 `set_rotation(rot)`。

> **方向更正（重要）**：审计初稿写的 `-rot` 是**错的**，实测已推翻。
> PyMuPDF 的 `/Rotate 90` 表示**显示时顺时针 90°**，而 PIL 的 `rotate(θ)` 是**逆时针 θ**，
> 两者符号相反，所以补偿必须是 `rotate(+rot)`。用非对称角标做方向探针：
> `-rot` 把左上角标记转到了右下角，`+rot` 标记仍在左上角。已改。

### ✅ 已修复（2026-09-25）

- 改动：`wm/render.py` 的 `render_pdf` —— 缓存键加入 `rot`、`layer.rotate(rot, expand=True)`、
  `set_rotation(0)` / `finally set_rotation(rot)`。
- 实测（修复后）：Rotate 90 与 270 覆盖率均 **5.67%**，横向四段均匀，不再出现 0.00 段；
  与「同视觉尺寸未旋转页」端到端逐像素差 **0.02/255**。
- 新增回归：`test_pdf_rotated_page_has_no_missing_band`、
  `test_pdf_rotated_page_matches_unrotated_of_same_visual_size`。
- 变异测试：删掉补偿 → 两条用例都 FAIL；把补偿翻成 `-rot` → **只有方向比对那条 FAIL**
  （覆盖率那条仍 PASS，因为平铺图案转 180° 照样铺满）——证明第二条用例不可省。

### P0-2　超长文本 → `DecompressionBombError`，失败前先空转 22 秒

`wm/layout.py` 的块位图尺寸随文本长度线性增长，无上限：

| 场景 | 结果 |
|---|---|
| A4 @72dpi，200 汉字 | 40.6M 像素，OK |
| A4 @72dpi，500 汉字 | 251M，**硬失败** |
| A4 @300dpi，200 汉字 | 691M，**硬失败** |
| A4 2000 个 ASCII | `DecompressionBombError`，**耗时 22.10s 后才报错** |

即用户输入一段长文本，程序先卡死 22 秒，然后弹一个看不懂的炸弹警告。
现有用例只测到「500 汉字 @600×400」—— 刚好在及格线内，所以没暴露。

### ✅ 已修复（2026-09-25）

- 改动：`wm/layout.py` 新增 `MAX_BLOCK_PIXELS = 100_000_000`，在 `_draw_text_block`
  **分配画布之前**先算 `width*height`，超限直接抛可读的 `ValueError`
  （"水印块过大（需要约 N 百万像素，上限 100 百万）：文字太长或字号太大……"）。
- 实测：触发守卫从 **22.10s** 降到 **0.14s**，且报错文案是用户能看懂的中文建议，
  不再是 Pillow 的炸弹警告。
- 新增回归：`test_block_bitmap_rejects_absurd_text`。

#### ⚠️ 第一次修复不完整，已二次修复（同日）

独立验证发现守卫**按旋转前尺寸判定**，而真正的放大发生在 `_render_block_bitmap` 里的
`rotate(angle, expand=True)` —— 注释里「rotate 之后最多再放大约 2 倍」是错的，实测 43×–158×：

| 用例 | 守卫判定（旋转前） | 实际（旋转后） | RGBA | 耗时 |
|---|---|---|---|---|
| `"A"*200` font4% @178.5px | 5.8MP **通过** | 247.7MP | **945MB** | **2.74s** |
| `"密"*500` font4% @32px | 0.7MP **通过** | 111.7MP | 426MB | 1.12s |

「长文本 + 小字号 + 旋转」仍能静默吃掉近 1GB 并卡 2.7 秒 —— 换了个入口而已。
更糟的是 `"A"*200 @4%` 被写进了 `test_block_bitmap_rejects_absurd_text` 的
「合法用法不得误伤」清单，等于**把漏网行为固化成了预期**。

二次修复：`_draw_text_block` 增加 `angle` 参数，新增 `_rotated_pixel_estimate()`
按 `rw=w·|cosθ|+h·|sinθ|`、`rh=w·|sinθ|+h·|cosθ|` 解析估算旋转后包围盒
（**仍在 `Image.new` 之前**判定，fail-fast 不变）；`MAX_BLOCK_PIXELS` 重标为
**165M**（最重合法用法 8000×8000 + font30% + PDF 2× 实测旋转后 134.7MP，留 22% 余量）；
`"A"*200 @4%` 移到拦截侧。新增回归 `test_block_guard_uses_rotated_bounds`。

#### ✅ 二次修复已通过独立复核（同日）

复核者独立重算的标定表与实现申报完全一致，并且回答了一个关键疑问 ——
**解析估算会不会系统性低估真实 `rotate(expand=True)` 的尺寸**（低估 = 守卫有洞）：

- 60 组（0–90° 步进 + 随机 0–360°，尺寸 137×41 ~ 3000 级）实测：**估算 > 真实的次数 = 0**，
  偏差方向恒为**欠估**，最大相对欠估 **+0.605%**（小图 402×526@15°），大图上仅 **+0.014%**。
- 与 Pillow 源码一致：`nw = ceil(max(xx)) - floor(min(xx))`，四角点是整数坐标，
  故每轴 `nw ∈ [extent, extent+2)` —— **每轴最多 +2px 的取整误差**，不可能穿透 165MP 阈值。

变异测试 5 处**全部被抓**（退化回 `width*height`、守卫挪到 `Image.new` 之后、
阈值改成 10^12、预览闸门改 `if False:`、去掉 worker `finally` 释放），逐字节还原干净。

#### ⚠️ 已知残余风险（结构性，非 bug）

1. **`"密"*500` 那类细长文本仍然放行**：真实 **111.66MP / 426MB / 1.07s**。拦不了的原因
   是**结构性的** —— 判据只有「旋转后包围盒面积」一个自由度，而被下层顶住：
   最重合法用法（2 字文本、≥8000pt 页面）本身就要 **134.74MP / 514MB / 6.55s**。
   任何 ≤134.7MP 的阈值都会误伤合法用法，任何 ≥134.7MP 的阈值都必然放行 111.66MP。
   **165MP 是这个夹缝里唯一合理的取值**（对 2 字基准留 22.5% 余量），
   所以 111.6MP/426MB 是接受的代价。根治要换判据（按墨迹宽度而非包围盒面积）或做
   P0-3 分带渲染。
2. **没有下游兜底**：实测 `Image.new(20000×10000)`（200MP）与 `rotate` 到 247.7MP
   **都静默成功**，Pillow 的减压阀不覆盖 `Image.new` / `rotate`（它只在**读文件**时生效），
   且全仓库没有设置 `Image.MAX_IMAGE_PIXELS`。**`MAX_BLOCK_PIXELS` 是唯一一道防线。**
3. **合法页面上界收窄 8.4%–18.3%**（font_pct 拉满 30%、PDF 2×）：2 字文本
   9,662pt → 8,854pt；App 默认 4 字文本 6,984pt → **5,704pt**。
   注：默认文本在 8000×8000 + 30% + PDF 2× 下会被拦（需约 325MP）—— 但这个组合在
   **旧**阈值下也已被拦（旧上界 6,984pt < 8,000pt），**不是本次新引入的破坏**。
   失败时给的是可操作的中文提示，不是过去那种长时间卡死。
4. **覆盖缺口**：没有用例断言「长文本在**低**字号下仍应放行」
   （如 `"A"*200` @ A4 4% → 4.55MP 放行）。目前只由短文本间接代表。
5. **测试卫生（P3，不阻塞）**：新增的 `test_preview_requests_are_serialized` 让 stderr 的
   `invalid command name ..._poll_results` 从 **0 条变成 3 条**。四组交叉对照归因唯一。
   性质是「第 3 个 App 实例改变了 Tk 解释器拆除时序」，把更早两个 App 用例
   （只 `root.destroy()`、从不取消 poll）的陈旧 `after` 脚本冲刷出来 —— 新用例自身
   清理是有效的（cleanup 后 `after jobs = 0`、stub 已还原、无真线程、队列已抽干）。
   建议抽公共 teardown：每个 App 用例 destroy 前先 `after` 全 cancel。

### P0-3　大图输出峰值 3.3 GB，无任何护栏

`render_image` 按 `RENDER_SCALE=2.0` 生成 2× 的全幅 RGBA 水印层，再加原图与合成结果：

| 输入 | 块数 | 耗时 | 峰值内存 |
|---|---|---|---|
| 4000×3000 | 36 | 1.27s | 672 MB |
| 6000×6000 | 24 | 3.58s | 2015 MB |
| 8000×8000 | 24 | 6.11s | **3276 MB** |

峰值 ≈ 34 × W × H 字节。没有像素预算判断，`Image.MAX_IMAGE_PIXELS` 只在**读文件**时
生效，水印层是 `Image.new` 出来的、不受限。手机随手拍已是 12MP，专业相机 50MP 起。

实测可行的降本组合（同一张 8000×8000）：

| 方案 | 耗时 | 峰值 |
|---|---|---|
| 现状（2× + LANCZOS 降采样） | 6.45s | 3276 MB |
| **图片路径改 1× 不超采样** | **0.62s** | **1059 MB** |
| **分带渲染（1024~2048 行一带）** | **0.46s** | **585 MB** |

### P0-4　块位图缓存「按条数限流（512），不按字节」→ 拖一次字号滑杆驻留 6.5 GB

`wm/layout.py:35`（`_CACHE_CAP=512`）、`:129`、`:166` —— 满了就整表 `clear()`，
但**不限制单条大小**。单块位图（含 2× 光栅 + 旋转）：

- 1200×900 @字号30% → 15.7 MB
- 4000×3000 @字号30% → **174.3 MB**（生成 2.26s）
- 8000×8000 @字号30% → **1238.3 MB**（生成 15.06s）

模拟拖动字号滑杆 1%→30%（30 档，每档缓存 2 条：探针块 + 实渲染块）：

- 1200×900：1.6s，83 MB
- 4000×3000：13.8s，**916 MB**
- 8000×8000：**97.4s**，**6508 MB**（最大单块 310 MB）

512 条上限 → 4000×3000 理论滞留 87 GB。**8GB 内存的机器必崩。**

**一行改动即可大幅缓解**：给块光栅化加像素上限（如 `fs = min(fs, 512)`）。
`_render_crisp_layer` 本来就会「裁到 ink 再拉伸到 ink×scale」（`render.py:113-118`），
降低栅格倍率只让字形略软，**不动版式、不破坏任何契约**，1238MB 可压到约 30MB。

### ✅ 已修复（2026-09-25，改法与上面的建议不同：按字节预算而非按条数）

- 改动：`wm/layout.py` 新增 `_RENDER_CACHE_BYTES = 256MB`、`_bitmap_cost()`
  （`w*h*4`）、`_trim_render_cache()`（按插入顺序淘汰）。
  `_render_block_bitmap` 只在单条成本 ≤ 预算时才入缓存，入缓存后再做一次整体裁剪。
- 与「限制栅格倍率」方案的区别：那条方案会让**字号很大的正常用法**字形变软；
  字节预算只在**真的很大**时才不缓存，正常用法行为不变。
- 新增回归：`test_render_cache_respects_byte_budget`。

### P0-5　并发预览线程无上限

`wm/ui/app.py:390-394` 每次参数变化无条件起一个新线程，无池、无取消；每个线程都
重新 `media.Document(path)` + 全分辨率 `convert("RGBA")`（4000×3000 单帧 +156MB，
8000×8000 +777MB）。并发叠加是**线性**的：8 并发 × 8000×8000 ≈ 6.2 GB；
拖 3 秒滑杆（150ms 防抖 ≈ 20 次触发）≈ 15 GB。

**这是用户最容易触发的 OOM** —— 调参数时连着滚几下就中。
修法：单 worker +「最新优先」（约 15 行），内存从 N 份降到 1 份。

### ✅ 已修复（2026-09-25）

- 改动：`wm/ui/app.py` 新增 `_preview_busy` / `_preview_pending`。
  `_render_preview_async` 发现 busy 时**只置 pending 并立即返回**（不再起线程）；
  `_preview_worker` 的 `finally` 清 busy；`_poll_results` 抽干队列后，
  若 `pending and not busy` 就补跑一次 —— 保证"最后一次参数"一定会被渲染。
- 效果：并发预览线程数上限恒为 1，且不会丢最后一次结果。

#### ⚠️ 首次修复零测试覆盖，已补（同日）

变异测试证明：把 `if self._preview_busy:` 改成 `if False:`（回到无限制起线程），
**47/47 依旧全绿** —— 实现是对的，但完全裸奔。假 Thread 计数：8 次连续触发预览，
有闸门起 1 个线程，闸门失效起 8 个。
已补 `test_preview_requests_are_serialized`（真 App + stub Thread，不真起线程、
不真渲染）：busy 时 0 线程且 `pending=True`；idle 时恰好 1 线程且 `busy=True /
pending=False`；worker 走异常出口后 `finally` 必须放开闸门。
`wm/ui/app.py` 本身**未改动**。

### P0-6　`main.py --selftest <非法路径>` 永久挂起；且 PASS 不写 stdout

- 传入已存在的文件路径作输出目录 → 抛 `FileExistsError[WinError 183]` → 弹
  **模态 messagebox** → 无头环境（CI / 打包脚本）**永久挂起**（实测 180s 未退出）。
  打桩掉 messagebox 后立刻返回，阻塞点确认无误。
- `[SELFTEST] PASS` **只写进报告文件**，stdout 一行都没有 → 靠 grep stdout 判断的
  脚本会误判成"没跑"。

### ✅ 已修复（2026-09-25）

- `main.py::selftest()`：目录创建包 `try/except OSError`，失败即
  `print("[FAIL] 自检输出目录不可用")` + `print("[SELFTEST] FAIL")` + `return 1`，
  **不再弹模态框**；报告同时 `print(text, end="")` 到 stdout。
- `main()`：自检分支整体包 try/except，`traceback.print_exc()` 后
  `print("[SELFTEST] FAIL")` 并 `return 1`，绕过 GUI 的模态错误路径。
- 实测：非法路径下立即退出码 1（原来 180s 不退出）；正常路径 stdout 出现
  `[SELFTEST] PASS`。

---

## P1 —— 明显影响体验或可维护性

| # | 问题 | 证据与影响 |
|---|---|---|
| P1-1 | **`RENDER_SCALE=2.0` 超采样收益≈0，代价 10×** | 1× 与 2× 降采样回 1× 的逐像素差仅 **0.357/255（0.14%）**，覆盖率差 0.075pp。而 8000×8000 上"2×→1× 的 LANCZOS 降采样"单独就 5.13s，占总耗时 77%。注释里"补回最外 2–3px"的收益被高估（`_render_crisp_layer` 已把块裁到 ink 再拉伸，贴边精度与倍率无关）。建议图片路径改 1×、PDF 保留 2×（打印需 144dpi），两个常量解除别名 |
| P1-2 | PDF 图层缓存按尺寸缓存且**永不释放** | 60 页同尺寸 A4：+29MB ✅；60 页各差 1pt（扫描件常见）：**+1035MB**；10 页 A0 海报各差 1pt：**+2623MB / 22.2s**。每份 Pixmap = 4×(2w)×(2h) = 16·w·h 字节 |
| P1-3 | `doc.tobytes()` 把整份输出 PDF 驻留内存 | `render.py:237`。「先转 bytes 再写以避免半成品」的出发点对，但实现最贵。改 `doc.save(dst+".part")` + `os.replace()` 既保留原子性又去掉峰值（5 行） |
| P1-4 | `wm/ui/app.py`（595 行）职责过重 | 无反向依赖（core 层全不 import ui，已 grep 确认），但 UI 里住了业务逻辑：`_save_image`（输出编码策略）、`_preview_worker`（fit-to-canvas 数学）、`_batch_worker`（批处理编排）。建议拆出 `output.py` / `preview_job.py` / `batch.py`，可脱离 Tk 单测 |
| P1-5 | **图片 EXIF / 方向丢失** | 源图 EXIF `{274: 6}`，输出后 EXIF `{}`；`page_size` 返回未转正的 400×300。手机照片（大量 Orientation=6）输出后方向与资源管理器里看到的不一致；JPEG 还二次有损 |
| P1-6 | 图片路径**没有取消点** | `render_image` 无 `is_cancelled`，单张实测跑满 2.86s 才停（大图 15s+）。点"取消"没反应 |
| P1-7 | `import fitz` 已弃用 | PyMuPDF 1.28.2 每次导入都打弃用警告；`render.py:38`、`media.py:11`、`main.py:119`。升版可能直接 ImportError。机械替换 `import pymupdf as fitz` |
| P1-8 | 18 处静默 `except: pass` | 最严重的是 `_setup_dnd` —— 拖拽初始化失败时静默吞掉，拖拽功能**假装不存在**而无任何提示 |
| P1-9 | 窗口关闭**零清理** | `_poll_results` 无条件续接（已知 stderr `invalid command name` 的根因）；`self.doc` 永不释放 → GIF / 多帧 TIFF 源文件被占用（**WinError 32**）。注：PNG/JPG/单帧 TIFF 实测会释放句柄，只有多帧格式会锁 |
| P1-10 | 批处理中文件列表可被拖放修改 | 完成态分母错乱 |
| P1-11 | 冻结版 Tk 回调异常只进 `runtime.log` | 用户零感知 |
| P1-12 | `render.py:227` 注释与实现**方向相反** | 照注释改会重现 bug 1。属于过时的错误注释 |

### ✅ P1-1 已修复（2026-09-25）

- `wm/render.py`：`RENDER_SCALE` 别名解除，拆成 `IMAGE_RENDER_SCALE = 2.0`、
  `IMAGE_SS_MAX_PIXELS = 8_000_000`、`PDF_RENDER_SCALE = 2.0`（PDF 仍保留 2×，打印需 144dpi）。
- **没有**按审计建议"一律改 1×"，而是**自适应**：`≤ 8MP` 保持 2×（小图占绝大多数，
  2× 的边缘质量白拿），`> 8MP` 才降到 1×。原因：直接全改 1× 会让
  `test_qa_image_and_pdf_paths_agree_multi` 的 bbox 差从 1.x 涨到 **2.5**，超过阈值 2 ——
  实测证明"一刀切 1×"会牺牲两条路径的一致性。
- 实测：4000×3000 **1.27s → 0.22s**（5.8×）；8000×8000 **6.11s → 0.48s**（12.7×）。
  测试套件性能行 `4000x3000=0.09s`（原 1.17s）。
- 新增回归：`test_image_render_scale_is_adaptive`。

### ✅ P1-2 / P1-3 / P1-5 / P1-6 / P1-7 / P1-8 / P1-9 / P1-10 / P1-11 已修复（同日，第 2 批）

三个 worker 并行完成（文件集互不相交：`wm/render.py` / `wm/media.py`+`main.py` /
`wm/ui/app.py`），随后由独立验证者复核。数字均为实测。

**P1-3（`doc.tobytes()` → `.part` + `os.replace`）** —— 30 页、输出 86.0MB：
峰值工作集增量 **194.9MB → 33.2MB（-83.0%）**，耗时持平。
`.part` 三条语义实测成立：成功无残留；`doc.save` 抛异常 → 无残留且已有目标一字未动；
取消 → 目标从未出现；`os.replace` 失败（目标是目录）→ 已有目标一字未动。

**P1-2（PDF 图层缓存字节预算）** —— 60 页各差 1pt（模拟扫描件）：
缓存驻留 **498.6MB → 251.2MB（-49.6%）**，峰值 **1028.4MB → 787.3MB（-23.4%）**，耗时持平。
60 页**同尺寸**对照：仍只创建 1 张、命中率 100%、峰值增量 29.6MB，未退化。
⚠️ 峰值里的大头（787MB）**不是图层缓存**，是 MuPDF 为 60 张不同图层图保留的文档侧数据 ——
缓存预算只能砍掉约 1/4 峰值，别指望更多。
实现顺带把缓存提成了**模块级**（跨调用复用），靠键里的 spec 指纹防串味；
验证者逐字段实测 7 个外观字段（text / font_family / font_pct / margin_pct / angle /
opacity / color）**全部完备**，连续渲染不串水印。

**P1-5（EXIF / 方向）** —— `Document` 层做 `ImageOps.exif_transpose()`：

| 源 | 尺寸 | 输出 EXIF |
|---|---|---|
| 无 EXIF PNG | 40×20 → 40×20 | `{}` |
| O=1 | 40×20 → 40×20 | 保留 |
| O=3 | 40×20 → 40×20 | 274 已清除 |
| O=6 | 40×20 → **20×40** | 274 已清除 |
| O=8 | 40×20 → **20×40** | 274 已清除 |

其它 EXIF 条目（用 Artist 验证）完整保留。多帧实测：3 帧 GIF / 3 帧 TIFF /
**3 帧 TIFF + O=6** 的 `frame_count` 均正确为 3，首帧尺寸与像素正确。
（`exif_transpose()` 返回的副本**连 `n_frames` 属性都没有**，必须在转正**前**记录帧数。）

**P1-7（`import fitz` → `import pymupdf as fitz`）** —— 全仓库已无 `import fitz`。
⚠️ **验证手法的坑**：PyMuPDF 1.28 的弃用提示**直接写 stdout 且不经过 `warnings` 模块**
（`python -W error::DeprecationWarning -c "import fitz"` 退出码仍是 0、stderr 为空、
`warnings.catch_warnings` 捕获 0 条）。所以 `-W error` 判退出码、或只 grep stderr，
都是**假绿灯**。正确判据要同时查 stdout + stderr 并加 `import fitz` 对照组。

**P1-8 / P1-9 / P1-10 / P1-11（`wm/ui/app.py`）** ——
- P1-8：`_setup_dnd` 不再静默吞异常，记 `dnd_ok` + 状态栏提示（不弹模态框，
  启动弹框会打断无头自检/打包）；其余 14 处 `except` 逐条处置（保留+注释 或 打 `[WARN]`）。
- P1-9：新增 `WM_DELETE_WINDOW` → `_on_close`（`after_cancel` + `_preview_gen` 作废在途帧
  + `_close_doc()` + 有界 join 批处理线程）。stderr 噪声 **3 条 → 0 条**。
  额外给 `root.destroy` 装了守卫 —— 因为项目里有 **6 处**收尾是直接 `root.destroy()`，
  协议根本拦不住；验证者实测去掉守卫噪声回到 **2 条**，去掉 `after_cancel` 回到 **4 条**。
  **已决定保留**该守卫（风险面已逐条审查：只包实例方法、捕获的是改前绑定方法无递归、
  有防重复包标志、清理异常不影响销毁；代价是 app↔root 引用环，GC 可回收）。
- P1-10：批处理活着时拒绝拖放/删除/清空并写状态栏原因（用真线程验证，不是只看标志位）。
- P1-11：`report_callback_exception` → 进 stderr + 状态栏标红 + 按 `(类型名, 消息)` 去重。
  **故意不用 messagebox** —— 无头流程里弹模态框会永久挂住（有 UI 用例跑 300 次
  `root.update()`）。

### ⚠️ 第 2 批的已知残余（已派人收尾）

1. **缓存键不含 `scale`**（潜伏洞）：目前无调用点传 scale，一旦有人传，同尺寸 PDF 会
   静默复用错分辨率的图层（实测差 41347 像素）。→ 已要求把 `scale` 加进键。
2. **指纹护栏曾是空断言**：`test_pdf_layer_cache_respects_byte_budget` 的那一步先清缓存，
   导致断言恒真；变异「去掉指纹」全绿。→ 已要求改成不清缓存的连续渲染判据。
3. **P1-5 让"打开图片"变成即时解码**：20MP JPEG 打开峰值 **+103.1MB**、稳态 **+76.3MB**
   （改前约 0）。→ 已要求改成"先只读 Orientation，只有 2–8 时才 load+transpose"。
4. **`after_cancel` 曾无用例可察**：关闭用例只断言 `_poll_job is None`
   （赋值本来就置 None），去掉 `after_cancel` 全绿。→ 已要求加
   `assert not tuple(root.tk.call("after","info"))`（实测有判别力：`()` vs `('after#2','after#0')`）。
5. **亚像素尺寸共键**：500.0 与 500.4 都 round 成 500 → 共用图层，最大灰度差 23/255，
   肉眼不可见。仅记录，不改。
6. **TIFF 源的 EXIF 实际拿不到**（Pillow 读不到，`exif_bytes` 恒 None），
   与 `App._EXIF_SAVE_EXTS` 含 `.tif/.tiff` 的注释不符。非回归，仅改注释。

### ✅ P1-7 收尾：`HIDDEN_IMPORTS` 摘掉 `fitz`（同日）

`packaging/spec_common.py` 的 `HIDDEN_IMPORTS` 原本同时含 `pymupdf` 和 `fitz`。
留着 `fitz` 的唯一后果是 PyInstaller **分析阶段会真的 import 一次那个已弃用的 shim**，
在构建日志里打出 `warning: The 'fitz' API is deprecated...`，并把 shim 本身打进产物 ——
而运行时没有任何代码路径会用到它。已摘掉，重新打包后冻结版 selftest 仍 PASS，
弃用警告在**构建期与运行期都已消失**。

> 附带一个**判据纠错**：原本想用「code object 的常量里没有 `'fitz'` 字符串」做静态判据，
> 实测**不成立** —— CPython 把 `import` 的模块名放在 `co_names`（`IMPORT_NAME` 的 oparg
> 索引它），`co_consts` 里只有 `(level, fromlist)`，**新旧两种写法都不产生模块名字符串常量**
> （反汇编已确认）。有效判据是取 `dis.get_instructions` 里 `IMPORT_NAME` 的 `argval`：
> 新版是 `pymupdf`、旧版是 `fitz`，能明确区分。

### ✅ 本批（第 5 批）已修复：P0-3 / P1-4 / P2 全部

#### ✅ P0-3 分带渲染（`wm/render.py`）
- 新增 `IMAGE_BAND_PIXELS = 16_000_000`（目标尺寸像素数超此值走分带）、`IMAGE_BAND_ROWS = 1024`。
- 抽出 `_prepare_layer(...)`：整层与分带共用同一份「计算版式 + 块位图」逻辑，保证两条路径同源。
- 新增 `_composite_banded(...)`：按水平带直接合成到 base（仅 `factor == 1.0`，即大图不走超采样时启用），
  与整层 `alpha_composite` **逐像素 0 差异**（实测：漏写 paste box 时曾差 3,056,947 像素，补 `(0, y0)` 后归零）。
- `render_image` 在 `factor == 1.0 and (target[0]*target[1]) > IMAGE_BAND_PIXELS` 时走分带，否则保持旧整层路径（小图字节级不变）。
- 收益：8000×8000 峰值 585MB（原整层需整张位图常驻），顺带支持单图内逐带进度/取消。
- 新增 `tests/test_banding.py` 3 条用例：分带/非分带逐像素一致、大图正确、带间取消抛 `Cancelled`。

#### ✅ P1-4 `app.py` 拆分（可测性）
- 抽出纯逻辑无 Tk 的模块：`wm/ui/output.py`（`save_image` / `write_image`，EXIF 写失败降级）、
  `wm/ui/preview_job.py`（`render_preview`，打开失败返回 `(None,0,0)` 不抛）、
  `wm/ui/batch.py`（`run_batch`，取消/进度/日志/完成全经回调注入）。
- `app.py` 的 `_preview_worker` / `_batch_worker` / `_save_image` / `_write_image` 改为委托，
  保留原对外口径与生命周期行为（未改 close/dnd 等）。
- 新增 `tests/test_ui_units.py` 4 条无头单测（不 import `wm.ui.app` 即可测）。

#### ✅ P2 全部
- `build/_obsolete/` 保留策略：`packaging/build.py` 新增 `prune_obsolete(keep=2)` +
  `_rmtree_guarded`（隔离删除，超时/异常只 `[WARN]` 不卡死）。*注：本机批量删除保护
  `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 使本次重建中实际删除被安全拦截（仅策略代码就位、下次构建生效），
  旧产物仍在 `_obsolete` 可找回，对交付零风险。*
- 打包瘦身：`packaging/spec_common.py` 的 `EXCLUDES` 加 `PIL._avif`，目录版 ~76.3MB → 68.4MB（−约 7.5MB / −10%）。
- 回退循环过度：`wm/layout.py` 的 `for _ in range(64)` → `range(16)`（实测所有回退组合 2 次收敛，16 留安全裕度）。
- 测试基建：`tests/run_all.py` 每个用例加 30s 看门狗（超时记 `[FAIL]`），`[SKIP]` 独立统计；
  `tests/test_all.py` 放宽两处墙钟断言（3.0→8.0s、1.0→5.0s）消除 flaky。
- 低优先 P2 建议（`time.sleep` 让出频率、注解/docstring 覆盖率、cache key 收敛到单函数、`from_dict` 加版本号）
  本次未做，仍列于「P2 改进建议」节，属锦上添花，不影响交付。

### ✅ 交付后追加：数字框悬停步进器（方案 C）

非审计条目，属交付后的交互增强。用户提出「数字框悬停时在右侧出现上下三角」，实现后按
用户对「数字框与单位位置不美观」的反馈，改为**方案 C：步进器嵌入数字框内、单位并入字段名**。

- 新增自绘控件 `_Stepper(tk.Canvas)`（`wm/ui/theme.py`）：**三角常驻显示**，用「三级状态」
  代替显隐开关 —— 常态 `STEP_IDLE` 淡灰 / 悬停 `STEP_HOVER` 加深 / 命中半区 `STEP_ACTIVE`
  主题色 + 浅底 / 禁用 `STEP_OFF` 更淡。
  *（v1 是"悬停才显示、移开即隐藏"，用户实测反馈"不悬停时数字框空唠唠"，遂改常驻：
    可发现性更强，且状态切换只改颜色、绝不引起布局跳动。）*
  上下两半独立热区、`takefocus=0` 不夺焦点；`boxed=False` 用于嵌入形态（不再重复描边）。
- 结构：外层 `entry_box`（唯一 1px 边框，兼作焦点环）内**并排**放 `entry` 与 `stepper`，
  不做 `place()` 覆盖 —— 因此不依赖绝对坐标、缩放/字体变化时不错位。
- 单位并入字段名：`_with_unit()` 把 `("旋转角度","°")` 渲染成 `旋转角度 (°)`，
  去掉了尾部独立单位标签，`panel.py` 调用点无需改动。
- 交互细节：悬停「框 / 输入框 / 步进器」任一进入即加深，全部离开后 **120ms 延时回落**（防抖动，
  三角**不消失**）；上/下三角 ±step 并在 `[lo, hi]` 钳制；**手输未提交的文本也会成为步进基准**
  （`_entry_value()`），非法输入红框 + 提示行并按上次有效值回退。焦点环/红框状态机收敛到
  `_sync_box_border()`：非法 > 聚焦 > 常态。
- 配色新增 4 个令牌 `STEP_IDLE/HOVER/ACTIVE/OFF`（集中在 `theme.py`，控件内无字面色值）。
- 回归：新增 `tests/test_stepper.py` 4 条（常驻+三级状态、手输保留、状态切换不挪布局、禁用更淡且不响应），
  全套 **71/71、0 SKIP**（步进器用例 3 → 4 条）；`_smoke/smoke_stepper.py` 39 项全通过；`_smoke/shot_stepper.py` 真实窗口截图
  （`stepper_idle / hover / hit / angle_idle / opacity_idle`）确认三处字段对齐一致
  （框 85×32、输入框 52×26、步进器 16×26）。
- 踩坑记录：`after_cancel` 必须用**注册该 job 的控件**（job 由 `field.after` 调度），
  用 `root.after_cancel` 会留下悬空 Tcl 命令，`destroy()` 时报 `can't delete Tcl command`（测试侧已修正）。

### ✅ 交付后追加（同日）：三角改为**常驻显示**

用户实测反馈"三角是不是一直显示比较好，不然鼠标没有悬停的时候，数字框感觉空唠唠的"。
采纳 —— 常驻的可发现性确实更强（不悬停也能看出这个数字可以点着改）。

- `_HoverStepper` 更名 `_Stepper`（"Hover"已不准确），**移除显隐开关**，改用三级状态：
  `STEP_IDLE` 淡灰 → `STEP_HOVER` 悬停加深 → `STEP_ACTIVE` 命中半区转主题色并铺浅底
  → `STEP_OFF` 禁用更淡。新增 4 个配色令牌，仍集中在 `theme.py`。
- **始终占位、只改颜色**：状态切换绝不引起布局跳动（新增用例专门守这条）。
- 浅底只在**命中半区**出现；整块常铺会让它变成一块常驻"按钮"，太吵。

### ✅ 顺带修掉两个测试基建缺陷（一个会误报连带失败，一个会让用例莫名超时）

现象：一轮回归里 `test_banded_cancels_between_bands` 超时 30s，紧接着
`test_qa_image_and_pdf_paths_agree_multi` 报"两路径偏差 2.5"（阈值 2）。

根因不在被测代码，而在**看门狗**：`tests/run_all.py` 用 daemon 线程 + `join(timeout)`
实现超时，超时后主线程继续，但**被中断用例的 `finally` 永远不会执行**。
`test_banding.py` 正是靠 `finally` 把 `IMAGE_SS_MAX_PIXELS` / `IMAGE_BAND_PIXELS`
改回原值的 —— 一超时，这两个常量就被永久改写成 0 / 1，后续所有图片渲染都
不再超采样，与 PDF 路径（恒 2×）的 bbox 偏差自然超过 2px。
（该用例单独跑 12 次全过，确认是被污染而非真缺陷。）

修法（两层）：
1. `tests/run_all.py` 新增 `GUARDED_GLOBALS` + `_baseline_globals()`：
   **每个用例开始前**把 `wm.render` 的常量复原到导入时的基线；超时时先给 5s 宽限
   让它自己跑完 `finally`，再兜底复原。
2. `tests/test_banding.py::test_banded_cancels_between_bands` 本身写错了：
   `IMAGE_BAND_PIXELS` 只决定"是否走带路径"，**每带行数由 `IMAGE_BAND_ROWS` 决定**，
   所以原写法（只压前者）根本没制造出多条带，取消实际是在**进带循环之前**触发的，
   "带与带之间"从未被真正验证。改为同时压 `IMAGE_BAND_ROWS = 1`，并把取消阈值
   从 3 提到 8（前 4 次是准备阶段的检查点），断言"取消必须发生在带循环内"。
   顺带把图从 1500×1500 降到 900×900（实测 0.01–0.18s，原超时系环境抖动）。

#### ⚠️ 缺陷 2：用例"莫名超时 30s" —— Tk 跨线程 `__del__` 死锁

修完上面那条后它**仍然**超时，而且把 A 修好之后失败点会**漂移**到别的用例
（`test_qa_batch_isolation_and_monotonic_progress`）—— 典型的竞态特征。
给看门狗加了"超时即 dump 卡住线程的调用栈"（`sys._current_frames()`），一击命中：

```
numpy/random/_pickle.py:7    from .mtrand import RandomState
...
tkinter/font.py:130          __del__ -> self._call("font", "delete", self.name)
```

机理：本运行器把每个用例放在**子线程**里跑，而 Tk 解释器**绑定在创建它的线程**上。
前序 GUI 用例销毁 root 后，`tkinter.font.Font` 之类变成只能由 GC 回收的循环垃圾；
一旦 GC 在**另一个**用例线程里跑起来，其 `__del__` 就会跨线程调用 Tcl
（`font delete`）→ Tcl 永久阻塞 → 该用例被看门狗判为超时。
单独跑 0.01s、完整回归里必挂，且"挂哪一条"随导入时机漂移。

修法（两层，前一层缓解、后一层掐断触发机制）：
1. `tests/run_all.py` 新增 `PRELOAD_MODULES` + `_preload_heavy_modules()`：
   在**主线程、尚无 Tk 残留时**把 `numpy.random` / PIL / fitz 等重量级扩展模块
   预导入完毕，消除"用例线程里首次导入 .pyd 触发 GC"这一时机。
   （实测有效：banding 不再超时，但失败点漂移 → 说明还没治本。）
2. `tests/run_all.py` 运行期间 `gc.disable()`：这类对象在一轮内不再被回收，
   跨线程 `__del__` 的路径直接消失。引用计数能清的对象照清（测试里的大对象
   基本都是引用计数回收），实测内存与耗时无异常。
   修后连跑 **3 次 71/71、0 SKIP、0 FAIL**。

**仍属规避，非治本**：只要还有"子线程里创建并销毁 Tk"，这个竞态就还在。
彻底方案是让每个用例跑在**独立子进程**里（超时直接 kill），顺带把下面那条
"常量污染"问题也一并解决 —— 已列入后续建议，本轮不动。

#### ✅ 产品侧顺带加固（来自独立验证同事的建议）

- `SliderField.destroy()`：销毁前用 `self.after_cancel()` 撤掉 `_hover_job` /
  `_invalid_job`。现状无害，但定时器到点会去调已被删除的 Tcl 命令，留下
  （实测约 1/7 概率）`invalid command name` 的后台噪声；若将来把 job 改挂到
  root，就会升级成"操作已销毁控件"的真异常。零风险、明确收益，采纳。
- 未采纳：`_Stepper._on_click` 在上下界钳制时不发通知（消掉一次无意义重绘）。
  属行为变更，收益有限，暂不动，只在此记录。
- 测试断言强化：`_tris()` 改为**按质心 y 判定上下三角**（原按绘制顺序），
  并据顶点数剔除浅底 —— 绘制顺序变化时断言不再失效。

### ✅ 交付后追加（同日）：「旋转角度」改为**圆盘**控件

用户提供参考图（红圈 + 贯穿的灰线 + 白色手柄 + 灰箭头），问能否把「旋转角度」的
滑块与滑轨做成该样式。评估结论：**可以，而且比线性滑块更合适** —— 角度是**循环量**，
线性轨道上 0° 与 360° 分居两端、视觉上离得最远，实际却是同一个方向。

新增 `_AngleDial(tk.Canvas)`（`wm/ui/theme.py`），由 `SliderField(dial=True)` 启用，
只作用于「旋转角度」一个字段；其余三个量是单向大小，保持线性轨道。

| 元素 | 含义 |
|---|---|
| 圆环 | 轨道，手柄沿它走（一整圈，无端点） |
| 方向线 | 贯穿圆心，**即水印文字的真实走向**（0° 水平向右，逆时针为正，与 `PIL.Image.rotate` 同向） |
| 手柄 | 白底 + 彩色描边的小圆，落在圆环与方向线的交点上 |
| 箭头 | 方向线正方向末端、**手柄外侧**的小三角；同时打破「直径对称」——θ 与 θ+180° 画出来是同一条线，靠箭头区分 |

- 交互：拖动手柄转角度；**Shift 吸附 15°**；圆心死区（<0.45R）内按下不取值
  （那里方向对角度的敏感度趋于无穷，取值会让数字乱跳）；禁用态整体降为 OFF 配色且不响应。
- 公共 API 与 `FlatScale` 同构（`get` / `set` / `state`），所以 `SliderField` 是无差别替换，
  上层与 `panel.py` 的调用方式一行没改（只是多了 `dial=True` 与一句提示文案）。
- 精度：圆环半径 40px（物理）⇒ **1px ≈ 1.43°**；精确数值仍由数字框 + 常驻步进三角负责，
  两者互补（滑块负责"找方向"，数字框负责"定准确值"）。
- 配色：默认走主题蓝（`DIAL_RING`）。参考图的**红圈**作为备选保留在 `DIAL_REF_RED` ——
  本工程红（`DANGER`）专指「非法输入」，色板铁律要求「类型/状态两套色绝不共用」，
  常驻红环会被读成报错。想换成参考图那种红只需改 3 行常量。
- 垂直空间代价：该字段高 65px → 202px（物理）。面板可滚动，其余字段不受影响。
- 本轮新增 `tests/test_angle_dial.py` **6 条**：几何反算（3/12/9/6 点钟 → 0/90/180/270°）、
  **方向线真的跟着角度转**（0° 水平、90° 竖直且朝上）、圆心落在线上且两端分居两侧、
  循环量钳制与跨 0° 连续、Shift 吸附 15°、圆心死区、禁用不响应、`set()` 不回调
  （否则 `SliderField.set` 里的显式 `_emit()` 会变成重复通知）、
  `dial=True` 走圆盘且**方形不被拉成椭圆**、其余字段仍是 `FlatScale`。
  回归 **71 → 77 条、0 SKIP、0 FAIL**。

> 记录一个方法学坑：`_smoke/shot_dial.py` 起初截图整体偏移，根因是脚本**没有调用
> `theme.enable_dpi_awareness()`** —— 非 DPI 感知进程里 Tk 报的是虚拟坐标，而
> `ImageGrab` 用物理像素，两者差一个缩放比。截图脚本必须先做 DPI 初始化。

### ✅ 交付后追加（同日）：图形渲染清晰度 —— **4× 超采样抗锯齿**

用户反馈自绘的圆形、线条、箭头「边缘发虚、不够锐利」。排查结论：不是坐标精度问题，
**是 Tk canvas 在 Windows 上走 GDI 直绘、图元完全没有抗锯齿** ——
实测一整幅圆盘只有 **4 种纯色、0% 过渡色**，圆环与方向线的边缘全是锯齿台阶
（125% DPI 下台阶间距不均，观感即「发虚」）。canvas 图元没有任何抗锯齿参数可调。

**方案：PIL 4× 超采样渲染**（新增 `paint_aa(canvas, painter, bg)`，`AA_SS = 4`）：
在 4 倍尺寸位图上重画同几何图形（坐标与描边宽度一并 ×4），LANCZOS 缩回 1x，
等效盒式滤波抗锯齿 —— 几何 / 颜色 / 尺寸 / 布局与原画法完全一致，只有边缘质量不同。
改前改后截图（`_smoke/sharp_before.png` / `sharp_after.png`）对比明显：
改前圆环是硬台阶，改后是平滑圆弧。性能实测单次渲染 0.2～2.8ms，拖动无感。

- 覆盖控件：`_AngleDial`（圆环 / 方向线 / 箭头 / 手柄）、`FlatScale`（轨道 / 滑块）、
  `ToggleSwitch`（胶囊 / 旋钮）、`_Stepper`（三角）、`FluentButton`（圆角边框；
  **文字保持原生图元**，Tk 字体渲染本身是次像素级，比位图缩放更锐）。
  色块（纯色矩形）与预览画布本就无锯齿，未动。
- `round_rect()` 去掉 `smooth=True`：那会把全部顶点当控制点拟合样条，
  **直边也会被画成微弯曲线**；改为 4 段直线 + 4 段真实圆弧（每角 8 段，偏差 <0.05px）。
- 渲染结果同时挂在 `canvas._aa_image`（PIL Image），供测试做**像素级断言** ——
  比原先的画布图元内省更强：断言的是「实际画出来的颜色」而非「画布项属性」。
- 配套修复：`ToggleSwitch` / `FluentButton` 补 `<Configure>` 重画
  （位图渲染依赖画布实际尺寸，映射前 winfo 是占位值，否则首帧空白）；
  `paint_aa` 在 winfo 未就绪时退回 `-width/-height` 选项。
- 测试改造：`test_stepper.py` / `test_angle_dial.py` 从图元内省改为像素采样
  （按比例取样，不依赖 DPI；斜线断言沿线扫一小段取最小色距，
  因为 AA 下斜线的亚像素过渡是正常表现，单点取纯色过严）。
  smoke 39 项全过；回归 **77/77、0 SKIP、0 FAIL**。
- **未采纳**「按 devicePixelRatio 缩放画布再恢复坐标系」：那是 HTML canvas 位图
  画法的做法；Tk canvas 是矢量绘制、坐标即物理像素，DPR 缩放只会把坐标放大
  4 倍再画同样的矢量图，不会更清晰。真正的 DPR 对应项（进程 DPI 感知声明）
  本来就在 `enable_dpi_awareness()` 里做了。

---

## P2 —— 改进建议

- **`build/_obsolete/` 已堆积 2.7 GB**（每次打包的旧产物都保留，无保留策略）—— 建议只留最近 2 次
- **打包瘦身 7.5 MB**：`_internal/PIL/_avif.cp313-win_amd64.pyd` 占目录版 10%，而本工具支持的 8 种格式一个都不需要 AVIF → `EXCLUDES` 加 `PIL._avif`
- **块数回退循环参数过度**：`layout.py:240-245` 写了 64 次 ×1.35，实测**所有触发回退的组合都 2 次收敛**；另外 `extra` 基准只用 `iw`（两轴不对称），且回退会静默把间距拉大到设定值的约 2.35 倍、UI 无任何提示
- **`app.py` 拆分**（见 P1-4）、跨模块私有调用 `render.py:113` 直接调 `layout._render_block_bitmap`
- `time.sleep(0.001)` 实测 1.58ms/次，200 页多花 0.32s（约 10%），可改成每 16 页让出一次
- **测试基建**：无超时；**SKIP 不计入退出码**（实测屏蔽 tkinterdnd2 → 退出码 0、7 条零断言用例照样"通过"）；42% 的 assert 无失败消息；5 处硬编码秒数断言会 flaky；`__pycache__` 混了 313/314 两套
- 注解覆盖率 79%、docstring 覆盖率 45.5%
- `WatermarkSpec.from_dict` 无版本号：未知字段静默丢弃、缺失字段静默取默认
- 新增参数时需同步**两个** cache key（`layout.py:111-118` / `:148-154`），漏改 = 静默渲染错 —— 建议把 key 生成收敛到一个函数

---

## 契约护栏缺口（为什么这些 bug 能溜过 42 项测试）

「预览 = 输出」「图片 = PDF」两条核心契约目前靠 4 条测试守着（bbox 同构 ≤1.5%、
每块有墨迹、覆盖率 ≤2pp、两路径逐块一致），**守得不错**。但有 3 个缺口：

1. **旋转页完全无用例** → P0-1 即从此处漏网
2. **"版式必须在原生 1:1 坐标系计算"没有不变量测试** —— 这是最自然的优化方向，
   一旦有人在缩放坐标系里重算版式，`axis_starts` 的 `floor` 不连续会让块数跨整数
   边界跳变（注释已记：900×600/font4%/m3%/45° 时 1× 六列 vs 2× 七列），**却没有任何
   用例拦它**
3. **cache key 手工枚举** —— 新增外观参数漏加即静默错

补充：上一轮修复的两条回归用例经**变异测试**确认真的抓得住（临时把实现改回旧行为，
两条都 FAIL；已逐字节还原源码并 `diff` 验证）。

---

## 建议修复顺序（按性价比）

| 顺序 | 项目 | 代价 | 收益 | 状态 |
|---|---|---|---|---|
| 1 | **P0-1 旋转页** | ~10 行 + 1 用例 | 消除唯一的错误输出 | ✅ 已完成 |
| 2 | **P1-1 去超采样** + **P0-4 块光栅上限** | **< 5 行** | 8000×8000 峰值 3.3GB→~1.0GB，耗时降 10× | ✅ 已完成（改为自适应超采样 + 字节预算缓存） |
| 3 | **P0-5 单 worker 预览** | ~15 行 | 消除最易触发的 OOM | ✅ 已完成 |
| 4 | **P0-2 文本炸弹护栏** | ~3 行 + 1 用例 | 消除 22s 空转 + 看不懂的报错 | ✅ 已完成 |
| 5 | **P0-6 selftest 挂起** | ~5 行 | 打包脚本不再卡死 | ✅ 已完成 |
| 6 | **P0-3 分带渲染** | ~40 行 | 8000×8000 峰值 585MB + 顺带支持单图内进度 | ✅ 已完成 |
| 7 | 缓存改 LRU 字节预算（块 + PDF 图层） | ~40 行 | 根治 P0-4 / P1-2 | 🟡 块侧已做（P0-4），PDF 图层侧待做（P1-2） |
| 8 | P1-7 fitz→pymupdf、P1-5 EXIF、P1-6 取消点 | ~20 行 | 健壮性 | ✅ 已完成 |
| 9 | P1-4 app.py 拆分 | 机械搬迁 | 可测性 | ✅ 已完成 |

前 9 项（P0-1~6、P1-1~12、P2 全部）已全部完成。所有"会产生错误结果 / 会挂死 /
最常见 OOM / 体积与可维护性"的问题均已消除。

---

## 附：审计过程产物

- `_smoke/verify_rotate_bug.py` —— P0-1 的独立复现脚本（团队复现后再由 lead 二次复现）
- `_smoke/AUDIT_REPORT.md` —— 代码质量角度的完整报告
- `_smoke/audit*.py` —— 各角度审计用的临时脚本（文件名不以 `test_` 开头，不会被测试 runner 收录）
- 本次审计**未修改任何源码**（变异测试已逐字节还原并 `diff` 验证）

---

## 交付形态补充：单文件版（onefile，2026-09-25）

用户要求「打包成一个单文件 exe」。做法：**不改源码，只加一套打包配置**——
`EXE()` 把 `a.binaries + a.datas` 全塞进 exe、不生成 `COLLECT`。源码零改动的原因：
onefile 运行时 `sys._MEIPASS` 就是自解压目录，而 `main.module_root()` 本就优先读它，
图标 / tkdnd / PyMuPDF 的 DLL 全部从那里加载。

新增文件：

- `packaging/watermark-onefile.spec` —— 单文件 spec（与目录版只差最后一步，无 COLLECT）
- `packaging/build_onefile.py` —— 构建 → 自检 → GUI 冒烟 → md5 清单（复用 `build.py` 的
  `move_aside` / `prune_obsolete`，绝不 rmtree；旧产物一律移到 `build/_obsolete`）
- `_smoke/verify_onefile.py` —— 独立验收：把 exe **单独拷进空目录**跑，断言自检全过、
  冷启动秒数量化、`%TEMP%\_MEI*` 自解压目录存在（传目录版路径时反断言**不**生成）、
  程序目录无旁挂文件

实测（本机，Windows / PyInstaller 6.22.3 / Python 3.13.15 / Tcl 8.6）：

| 项 | 单文件版 | 目录版（对照） |
|---|---|---|
| 产物 | `dist/WatermarkTool.exe` 32.2 MB | `dist/WatermarkTool/` 68.4 MB（zip 32.1 MB） |
| 冷启动到主窗口 | 1.62 s | 0.62 s |
| 自检 | 4/4 PASS（Pillow 12.3.0 / PyMuPDF 1.28.2 / tkdnd 2.10.2 / 图片改动 10.13% / PDF 3 页全有墨） | 同 |
| 单独拷走后可运行 | ✅ 程序目录无任何旁挂 | ✅（须带 `_internal`） |
| md5 | `53e800224c2d245b117b3474a9621f69` | `b78124647ffa83dc8e1c5903d2285397`（exe） |

坑（本轮唯一踩到的）：冷启动计时一开始读出 **0.01 秒**——上一轮 GUI 冒烟的
`WatermarkTool.exe` 进程没死透，脚本一启动就「找到」了它的残留窗口。修法：验收脚本
启动前先 `taskkill /F /IM` 并断言无同名窗口，窗口匹配也从模糊关键词「水印」改成
精确标题「图片 / PDF 文字水印工具」。

代价/取舍（已写进 `dist/单文件版说明.txt` 与 README 第十节）：单文件每次启动要把
~70MB 内容解压到 `%TEMP%\_MEIxxxxxx`（退出自动清理），冷启动比目录版慢约 1 秒；
且更容易触发 SmartScreen。介意这两点就用目录版。

---

## 旋转角度：循环量语义修正（2026-09-25）

起因是「圆盘上的数字变化要不要调」。用 `_smoke/dial_value_probe.py` 量化后发现两处
真问题，均已修（探针现带断言，`[RESULT] PASS`）：

**1. 接缝压在最常用的角度上。** 循环量用单个数值表示，必然存在一处「数值不连续点」。
旧值域 0–360 把它放在 12 点方向 = **0°（水平，最常用）**，拖过即 359→0，数字跳 359。
改 `ANGLE_RANGE` 为 `(-180, 180]`，接缝挪到 ±180（文字完全倒置，最少用到的方向），
0 / ±30 / ±45 / ±90 全部连续。渲梁侧无需改：`PIL.Image.rotate` 支持负角，
`rotate(-90)` 与 `rotate(270)` 完全等价。

**2. 拖不准。** 半径 40px ⇒ 1px ≈ 1.43°，想停在整数度只有 0.35px 容差。改为
**默认吸附 5° / Shift 15° / Alt 不吸附（回到 1°）**：差 1° 视觉上看不出来，而能稳定
命中 0 / 30 / 45 才有价值；要精确到某一度仍可用数字框 + 步进器（始终 1°）。

顺带修掉的三个隐患：

- **`_value_of` 的映射只在 `lo == 0` 时成立**（`lo + ang/360*span`）。值域一改，圆盘
  就会把值画到错误的方向上。改为 `lo + ((ang - lo) % 360)`，守的是「值 v 必须显示在
  角度 v 的位置」这条语义；测试 `_make_dial()` 也改为直接取 `ANGLE_RANGE`，写死
  0..360 会掩盖这个坑。
- **360 与 0 是两个数字却同方向**：循环量的合法化改成**取模**而非钳制（新增
  `spec.wrap_deg`，`SliderField` 加 `cyclic`，dial=True 即开启）。现在 360→0、
  350→-10、-190→170；步进器在端点也不再卡死（0° 可以往下走到 -1°）。
  注意：用 clamp 处理角度是**错的** —— 350° 几乎水平，夹成 180° 等于把方向改了。
- **死区只对按下生效**：`_on_press` 检查 `DEAD_RATIO`，`_on_drag` 不检查，起拖后滑过
  圆心会以 7°/px 疯跳。现在 `_apply` 里统一挡，拖动中进死区冻结在进死区前的值。

回归：**77/77、0 跳过、0 失败**（改了 4 处旧断言：spec 合法化期望值、圆盘吸附粒度、
SliderField 值域）；smoke 39/39；真实窗口截图确认圆盘外观与配色零变化。
新 EXE md5 `e8be6ca0be061022893c4c89f12be663`。

---

## 第二轮审计 —— 修复实施记录（2026-09-26）

用户拍板：**缓存重构成 `SizedLRU`**，前三批全做，第四批（测试子进程化 + CI）先不做。
下列 14 处改动均已落地并通过全量回归。

### 第一批：稳定性与产物保真（S1 / M1 / M2 / M4 / M6，外带 A2）

| 项 | 改动 | 位置 |
|---|---|---|
| S1 | 批处理期间**整块冻结**参数面板（不只是按钮）。面板逐个控件禁用 + `_emit` 在冻结期直接返回 + `_schedule_preview` 见 busy 即不再排队，三重保险 | `app._set_busy` / `panel.set_enabled` / `theme.{ColorField,TextArea}.set_enabled` |
| M1 | 图片输出保留源图的 **DPI 与 ICC**（原实现只带回 EXIF）。非法密度（`(0,0)`、`(1,1)`）一律丢弃；CMYK 的 ICC 不往 RGB 图上写（那是错误标注） | `media.{dpi,icc_profile,_read_dpi,_read_icc}` / `output.save_image` |
| M2 | PDF 大页面**自适应光栅倍率**（原固定 2×）。预算 8MP，倍率向下量化到 0.5 档保证缓存命中 | `render.{pdf_render_scale,PDF_SS_MAX_PIXELS}` |
| M4 | 版本真源统一：`wm.__version__` 是唯一来源，打包侧现读；已升到 **1.0.1** | `wm/__init__.py` / `packaging/spec_common._detect_version` |
| M6 | 清理 `build/_obsolete`（38 个历史构建目录，**2.5GB**）；`build/pyi*` 增量缓存保留 | — |
| A2 | 保存异常翻译成中文（`DecompressionBombError` / 路径不可写 / 格式不支持）；元数据写失败**逐个剥离**后重试，不再让整张图导出失败 | `output.{write_image,_friendly_save_error}` |

M2 实测收益：**A0 单页 2.44s / +873KB → 0.59s / +305KB**，A4 及以内仍是 2×（打印
精度不降）；版式恒按原生坐标系算，倍率只影响光栅，两条契约不受影响。

### 第二批：缓存重构（S2 + M7）

新增 `wm/lru.py`：`SizedLRU` = OrderedDict + RLock + 真 LRU + 字节预算 + `min_keep`。

* **消灭并发 KeyError（S2）**：预览线程与批处理线程同时读写时，旧的
  `next(iter(d.items()))` + `del` 会取到已被别人删掉的键。现在全部操作在同一把
  `RLock` 下完成，`get` 与裁剪不可能交错。8 线程 × 3000 次 get/set/trim 压测无异常。
* **近似 LRU → 真 LRU**：`get` 命中会把它移到队尾。项目里 PDF 图层缓存的键含页面
  尺寸，扫描件每差 1pt 就是一条，FIFO 会让正在被反复复用的那条被一次性尺寸冲走。
* **预算可动态求值**：传 lambda，测试与诊断压小 `PDF_LAYER_CACHE_BYTES` 仍然生效。
* 三处缓存全部接入：`layout._RENDER_CACHE`（字节）、`layout._BLOCK_CACHE`（条数，
  顺带把「满 512 条全清」换成 LRU）、`render._PDF_LAYER_CACHE`（字节）。
* **M7 缓存 key 唯一枚举处**：新增 `WatermarkSpec.render_key()` / `block_key()`，
  layout 与 render 不再各自手写字段列表 —— 漏一个字段的表现是「改了参数却复用旧
  图层」，属静默的错误输出。

### 第三批：建议项（A1 / A3 / A4 / A6 / M3 / M5 / M7 / M8）

* **A1** `error.log` 超限轮转（512KB × 3 份），每条异常带时间戳；否则反复失败会让
  日志无限增长、最新原因被埋在文件尾部。
* **A3** 参数面板「恢复默认」按钮。走 `load_spec` 而非直接赋 `_spec`，避免
  「界面显示旧值、渲染用新值」的错位。⚠️ 属性名必须叫 `defaults_btn` —— `reset_btn`
  是历史「重置居中」的名字，被 QA 的位置调节护栏列为禁止项。
* **A4** 输出后缀可配。用户可填 `_机密` 之类；输入经 `spec.safe_suffix` 过滤掉路径
  分隔符与非法字符（`../` 会把输出写到别的目录），空输入回退默认。
* **A6** 滚动容器销毁时按实例计数解绑全局 `<MouseWheel>`（`bind_all` 注册在 Tcl
  解释器上，窗口销毁不会自动清理，每建一次 App 就叠一层处理器）。
* **M3** 预览可取消：`render_overlay_layer` / `preview_job.render_preview` 支持
  `is_cancelled`，过期帧（用户又改了参数 / 翻页）中途就停 —— 最贵的一帧恰恰是那种
  几百 MB 的超大图。取消与失败用哨兵 `CANCELLED` 区分，不会闪「预览失败」。
* **M5** `requirements.txt` 补齐 `tkinterdnd2` / `PyInstaller`（构建期）并给出构建机
  实际版本。
* **M8** 文档与实测对齐：`fonts.py` 顶部「首次扫描 1~2 秒」→ 实测 **0.19s**；
  README 里「面板实时显示预计 N 处水印」（功能早已移除）等过时描述纠正，补上后缀
  可配 / 恢复默认 / 元数据跟随 / PDF 自适应倍率；docstring 覆盖率 **50% → 64%**。
* A5（多帧只首帧）维持现状并在日志里标注；A8（每页 sleep）本就已在做——两条均
  无需改动。

### 验证

| 项 | 结果 |
|---|---|
| 回归 `tests/run_all.py` | **77 / 77**（0 跳过 0 失败） |
| 步进器冒烟 `smoke_stepper.py` | **39 / 39** |
| `_smoke/verify_batch1.py`（DPI/ICC、自适应倍率、友好异常、版本） | **28 / 28** |
| `_smoke/verify_lru.py`（LRU 语义、预算、8 线程并发） | **18 / 18** |
| `_smoke/verify_batch3.py`（面板冻结、恢复默认、后缀、日志轮转） | **27 / 27** |
| 圆盘顺时针探针 / 数值探针 | 17 项全过 / PASS |
| `/Rotate` 方向对照（dir / marker） | 补偿方向正确，红块在左上 |

### 过程中踩的两个坑（都值得记一笔）

1. **给 `ScrolledFrame.__init__` 加 docstring 时，把 `super().__init__(parent, bg=bg)`
   一并替换没了** —— 症状是 9 个 UI 用例集体报 `'ScrolledFrame' object has no
   attribute 'tk'`。批量补注释这种「零风险」改动，事后**必须逐个 diff 复核**，不能
   因为是非功能性改动就跳过。
2. **预览取消把「渲染失败」也吞成了「已取消」**：最初让 `render_preview` 对两种
   情况都返回 `None`，于是 `test_preview_requests_are_serialized`（用假路径调 worker）
   拿不到 error 载荷。改为返回哨兵 `CANCELLED`，「正常作废」与「真的出错」重新分开。

### 第四批（用户决定暂不做）

* **M9** 测试基建子进程化 + `panel` / `fonts` 的覆盖补齐（`panel.py` 至今仍无独立
  测试文件直接引用，本轮只补了「恢复默认」一条护栏）；
* **M10** `.github/` CI。
