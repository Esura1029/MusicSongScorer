# 音乐APP自动评分助手

基于 ADB 和 OCR 技术的音乐APP自动评分工具，支持自动识别评分入口、执行维度评分、提交评分等全流程自动化操作。

## 项目结构

```
CloudMusicCopilot/
├── core/                      # 核心模块
│   ├── __init__.py           # 模块导出
│   ├── adb_tools.py          # ADB 设备交互工具
│   ├── bot_engine.py         # 评分机器人核心引擎
│   ├── ocr_engine.py         # PaddleOCR 文字识别引擎
│   ├── page_recognizer.py    # 页面识别器（状态检测）
│   ├── score_entrance.py     # 评分入口管理器
│   ├── screen.py             # 屏幕帧模型（截图 + OCR 结果缓存）
│   └── song_scorer.py        # 歌曲评分执行器
├── utils/                     # 工具模块
│   ├── __init__.py
│   └── config.py             # 配置管理
├── screenshots/              # 截图缓存目录
├── gui_main.py               # GUI 主界面
├── main_bot.py               # 命令行入口
├── debug_click.py            # 坐标调试工具
└── config.json               # 配置文件
```

## 核心模块说明

### 1. 评分入口管理器 (`core/score_entrance.py`)

负责检测并进入音乐APP的评分入口。

**入口类型：**
- `评定今日歌曲` - 每日首次评分的入口
- `继续评定` - 中断了评分后继续的入口
- `每日听歌` - 日常听歌评分的入口

**工作流程：**
1. 在当前页面查找评分入口按钮
2. 如果未找到，向上滑动页面继续查找
3. 找到后点击进入评分流程

### 2. 歌曲评分执行器 (`core/song_scorer.py`)

完成音乐APP评分流程中的所有评分操作。

**评分分为两个阶段：**

#### 阶段1：总评分弹窗
```
┌─────────────────────────────────────────────────┐
│           🎵 请评定这首歌曲                       │
│           ★ ★ ★ ☆ ☆                            │
│           [2星] [3星] [4星] [5星]                 │
└─────────────────────────────────────────────────┘
```

#### 阶段2：维度评分页面
```
┌─────────────────────────────────────────────────┐
│  旋律: ★ ★ ★ ☆ ☆  [点击选择星级]                │
│  演唱: ★ ★ ★ ★ ☆                              │
│  歌词: ★ ★ ☆ ☆ ☆                              │
│                                                │
│           [提交并评下一首]                       │
└─────────────────────────────────────────────────┘
```

**评分维度：**
- 旋律（曲调）
- 演唱（歌声）
- 歌词（词内容）

### 3. 页面识别器 (`core/page_recognizer.py`)

使用 OCR 技术识别当前页面类型：

| 页面类型 | 特征文字 | 说明 |
|---------|---------|------|
| `ENTRY_PAGE` | 评定今日歌曲/继续评定/每日听歌 | 评分入口页面 |
| `RATING_POPUP` | 请评定 | 总评分弹窗 |
| `RATING_DIMENSION` | 旋律/演唱/歌词 | 维度评分页面 |
| `RATING_COMPLETE` | 提交并评下一首 | 评分完成页面 |
| `MUSIC_PLAYING` | 听歌/播放/播放中 | 听歌中页面 |
| `UNKNOWN` | 无 | 未知页面 |

### 4. ADB 工具 (`core/adb_tools.py`)

封装与 Android 模拟器通过 ADB 协议进行交互的操作：
- `connect()` - 连接设备
- `tap(x, y)` - 点击指定坐标
- `swipe(x1, y1, x2, y2, duration)` - 滑动操作
- `take_screenshot(filename)` - 截取屏幕（文件方式，供调试工具使用）
- `capture_png()` - 直接返回 PNG 字节流（默认走 `exec-out`，失败自动回退）

### 5. OCR 引擎 (`core/ocr_engine.py`) 与屏幕帧 (`core/screen.py`)

基于 PaddleOCR 的文字识别引擎，提供：
- `detect(image)` - 对一帧画面执行一次 OCR，返回全部识别结果
- `find_text(img_path, target)` - 单次查询（每次调用都会执行 OCR）
- `find_all_text(img_path)` - 单次查询全部文字

`ScreenSource` / `Screen` 是所有上层的共享屏幕缓存：

- 一帧画面只执行 **一次** OCR，`search/find/find_all` 全部在内存结果上完成
- 截图使用 `adb exec-out screencap -p` 直接获取 PNG 字节，省掉 `pull` 和重复编解码
- OCR 输入沿用"文件路径"方式（写入一个复用的 `screenshots/frame.png`），
  避免 PaddlePaddle 3.x + oneDNN 对数组输入/扩展参数的兼容性问题
- 画面未变化（PNG 字节 MD5 相同）时复用上一帧结果，跳过 OCR
- 页面识别、入口查找、评分定位共用同一个 `Screen`，不再重复推理

> 优化前，识别一次评分页面会触发 10 次以上整图 OCR（一个完整评分周期约 26 次）；
> 优化后每帧 1 次、一个评分周期约 3 次，CPU 占用大幅下降。

## 安装依赖

```bash
pip install paddlepaddle==2.6.2 paddleocr==2.7.3 numpy==1.26.4 pandas==2.3.3 customtkinter pillow
```

> **NumPy / OpenCV 版本注意**
> PaddleOCR 2.7.3 依赖 `opencv-python/opencv-contrib-python <= 4.6.0.66`，
> 这些 OpenCV 轮子是按 NumPy 1.x 的 ABI 编译的。若环境里是 NumPy 2.x，
> `import cv2` 会报 `numpy.core.multiarray failed to import`，导入阶段就会失败。
> 需要固定 `numpy==1.26.4`。
>
> 同一环境里只保留**一对同版本**的 OpenCV（`opencv-python==4.6.0.66` +
> `opencv-contrib-python==4.6.0.66`）。不要再安装 `opencv-python-headless`
> 或其它版本的 OpenCV：它们共用同一个 `cv2/` 目录，混装后卸载任意一个都会
> 把其余的安装一起破坏（表现为 `cv2` 能导入但缺少 `INTER_NEAREST` 等属性）。
> 若已混装，先全部卸载、删除残留的 `site-packages/cv2` 目录，再重新安装。

> **PaddlePaddle 版本注意**
> 本项目实机验证的稳定组合是 `paddlepaddle==2.6.2` + `paddleocr==2.7.3`。
> 不建议升级到 PaddleOCR 3.x + PaddlePaddle 3.3.x：后者的 oneDNN 执行器存在
> `ConvertPirAttribute2RuntimeAttribute not support` 缺陷，CPU 上 OCR 会直接崩溃。
>
> 程序同时兼容 PaddleOCR 2.x / 3.x 的接口与结果格式；万一运行在 3.3.x 上，
> 会自动关闭 MKL-DNN 后重试（功能可用，但 CPU 识别会明显变慢）。

## 配置说明

编辑 `config.json` 配置文件：

```json
{
    "adb_path": "D:\\Software\\MuMu Player 12\\shell\\adb.exe",
    "device_id": "127.0.0.1:16384",
    "min_star": 2,
    "max_star": 4,
    "wait_music_seconds": 18
}
```

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| `adb_path` | ADB 可执行文件路径 | - |
| `device_id` | 模拟器设备 ID | 127.0.0.1:16384 |
| `min_star` | 最低评分星级 | 2 |
| `max_star` | 最高评分星级 | 4 |
| `wait_music_seconds` | 听歌等待时间（秒） | 18 |

## 使用方法

### GUI 模式（推荐）

```bash
python gui_main.py
```

启动后可以：
- 配置评分星级范围
- 一键开始/停止自动评分
- 查看实时运行日志
- 跟踪评分进度

### 命令行模式

```bash
python main_bot.py
```

### 调试工具

```bash
python debug_click.py
```

用于调试 OCR 识别和点击坐标。

## 工作原理

### 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      BotEngine (机器人引擎)                  │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    主循环                             │   │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌───────┐  │   │
│  │  │ PageRec │→ │ScoreEnt │→ │ SongScr │→ │ 循环  │  │   │
│  │  │ 页面识别 │  │ 入口管理 │  │ 评分执行 │  │ 继续  │  │   │
│  │  └─────────┘  └─────────┘  └─────────┘  └───────┘  │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 状态机流程

```
                    ┌──────────────┐
                    │  入口页面    │
                    └──────┬───────┘
                           │ 点击进入
                           ▼
                    ┌──────────────┐
          ┌────────→│  评分弹窗    │←───────┐
          │         └──────┬───────┘        │
          │                │ 评分           │
          │                ▼                │
          │         ┌──────────────┐        │
          │         │  维度页面    │────────┘
          │         └──────┬───────┘
          │                │ 提交
          │                ▼
          │         ┌──────────────┐
          │         │  听歌中      │
          │         └──────┬───────┘
          │                │ 等待
          └────────────────┘
```

## 注意事项

1. **分辨率要求**：推荐使用 720*1280 分辨率的模拟器
2. **ADB 连接**：确保模拟器的 ADB 端口已正确配置
3. **APP版本**：适用于国内版音乐APP
4. **评分范围**：建议设置 2-4 星，避免被检测

## 技术栈

- **OCR**: PaddleOCR (中文识别)
- **GUI**: CustomTkinter
- **ADB**: Android Debug Bridge
- **语言**: Python 3.8+

## License

MIT License
