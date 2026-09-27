# 最终全面审计 · 2026-09-26（版本 1.0.3 / commit 38263d6）

> 范围：代码逻辑与边界、错误处理与异常路径、安全与输入校验、性能与资源泄漏、
> 重复与命名、依赖与配置、与需求/设计的一致性。
> 方法：四路独立只读审查（解密与文件 I/O、UI 层、渲染与缓存、启动与打包配置），
> 关键指控由本人复核源码行号后确认；未复核通过的条目已剔除或标注「需确认」。
> 结论先行：**核心功能正确**，曾存在 4 处「界面永久卡死 / 静默退出」级别的缺陷。
>
> **后续状态（同日，第二轮）**：P0 四项**已全部修复并回归通过**（103/103），新增
> `tests/test_robustness.py` 锁住。随后按「修复剩下所有的问题」把 **P1/P2 全部
> 条目**（S5–S10、M1–M23、L1–L20）逐项落地，新增 `tests/test_core_units.py`
> （12 条）与 `tests/test_entrypoint.py`（9 条），全量回归 **131/131**。
> 按用户要求**始终未打包发版**（代码修完即止）。
> 内网真机端到端验证亦已通过（加密文件可自动解密并出水印成品），
> 见 §四 第 1、2 条。当前状态：**内部可用**，审计清单已清零（见 §五）。

---

## 一、问题清单

### 严重

| # | 位置 | 表现 | 原因 |
|---|---|---|---|
| S1 | `wm/ui/app.py:942-943` | 关窗时**先** `_release_all_plain()` 删掉临时明文，**后** `_join_batch()`（最多等 1s）等批处理线程。批处理正在读的明文被删 → 线程抛异常把正常文件记进失败清单，或写出半截输出；`dlp.release` 在 Windows 上撞 WinError 32 还会让后续明文漏删 | 清理动作与「等待消费者退出」的因果顺序颠倒；`_close_doc→release→join` 与 925-931 行 docstring 声明的顺序不符 |
| S2 | `wm/ui/app.py:857-873` | `_poll_results` 的 `try` 只接 `queue.Empty`。`_handle_result` 任一异常（关窗竞态的 TclError 等）都会穿出 → 第 872 行 `after()` 永不执行 → **轮询链永久断掉**：进度冻结、done 收不到、`_busy` 永远 True、预览永不刷新，窗口却还活着且无任何提示 | try 的作用域只为「用 Empty 结束 drain 循环」，没有为回调体兜底 |
| S3 | `wm/ui/app.py:818-825`（配合 `wm/ui/batch.py:61-98`） | `_batch_worker` 直接调 `run_batch`，两侧都无 try/finally。`read_path` / `on_progress` / `/ total` 任一抛异常 → 线程死亡 → `on_done` 永不入队 → **界面永久「处理中」，所有按钮永久禁用**，只能杀进程 | 只有 per-file try，缺覆盖整批的 `try/finally` 保证 `on_done` 必达；无看门狗 |
| S4 | `main.py:63`（`user_log_dir`）、`:310`（`install_stdio`）、`:324`（except 分支首句） | `os.makedirs` 无异常保护；`install_stdio()` 是 `main()` 第一条语句且在 try 外；崩溃兜底分支的第一句又调同一个函数。`%LOCALAPPDATA%` 不可写/磁盘满时，`OSError` 从**异常处理器内部**抛出 → 无 error.log、无弹窗、`console=False` 的 exe **双击后什么都不出现** | 兜底路径与被兜底路径依赖同一份未加固的 I/O |
| S5 | `wm/dlp.py:385,429,586` + `wm/ui/app.py:449,506-511` | 单文件解密最坏 **60 + 3×60 + 90 = 330 秒**，且 `resolve()` 同步执行、每文件只 `update_idletasks()`（**不处理输入事件**），**没有取消钩子**。拖 20 个加密文件最坏几十分钟无响应且关不掉 | 超时是「每通道/每次尝试」粒度，缺「每文件总预算」；解密链路未接入 `is_cancelled`；`update_idletasks` 不等于「不卡界面」 |
| S6 | `wm/dlp.py:208-210`（`release`）、`:213-219`（`release_all`）、`:172-188` | 明文删除失败**零日志、零重试、零上报**（`except OSError: pass` + `rmtree(ignore_errors=True)`）；且**没有启动期 GC**，任务管理器杀进程/崩溃/`os._exit` 残留的 `wm-dlp-*` 目录下次启动也不会清。Windows 上最常见的原因就是文件仍被占用 | 生命周期只依赖进程内 `atexit`；「删完校验」缺失 |
| S7 | `wm/render.py:313`（`scale=1.0` 硬编码）+ `wm/ui/preview_job.py:70-72` | 预览对超大页**无尺寸上限**：8000×8000 图、画布仅 1000px，仍分配 256MB RGBA 层 + LANCZOS 降采样；且预览先把**整图全分辨率解码**（`media.page_image` 未用 `draft()`）再缩放。单帧峰值 ~700MB–1GB，且进入 `page_image` 后**不可取消** | 输出路径有两道闸（`IMAGE_SS_MAX_PIXELS` + `IMAGE_BAND_PIXELS`），预览路径没有，两条路径防护强度不一致 |
| S8 | `wm/ui/app.py:761-777`（`_start_batch`）vs `:643-646`（`_schedule_preview`） | 点「开始处理」时**不取消、不等待**在飞的预览线程；`_set_busy(True)` 只改控件启用态，从不 bump `_preview_gen` 也不清 `_preview_pending`。于是预览（~777MB）与批处理同时驻留 → 进程可能被系统直接杀掉且无报错（786-788 行注释自己描述了这条风险） | busy 闸门只挡「新请求」，不挡「已在途的帧/pending」 |
| S9 | `wm/fonts.py:300-306`（`resolve` 末档 `next(iter(mapping.values()))`）、`:336-340`（`pil_font` 回退 `load_default`） | 字体回退最后一档是「任意第一个字体」，若首个是 Wingdings/Webdings 之类符号字体，中文水印变成符号或空白；`load_default` 的 Aileron **无 CJK 字形**。用户看到「水印没了」或豆腐块，**界面无任何提示**，渲染层也检测不到（bbox 为空只回退成整块尺寸） | 回退链只保证「返回字体对象」，不保证「能排出当前文本」；现成的 `can_render_cjk`（196-228）没用在回退链上 |
| S10 | `tools/LDDec/dec.exe`（已 git 跟踪）+ `packaging/spec_common.py:114-123` + `wm/dlp.py:804-821` | 第三方预编译二进制随仓分发并打进 exe，**无 SHA256、无来源比对、无许可证文件**；且解密通道**默认启用**（`WM_DLP_ENABLE` 缺省 `"1"`），首发行为就是起 `cmd.exe`/`powershell.exe` 子进程读文件写临时目录 —— 与 `docs/integration-lddec.md:131,163` 自己写的「不能投放现成二进制」「默认关闭，需显式开启」**直接矛盾**。`.gitignore` 未屏蔽 `*.exe` / `tools/LDDec/` | 为「开箱即用」把部署便利置于供应链审计之上（此项为**用户已决策接受**，但文档红线与实现未同步，且缺少可追溯记录） |

### 中等

| # | 位置 | 表现 / 原因 |
|---|---|---|
| M1 | `wm/dlp.py:438-443` | PowerShell 用 `[IO.File]::OpenWrite`（**不截断**）：若 `release(dst)` 因占用删除失败（且被静默吞掉），会在旧明文上只覆盖前 N 字节，**尾部残留上一个文件**；而 `verify_plaintext` 只校验头部 → 判定通过 → 把污染明文交付渲染。应改用 `FileMode.Create` |
| M2 | `wm/dlp.py:285-288` vs `:676-681` | `CommandProvider` 不受 `_lddec_lock` 保护。若 `WM_DLP_DECRYPT_CMD` 指向同一个 dec.exe（TCP 版固定监听 34500），并发必冲突（表现为「返回 0 但无产物」）。串行锁只保护内置 `LddecProvider` |
| M3 | `wm/dlp.py:713-723` → `:804` → `wm/ui/app.py:449` | 畸形 `WM_DLP_DECRYPT_CMD`（如引号未闭合）让 `shlex.split` 抛 `ValueError`，一路逃到 Tk 回调；而 `_resolve_sources` 没有 `resolve_all`（`dlp.py:886-892`）那样的 per-file 兜底 → **整个添加批次中断**，已成功解析的文件也不入列表。对比 `magic()`（99-101）有降级 |
| M4 | `wm/dlp.py:176-187` | `_ascii_temp_root()` 找不到纯 ASCII 根时**静默回落** `None`（`mkdtemp(dir=None)` 合法），恰好触发它自己那条「请确认临时目录为纯 ASCII 路径」的报错 —— 代码刚放弃的保证又拿来解释失败，且无日志 |
| M5 | `wm/dlp.py:807-814` + `README.md:393` | `WM_DLP_TIMEOUT` **只在设置了 `WM_DLP_DECRYPT_CMD` 时生效**，内置链（60/60/90）全是硬编码。文档写「单文件解密超时秒数」读起来像全局手段，管理员设 `=10` 期望快速失败实际无效（叠加 S5） |
| M6 | `wm/dlp.py:92-101` | `WM_DLP_MAGIC` 配错（奇数位/非十六进制）时 `bytes.fromhex` 静默回落默认魔数，无日志无提示 → 表现为「加密文件一律探测不到」，与「本机没加密」无法区分 |
| M7 | `wm/dlp.py:879` → `wm/ui/app.py:456-457` | 明文生命周期绑定「文件在列表里」而非「是否需要读」：加入 200 个加密 PDF 会立刻占数 GB 并持续到关窗，无 LRU、无上限、无空闲回收 |
| M8 | `wm/ui/batch.py:64-66` + `wm/ui/preview_job.py:88-91` | 取到明文路径后不校验存在/大小/mtime；明文被临时目录清理工具或杀软删掉 → 预览被裸 `except Exception` 吞成「预览渲染失败：文件无法打开」，真实原因丢失且**无任何日志**（对比 app.py:971/983 都打了 `[WARN]`）。若被**替换**则照读不误 |
| M9 | `wm/ui/output.py:86-104` | (a) `image.save` 全部重试失败后，**Pillow 建出的 0 字节/半截文件留在已预定的输出路径**；(b) 元数据降级是**累积剥离**的，真凶若是 `icc_profile`，第 1 轮会先无条件剥掉 `exif` 且**无任何提示** —— docstring「不会静默丢元数据」不成立 |
| M10 | `wm/media.py:158-164` | `normalized_exif.tobytes()` 位于 try 之外，畸形 EXIF 抛异常时 `source_image` 从未 close → Windows 句柄占用 → 后续 `dlp.release`/`rmtree` 全失败（且静默），与 S6 直接耦合 |
| M11 | `packaging/*.spec:24,35` `optimize=1` + `wm/media.py:185,199` | 生产代码用 `assert self._pdf is not None` 作契约守卫，而冻结版 `-O` 会剔除 assert → 同类错误在源码版是 AssertionError、在 exe 里退化成 `TypeError: 'NoneType' ...`，排查成本更高 |
| M12 | `wm/ui/theme.py:1091-1156` | 禁用态的 `SliderField` 仍能被滚轮/方向键/手输改值：`_emit` 里 `if self._enabled` 只挡**通知**，`_value` 与界面文本已被改掉 → 「框里显示 37、渲染用 12」。同文件 `ColorField._set`、`TextArea._on_modified` 是在**入口**处挡，三个控件三套做法 |
| M13 | `wm/ui/app.py:289-294,346-361` | 拖拽目标三重注册（文件列表 / 预览区 / 左侧面板，且 files_list 是 left_frame 的后代），若 `return "break"` 未能终止向祖先派发，一次投放会触发两次 `_on_drop`（重复解密 + 可能弹两个失败框）；`splitlist` 失败的回退把整串当一个路径，多文件含花括号时整体失效 |
| M14 | `wm/ui/app.py:783-797`（`_set_busy`）、`:894` | (a) 翻页按钮 `prev_btn/next_btn` **没被禁用**，批处理期翻页 → 导航显示第 5 页、画面仍是第 2 页；(b) done 分支只 `_set_busy(False)`，**不为批处理期间被丢弃的预览补跑** → 结束后预览与选中项/页码/参数全部错位且永不刷新 |
| M15 | `wm/layout.py:303-309` | `MAX_TILES` **不是硬上限**：16 轮回退不收敛就直接返回最后一次结果（实测 page=20000×20000、退化 ink=2×2 → 21609 块 > 4000）。`layout.py:34` 注释与 `tests/test_all.py:249` 断言都声称是硬上限 |
| M16 | `wm/layout.py:219-225` + `:171` | `block_geometry` 用 `spec.copy(opacity=1.0)` 的探针渲完整位图只为量 bbox，但探针也会进缓存（key 的 opacity 不同）→ 同一 (spec, 字号) 存**两份等大位图**，大块场景驻留直接 ×2 |
| M17 | `wm/fonts.py:145,342-344`（另 `:137,227`） | `_font_cache` / `_cjk_map` 是**无锁裸 dict**，被 UI 线程与工作线程共享写；「检查长度→clear→插入」三步无锁可交错；到 128 条**整体清空**造成悬崖式抖动。`SizedLRU` 已解决同类问题但 fonts 没复用 |
| M18 | `wm/render.py:528-530` | PDF 图层经 PNG 编解码往返（PIL → bytes → `fitz.Pixmap`），每遇新页面尺寸付一次全页 encode+decode，缓存未命中时每次都付。未走 `Pixmap(samples)` / `set_alpha` 直传 |
| M19 | `wm/render.py:119-126` | `pdf_render_scale` 的 `if area <= 0` **挡不住 NaN**（NaN 比较恒 False），随后 `int(math.floor(...))` 抛 `ValueError`。同类：`wm/spec.py:127-137` 的 `to_float` 过滤 NaN 但**不过滤 inf** → `math.fmod(inf,360)` 抛 `ValueError`，使自称「任何非法值都不抛异常」的 `normalized()` 抛异常 |
| M20 | `wm/layout.py:91-103` vs `wm/spec.py:150-167` | 颜色在渲染层自行解析 `#rrggbb`，失败一律静默回退红色；`normalize_color`（能识别 `"red"` 等）**渲染路径不调用**，`render_key` 也直接用未归一的 `self.color` → `"#D32F2F"` 与 `"#d32f2f"` 是两个缓存 key，像素相同却各存一份 |
| M21 | `README.md:16,22,197` | 硬编码 `C:\Users\Shibeng\...\Python314\python.exe`（**泄漏个人用户名**，且本机是 `Administrator`/Python313）→ 三处示例命令复制即用失败；同仓三种 Python 版本口径（README 3.14 / 打包脚本 3.13 / AUDIT 3.13.15） |
| M22 | 测试盲区 | `wm/lru.py` **无单测**（真 LRU 顺序、`min_keep`、单条超预算不入缓存全部只靠被 gitignore 的 `_smoke/verify_lru.py`）；`wm/fonts.py`、`wm/ui/panel.py` 无独立测试；`main.py` 与 `packaging/` **零覆盖**（交付门禁 `selftest()` 自身没有用例）；`tools/LDDec/dec.exe` 的真实可执行性从未被自动化验证（19 条 dlp 用例全是假 exe + mock `subprocess.run`） |
| M23 | `packaging/build_onefile.py:56-71` | 单文件版构建断言只查「是单文件 + 无 `_internal` + 自检通过」，**没有**对 mupdf DLL、tkdnd、也没有对 `tools/LDDec/dec.exe` 是否入包的校验；目录版（build.py:114-119）反而有 DLL 断言 |

### 轻微（精选）

| # | 位置 | 说明 |
|---|---|---|
| L1 | `wm/ui/theme.py:104 / 372 / 553` | 名字 `KNOB` 三重冲突：模块级是**颜色**，`FlatScale.KNOB=16` 是**直径**，`_AngleDial.KNOB=8` 是**半径**；`_redraw` 里 `knob_fill = KNOB` 与 `px(self.KNOB)` 混写，极易误读 |
| L2 | `wm/ui/theme.py:1212-1283`、`:69/75/89/92/95/96/136` | `ToggleSwitch` 整个类（~70 行）无实例化点，配套令牌随之成为死代码；另有 7 个色标定义后全库无引用 |
| L3 | `wm/ui/app.py:1007-1013` | `App.run()` 是无人调用的死代码，且缺 `set_window_icon` / argv 预载 / tkinterdnd2 降级三件事（`main.gui_main` 都有）—— 一旦被误用会得到一个残缺入口 |
| L4 | `wm/ui/output.py:119` | `isinstance(exc, OSError) or isinstance(exc, PermissionError)`：后者是前者子类，右半永远冗余 |
| L5 | `wm/ui/theme.py:176-178` | `round_rect` 在 `x1<x0`（极窄窗口/未布局完）时 `r` 为负，`dash` 被带进 `create_rectangle`，画出反向/不可见矩形 |
| L6 | `wm/ui/panel.py:104-121` vs `:149-156` | `ScrolledFrame.destroy()` 没有 `after_cancel(self._wheel_job)`；`_LIVE_COUNT` 依赖「root.destroy 递归调子控件 destroy」这一隐含前提，两个 App 并存时会误解绑另一个的滚轮 |
| L7 | `wm/ui/theme.py:1483-1504` | `TextArea.set(..., notify=False)` 不生效（写 tk.Text 触发 `<<Modified>>` → 回调），且 `<<Modified>>` 与 `<KeyRelease>` 绑同一处理器 → 每次输入触发两次；`SliderField`/`ColorField` 的 notify 是生效的，语义不一致 |
| L8 | `wm/ui/theme.py:34-50` | `ReleaseDC` 在 try 块内而非 `finally`，`GetDeviceCaps` 抛异常会漏掉一次 ReleaseDC（DC 句柄泄漏） |
| L9 | `wm/render.py:201` | 跨模块调用 `layout._render_block_bitmap`（下划线私有），封装边界被打破 |
| L10 | `wm/render.py:543` | 每页固定 `time.sleep(0.001)`，1000 页 PDF 凭空多 1s |
| L11 | `wm/lru.py:37` | `Budget = int` 类型别名未被使用，且语义错误（实际允许 `Callable[[], int]`） |
| L12 | `wm/spec.py:192-197` | `lines()` 只按 `\n` 切分，CRLF 的 `\r` 残留参与块宽计算 |
| L13 | `tests/run_all.py:104` | 仍在预导入已弃用的 `fitz` shim（与「全仓统一 `import pymupdf as fitz`」结论不符），而护栏用例的被测列表不含 `run_all.py` 自身 |
| L14 | `tests/test_dlp.py:33` | `MAGIC = dlp.DEFAULT_MAGIC` 在导入时固化，外部设 `WM_DLP_MAGIC` 会让整份用例全崩；`:429` 函数名 `prefers_lDDec` 拼写错误 |
| L15 | `main.py:104-115` | `runtime.log` 每次启动 `open(...,"w")` 清空、无进程锁，多实例并行会互相截断日志 |
| L16 | `main.py:345-346` | 未装 `sys.excepthook` / `threading.excepthook`，后台线程异常无 UI 痕迹 |
| L17 | `main.py:301` | argv 路径不存在被静默滤掉；`:312` `"--selftest" in argv` 用成员判断，`a.png --selftest` 会因 `i+1` 超界而丢弃全部待处理文件且无提示 |
| L18 | `packaging/deploy_lddec.py:103-111` | 只检查「是否有第二个 exe / DLL」，**不校验哈希或签名**，与该文件自己「需审计」的主张不匹配 |
| L19 | `README.md:26/244/277-279/391/419`、`docs/integration-lddec.md:20` | 文档多处与实现漂移：依赖清单漏 PyInstaller、复现示例仍写已弃用的 `import fitz`、体积与冷启动数据早于 dec.exe 入包、`WM_DLP_ENABLE` 少列 `false/no`、用例条数写成 11（实际 19） |
| L20 | `wm/dlp.py:311,708` | `finally` 只清理三个硬编码名字（`work`/`.dec_`/`_dec`），解密器产生的 `.dec`、`.tmp` 等永久留在明文目录 |

---

## 二、修复建议（按优先级）

### P0 — 交付前必须修（4 处「卡死/静默」级，均为局部小改）

> **✅ 四项已于 2026-09-26 全部修复**（未打包发版，按用户要求先只改代码）。
> 改动：`wm/ui/app.py`（关窗顺序、`_release_all_plain` 逐个兜错、`_poll_results` 单条结果
> 单独兜错 + `after` 移进 `finally`、`_batch_worker` 补投 done）、`wm/ui/batch.py`
> （`run_batch` 整段 `try/finally` 保证 `on_done` 必达）、`main.py`（`user_log_dir` 降级到
> `tempfile.gettempdir()`、`install_stdio` 移进 try）。
> 新增 `tests/test_robustness.py` 7 条专用回归（**修复前必挂、修复后必过**），
> 全量回归 **103/103**。S4 的第 4 小项「except 首句不依赖日志目录」未单独改：
> `user_log_dir()` 本身已吞掉所有 `OSError`，`except` 里再调它已是安全的，
> 额外的 try 属于冗余。

1. ~~**关窗顺序**（S1）~~— `app.py:941-943` 改为
   `self._cancel = True` → `_join_batch()` → `_release_all_plain()`，即**先等线程收尾再清资源**；并把 `_release_all_plain` 放进 `try/except`，保证一条删除失败不中断其余。
2. ~~**轮询链兜底**（S2）~~— `app.py:857-873` 把 `_handle_result(item)` 单独包一层
   `try/except Exception: log`，并让 `self._poll_job = root.after(...)` 移到 `finally`；同时把队列回调里的裸 `configure`（889-890、899-906）统一改用有销毁保护的 `_set_status`。
3. ~~**批处理必达 on_done**（S3）~~— 在 `run_batch`（`batch.py:61`）外层包 `try/finally`，
   保证任何路径都调一次 `on_done`；`app.py` 的 `_batch_worker` 再加一层兜底：异常时补投
   `{"kind":"done", failed:[...]}`，确保 `_busy` 一定复位。
4. ~~**崩溃兜底自加固**（S4）~~— `user_log_dir()` 的 `makedirs` 包 try，失败时降级到
   `tempfile.gettempdir()`；`install_stdio()` 移进 try；`main.py:324` 的 except 分支首句改为不依赖日志目录的写法（先尝试弹窗，再尝试写日志）。

### P1 — 强烈建议（正确性与资源）

5. **解密总预算 + 可取消 + 反馈**（S5、M5）— 给 `resolve()` 加「每文件总超时」参数（如 60s 上限，各通道按剩余预算分配），让 `WM_DLP_TIMEOUT` 对内置链也生效；`_resolve_sources` 每处理完一个文件就 `update()`（而非 `update_idletasks`）让用户能取消/关闭。
6. **明文可观测 + 启动 GC**（S6）— `release()` 删不掉时记一条 `[WARN]` 日志；启动时扫描 `%TEMP%/wm-dlp-*`，清理不属于本进程的残留目录（按 pid 文件或 mtime 阈值判定）。
7. **预览加尺寸上限与流式解码**（S7）— `render_overlay_layer` 套用与输出路径同样的
   `IMAGE_SS_MAX_PIXELS` 降倍率；`media.page_image` 增加「按目标尺寸」参数，预览侧先算
   `disp_w/disp_h` 再解码（PDF 用 dpi，图片用 `draft()`）。
8. **批处理启动即取消在飞预览**（S8）— `_start_batch` 里 `self._preview_gen += 1` 并清
   `_preview_pending`；`_render_preview_async` 入口补 `if self._busy: return`。
9. **字体回退不得静默**（S9）— `resolve()` 末档改用 `can_render_cjk` 筛选；`pil_font` 回退 `load_default` 时写一条状态栏/日志提示「未找到可用中文字体」。
10. **明文与缓存的安全边界**（M1、M2、M22）— PowerShell 改 `[IO.File]::WriteAllBytes`（天然截断）；把串行锁提到 `ChainProvider` 层（按「通道是否需要独占」抽象）；补 `tools/LDDec/dec.exe` 的入包断言（M23）+ 真实可执行性冒烟。
11. **`_resolve_sources` 加 per-file 兜底**（M3）— 复用 `resolve_all` 的 try/except；`_build_provider` 对 `shlex.split` 的 `ValueError` 就地降级为「无解密器」并给出可行动提示。
12. **明文有效性校验**（M8）— 读取前 `os.path.isfile` + 大小比对；预览失败区分「明文已失效」与「文件损坏」，并打日志。

### P2 — 应该修（一致性、体验、卫生）

13. M10（`tobytes` 移进 try 并在异常时 close）、M11（`assert` 改显式 `raise`，或打包去掉 `optimize=1`）、M12（禁用态在 `_value` 写入口拦截）、M13（拖拽改为单一注册点或确认 break 生效）、M14（翻页按钮纳入 `_set_busy`；done 后补跑被丢弃的预览）。
14. M9（保存失败清理 `dst`；元数据改成「逐个单独剔除定位真凶」，被剥离的项要提示）、M15（`MAX_TILES` 循环后加最终 clamp）、M17（字体缓存改用 `SizedLRU`）、M19（`to_float` 过滤 inf；`pdf_render_scale` 加 `math.isfinite`）、M20（渲染入口统一走 `normalize_color`）。
15. 文档与卫生：M21（README 路径脱敏为占位符 + 与打包脚本统一 Python 版本口径）、L19（逐项同步文档）、L2/L3（删 `ToggleSwitch`、`App.run()` 死代码）、L1（`KNOB` 重命名）、L18（deploy 脚本加哈希记录）、`.gitignore` 补 `*.exe` / `*.log`。
16. 供应链：为随包的 `dec.exe` 记录来源、SHA256、获取方式与审计结论（写一个 `tools/LDDec/README.md`），并同步修改 `docs/integration-lddec.md` 中「默认关闭/不投放到内网」这两条已与实现矛盾的红线 —— **按当前实现改写文档**，而不是反过来改代码。
17. 测试补齐：M22（补 `wm/lru.py`、`wm/fonts.py` 单测；把 `_smoke/verify_lru.py` 迁进 `tests/`）、L13（`run_all.py` 去掉 `fitz` 预导入）、L14（`MAGIC` 改为运行时读取）。

---

## 三、总体结论

**功能正确性：合格。** 核心链路（水印布局、PDF/图片渲染、批处理、解密三通道回退）逻辑正确，96/96 回归通过，exe 自检 4/4、冷启动 ~3s、单文件独立验收 PASS。「绝不改写原文件」这条硬契约经专项核查**成立**：交给解密器的一律是副本，明文落点随机命名且与副本前缀不同，`plan_output` 保证输出永不覆盖任何已存在文件，PDF 走 `.part` + `os.replace`。

**主要剩余风险（按暴露概率排序）：**

> **S1–S4 均已修复**（2026-09-26，`tests/test_robustness.py` 7 条锁住）。
> 以下按**修复前**的风险描述保留，用于说明当初为何定为 P0。

1. ~~**界面永久卡死**（S1/S2/S3）~~ —— 出错后用户只能杀进程，且已处理文件状态不明。触发条件都存在（关窗时批处理在跑、队列回调遇 TclError、批处理中任一回调抛异常），**不是理论风险**。
2. ~~**静默退出**（S4）~~ —— 日志目录不可写时双击无反应，恰好复现了本项目最想消灭的「闪一下就没了」。
3. **大文件峰值内存**（S7/S8）—— 超大图预览 + 批处理并发时可能被系统杀进程。常规尺寸（≤4000×4000）不受影响。
4. **交互冻结**（S5）—— 批量导入加密文件时最长可达分钟级无响应且不可取消（取决于本机是否真的要走解密通道）。
5. **合规与文档不一致**（S10）—— 解密默认启用与文档红线矛盾；随包二进制缺可追溯记录。此项为用户已决策接受的取舍，但**文档必须同步**，否则后续评审会按错误前提进行。

**是否达到可交付状态：建议修完 P0 四项后再交付。**
P0 四项都是局部小改（预计 30–60 行），风险极低，但直接决定「用户遇到异常时是看到提示还是只能杀进程」。修完 P0 后可以达到**内部可用**状态；P1 建议在下一个小版本（1.0.4）处理，其中第 5、7 项对内网真实加密环境的使用体验影响最大。若时间紧迫、只修一件事，优先修 **S2 + S3**（界面永久卡死）。

---

## 四、需要确认的信息（不猜测，请补充）

1. ~~**贵司 DLP 驱动对未授权进程的读取行为**~~ **✅ 已确认（2026-09-26，内网实机）**：
   `python -c "print(open(r'PLM验收单.pdf','rb').read(8))"` 返回
   `b'\x88}\x1c\xabk\x00\x04\x00'` —— 未授权进程**读得到字节、内容是密文**（行为 A：
   返回密文，不拒绝读）。当前 `dlp.is_encrypted`（`dlp.py:113-118`）的「读不到即
   `False`」假设与真实驱动行为一致，**判定顺序无需修改**，此项关闭。
2. **✅ 已确认（2026-09-26，内网实机 v1.0.3 exe）**：方式 A 全流程通过 —— 启动后
   状态栏显示三条通道均已启用，拖入加密文件能进列表、能预览、能批处理出成品。
   **端到端解密链路成立**。当初「具体哪条通道解的 / 单文件耗时」未知，现已备好
   两条取证路径（2026-09-27 补）：

   1. **设 `WM_DLP_TRACE=1` 再双击 exe** —— dlp 会把每个文件的每条通道
      「尝试 / 成功 / 失败 + 耗时」写进 `runtime-<pid>.log`，无需拷源码；
   2. **`python tools/probe_dlp_channels.py <加密文件>`** —— 只用标准库，
      强制逐通道单跑并给出「界面实际会走哪条」+ 冷/热两次耗时 + 原文件哈希前后
      比对。拷到内网最少带两个文件：它和 `wm/dlp.py` 放同一目录即可。

   *（顺带：本地用假加密样本跑探针时发现**三个通道都会把密文副本 `work` 永久
     留在临时目录** —— 它是在目录快照之后创建的，天然不在「待清理差集」里。
     已在三处 `finally` 补显式 `release(work)`，并加了 2 条残留断言锁住。）*

   **内网实测（2026-09-27，源码探针，`PLM验收单.pdf` 178.5 KB）**：

   | 通道 | 结果 | 耗时 |
   |---|---|---|
   | LDDec（dec.exe） | 未参与（该次只拷了 `dlp.py`，没带 dec.exe） | — |
   | **cmd 读取** | **成功**（产物 178,720 字节，头 `%PDF-1.6`） | **0.11~0.14 s** |
   | PowerShell 读取 | 产物不可用：仍是密文（182,816 字节 = 磁盘原始大小） | 0.45 s |

   **第二轮（同日，带 dec.exe，`WM_LDDEC_EXE` 指定）** —— 通道全部就位后的实测：

   | 通道 | 结果 | 单独耗时 |
   |---|---|---|
   | **LDDec（dec.exe）** | ✅ **成功**（178,720 字节，头 `%PDF-1.6`） | 0.39 s（链路内 0.15 s） |
   | cmd 读取 | ✅ 成功（同样 178,720 字节） | 0.11 s |
   | PowerShell 读取 | ❌ 仍是密文（182,816 字节 = 磁盘原始大小） | 0.20 s |

   → **定案**：实际由排在第一位的 **LDDec 解出，单文件 0.15~0.16 s**；原文件哈希前后
   一致（未被改写）；明文目录无残留。cmd 更快（0.11 s）但差 0.04 s，**保持 LDDec 优先**
   —— 它是面向该加密系统的专用工具，且已在本环境验证可用，cmd 继续当兜底。
   PowerShell 在这台机器拿不到明文，因排在第三、永不触发，保留无害。

   *（此前担心的「LDDec 若失败/卡住，每文件都要白等超时」不成立：它成功且是秒级。
     因此**不需要**调整通道顺序，也**不需要**做"连续失败熔断"。）*
3. **✅ 部分确认**：单文件版 exe 在 `_MEIPASS` 下**未被拦截** —— 状态栏显示
   LDDec 通道已启用，说明 `tools/LDDec/dec.exe` 被成功发现并随包解压；且即便
   dec.exe 被拦，cmd / PowerShell 两条通道会自动接管（已实测可用）。
   剩余未验证：`%TEMP%` 含非 ASCII 时 `_MEIPASS` 自身路径对 cmd 版 `type` 的影响
   （适配层只为**明文副本**选了 ASCII 根，`exe_dir` 不受控）。
4. **大文件的实际尺寸分布**：日常处理的图片/PDF 最大多大？决定是否必须现在做 S7 的流式解码（若普遍 ≤8MP，可降级为 P2）。
5. **PDF 口令加密（password-protected）的预期**：当前若遇到会走哪个失败分支、要不要单独提示「请输入密码」？现有代码未见处理。
6. **多显示器不同 DPI 的实际表现**：当前是 system-aware（非 per-monitor），跨屏拖动不会重算 `SCALE`。是否需要在本次交付范围内支持？
7. **`EXCLUDES` 排掉的 `ssl/hashlib/fontTools`**：当前源码确无引用（已 grep 验证），但依赖 PyMuPDF/Pillow 不引入懒加载。是否接受「冻结版在依赖小版本升级后有 ImportError 风险」这一取舍？

---

## 五、第二轮修复（P1 / P2 全量，2026-09-26）

用户指令「修复剩下所有的问题」后，§一 清单里 **S5–S10、M1–M23、L1–L20 全部落地**。
下表按编号给出「改了什么」—— 只记结论，代码内的「为什么」写在各自的 docstring 里。

### 严重（S5–S10）

| # | 修复要点 |
|---|---|
| S5 | `wm/dlp.py` 新增**每文件总预算**（`DEFAULT_BUDGET = 120s`，`WM_DLP_BUDGET` 可配）：`resolve()` 算出 `deadline`（monotonic 绝对时限）并贯穿 `decrypt(src, dst, deadline)`，各通道按剩余量裁自己的超时；`_resolve_sources` 每文件后 `root.update()`（真处理输入事件，可关窗）、进度显示「正在检查文件… i/n」 |
| S6 | `release()` / `release_all()` 失败与"删完仍存在"都写 `[WARN][dlp]`；`plaintext_dir()` 启动时跑一次**孤儿目录 GC**（`owner.pid` + `_pid_alive()` 判断别的实例是否还活着，宁可漏删不误删） |
| S7 | `wm/render.py` 新增 `PREVIEW_SS_MAX_PIXELS` 与 `preview_layer_scale()`：预览层按像素预算降倍率（`native_scale = min(1.0, …)`），与输出路径同一道闸 |
| S8 | `_start_batch` 先 bump `_preview_gen` 并清 `_preview_pending`，在飞的预览帧立即作废 |
| S9 | `fonts.resolve()` 末档改用 `can_render_cjk` 挑一个**真能排中文**的家族，实在没有才回退并 `_warn_once`；`pil_font` 回退 `load_default` 时同样告警 |
| S10 | `tools/LDDec/SHA256.txt` 记录来源 + SHA256；`deploy_lddec.py` 的 `deploy` 写清单、`check` 验清单；`.gitignore` 加 `*.exe` 并显式放行 `tools/LDDec/dec.exe`；`docs/integration-lddec.md` 的「随包分发的 `dec.exe`」一节按**当前实现**重写红线（cmd 版不含 Faker / 不冒充进程） |

### 中等（M1–M23）

| # | 修复要点 |
|---|---|
| M1 | PowerShell 通道改 `[IO.File]::WriteAllBytes`（天然截断），不再留上一文件的尾部 |
| M2 | 串行锁提到全局 `_call_lock`：**所有**外部解密调用（含 `WM_DLP_DECRYPT_CMD` 指向的 dec.exe）都排队 |
| M3 | `_split_command` 捕获 `shlex` 的 `ValueError` 返回 `[]`，由 `provider.problem()` 给出「多半是引号未闭合」的可行动提示；`_resolve_sources` 补 per-file try/except，一个文件炸了不连累整批 |
| M4 | `_ascii_temp_root()` 找不到纯 ASCII 根时 `_warn`，不再静默回落 |
| M5 | `WM_DLP_TIMEOUT` 对**内置链**同样生效（原先只在配了命令模板时有效） |
| M6 | `WM_DLP_MAGIC` 非法时 `_warn` 说明已回退默认魔数 |
| M7 | 明文**总量上限**（`WM_DLP_MAX_BYTES`，默认 4 GB），到顶即停并说明如何调；`release()` 现在会把额度**扣回去**（原先只增不减，删光也不恢复） |
| M8 | 新增 `dlp.plaintext_problem(path)`：批处理与预览在读取前校验存在性 / 0 字节 / 大小是否被改写，给出**可区分**的原因（"明文已失效，请重新添加" vs "文件为空"），不再混成一句「无法打开」 |
| M9 | `output.write_image` 先**逐项单独剔除**定位真凶（不再无条件先剥 exif），全剥离时告警，失败后 `_discard(dst)` 清掉 0 字节 / 半截文件 |
| M10 | EXIF 的 `tobytes()` 移进 try，异常时也能 close 源图（不再漏句柄拖垮 `dlp.release`） |
| M11 | 两处 `assert self._pdf is not None` 改为显式 `raise ValueError`（`-O` 下不会被剔除） |
| M12 | `SliderField.set()` 在 `not self._enabled` 时直接 return —— 在**写入口**拦截，不再只挡通知 |
| M13 | `_on_drop` 按 `data + 1 秒时间窗`去重；`_split_dropped()` 手写花括号感知解析兜底 |
| M14 | `_set_busy` 纳入 `prev_btn` / `next_btn`；done 分支补跑一帧预览（`_schedule_preview(0)`） |
| M15 | `compute_placements` 末尾 `_decimate()`：`MAX_TILES` 成为**硬上限**（此前 16 轮回退不收敛就直接返回，实测 21609 块） |
| M16 | 探针渲染走 `use_cache=False`，opacity-1.0 的探针不再占第二份缓存 |
| M17 | `_font_cache` / `_cjk_map` 改用 `SizedLRU`（带锁 + 真 LRU + 条目上限），不再「到 128 条整表清空」 |
| M18 | PDF 图层改 `fitz.Pixmap(csRGB, w, h, RGBA_bytes, True)` **直传**（MuPDF 存的是预乘 alpha，故先 `ImageChops.multiply` 预乘），不再 PNG 编解码往返 |
| M19 | `pdf_render_scale` 加 `math.isfinite` 挡 NaN；`spec.to_float` 也拒 ±inf |
| M20 | `_alpha_rgba` 与 `block_key` / `render_key` 统一走 `normalize_color`，`#D32F2F` 与 `#d32f2f` 不再各存一份 |
| M21 | README 的个人路径 `C:\Users\Shibeng\...\Python314` 全部换成 `<PY>` 占位符 + 统一 Python 3.13 口径 |
| M22 | 新增 `tests/test_core_units.py`（12 条：`SizedLRU` 真 LRU / 预算 / `min_keep` / 并发安全、fonts 缓存上限与回退、panel 销毁撤定时器）与 `tests/test_entrypoint.py`（9 条：`_split_argv`、`selftest()` 端到端 + 目录不可用必返回退出码、dec.exe 指纹 / PE / 自动发现 / 无参数会自行退出） |
| M23 | `build_onefile.py` 增加**字节级**入包断言（`mupdf` / `tkdnd` / `WM_DLP_DECRYPT_CMD` / `dec.exe`）；`build.py` 补齐 tkdnd 与 dec.exe 的存在性断言（原先只有 mupdf DLL） |

### 轻微（L1–L20）

| # | 修复要点 |
|---|---|
| L1 | `KNOB` 拆成 `KNOB_FILL`（颜色）/ `KNOB_DIAMETER`（`FlatScale`）/ `KNOB_RADIUS`（`_AngleDial`） |
| L2 | 删除无人实例化的 `ToggleSwitch`（~70 行）及其 `__all__` 条目 |
| L3 | 删除死代码 `App.run()`（缺图标 / argv 预载 / 拖拽降级三件事，误用会得到残缺入口） |
| L4 | `_friendly_save_error` 简化为 `isinstance(exc, OSError)`（`PermissionError` 是其子类） |
| L5 | `round_rect` 退化分支用 min/max，负尺寸矩形也能画出来 |
| L6 | `ScrolledFrame.destroy()` 先 `after_cancel(self._wheel_job)` |
| L7 | `TextArea` 只绑 `<KeyRelease>`（不再与 `<<Modified>>` 双重触发） |
| L8 | `enable_dpi_awareness` 的 `ReleaseDC` 移进 `finally` |
| L9 | `_render_block_bitmap` 改公开名 `render_block_bitmap`（不再跨模块调私有） |
| L10 | `time.sleep(0.001)` 改为每 32 页一次（1000 页不再凭空多 1 秒） |
| L11 | `Budget = Union[int, Callable[[], int]]`（原先写成裸 `int`，与实现不符） |
| L12 | `spec.lines()` 用 `splitlines()`，CRLF 的 `\r` 不再参与块宽计算 |
| L13 | `run_all.py` 预导入改 `pymupdf`（原先预热的是待废弃的 `fitz` shim） |
| L14 | `test_dlp.py` 的 `MAGIC` 改为运行时 `_magic()`；函数名 `prefers_lDDec` 拼写修正 |
| L15 | 运行日志改 `runtime-<pid>.log` 并只留最近 5 份（多实例不再互相截断） |
| L16 | 新增 `install_excepthooks()`：`threading.excepthook` 把后台线程异常写进 error.log |
| L17 | 新增 `_split_argv()`（按"第一个 `--` 选项"切分，不再因 `i+1` 越界丢文件）；`gui_main` 对不存在的路径打 `[WARN]` |
| L18 | `deploy_lddec.py` 新增 `sha256()` / `write_manifest()` / `verify_manifest()` |
| L19 | README / docs 逐项同步：补 PyInstaller 依赖、复现示例改 `import pymupdf as fitz`、体积与冷启动标注为早期基线、`WM_DLP_ENABLE` 补全 `false/no`、新增 `WM_DLP_BUDGET` / `WM_DLP_MAX_BYTES`、用例条数改为实数、`runtime.log` → `runtime-<pid>.log` |
| L20 | 解密产物清理改为**目录快照差分**（`_snapshot_dir` / `_cleanup_new_files`），不再只认 `.dec_` / `_dec` 三个硬编码名字 |

### 回归结果

全量 `tests/run_all.py`：**134 条，通过 134，跳过 0，失败 0**（10 个测试文件，退出码 0）。
新增的两份用例文件把此前**零覆盖**的 `wm/lru.py`、`wm/fonts.py`、`wm/ui/panel.py`、
`main.selftest()` 与随包 `dec.exe` 全部纳入门禁。

> ⚠️ **观测到一次偶发失败（值得记录）**：某轮全量跑里
> `test_stepper.py::test_stepper_always_drawn_with_three_hover_states` 挂了一次，
> 单独跑、按文件顺序在主线程跑、以及重跑全量**都通过**。成因是运行器把用例放在
> **子线程**里跑，而 Tcl 解释器绑定在创建它的线程 —— 与 AUDIT.md 里已记录的那类
> 「子线程里创建并销毁 Tk」同源。彻底方案仍是「每个用例跑在独立子进程」，
> 本次**不改运行器**（改动面大且不在本次修复清单内），仅在此留痕：
> **单条 Tk 像素用例偶发失败时，先单独复跑再判定**。

另外，本轮修订把 `wm/dlp.py`、`wm/layout.py`、`wm/render.py`、`wm/spec.py`、
`wm/ui/theme.py` 五个文件被编辑工具写成 CRLF 的行尾**统一改回 LF**（与仓库一致），
否则 diff 会显示成整文件重写（3001 行），完全掩盖真实改动。

> 打包 / 发版**始终未执行** —— 用户明确要求「只修代码，暂不打包发版」。
> 下次发版前应先跑一次 `packaging/build_onefile.py`（内含新的入包断言 + 自检 + GUI 冒烟）。
