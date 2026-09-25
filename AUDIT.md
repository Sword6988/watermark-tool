# watermark-tool 审计报告

- 审计日期：2026-09-25
- 代码规模：5552 行（含测试 1571 行）
- 基线：`tests/run_all.py` **42/42 通过**；冻结自检 4/4 PASS
- 审计方式：架构 / 测试覆盖 / 代码质量 三个角度并行独立审计 + 交叉验证
- **修复进度（2026-09-25）**：P0 已修 6/6、P1 已修 12/12、P2 全部完成；回归 **67/67 通过、0 SKIP**
  （连跑 8 次无 flaky）；已重新打包并通过字节级（符号级）验证。
  - 第 1 批产物：EXE md5 `ce4504633a47c7c83101b2ec55481bd7`
  - **最终产物（含全部 5 批修复：P0-1~6、P1-1~12、P2 全部）**：EXE md5 **`39b1859228c28c55a0bee0f9ed447b69`**
    （本批重建，目录版 68.4MB / 便携 zip 32.1MB）
    字节级校验 **0 项不符**（新增 P0-3 分带符号 `IMAGE_BAND_PIXELS` / `IMAGE_BAND_ROWS` /
    `_prepare_layer` / `_composite_banded`，及 P1-4 三模块 `save_image` / `write_image` /
    `render_preview` / `run_batch` 的模块归属核对；`IMPORT_NAME` 级 `pymupdf` 正面证据仍在）；
    冻结版 `--selftest` PASS、GUI 冒烟 4 秒存活、zip CRC 通过、`PIL/_avif*.pyd` 已不在包内。
    回归 **67/67、0 SKIP**（60 → 67，新增 `test_banding.py` 3 条 + `test_ui_units.py` 4 条）。
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
