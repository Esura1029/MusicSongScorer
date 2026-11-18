"""音乐APP自动评分机器人核心引擎

该模块封装了评分子机器人的核心逻辑，负责：
1. 协调各子模块的工作
2. 管理评分流程的状态机
3. 处理用户交互和进度反馈

【整体架构】
┌─────────────────────────────────────────────────────────────┐
│                      BotEngine                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    主循环                            │   │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌───────┐  │   │
│  │  │ PageRec │→ │ScoreEnt │→ │ SongScr │→ │循环   │  │   │
│  │  │ 页面识别 │  │入口管理 │  │ 评分执行 │  │继续   │  │   │
│  │  └─────────┘  └─────────┘  └─────────┘  └───────┘  │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
"""

import os
import glob
import time
import threading
from typing import Callable, Optional, Dict, Any

from core import (
    ADBTools,
    OCREngine,
    PageRecognizer,
    PageType,
    ScoreEntrance,
    SongScorer,
    ScreenSource,
)
from utils import load_config


class BotEngine:
    """评分机器人引擎

    协调所有子模块，按照状态机逻辑执行自动评分任务。
    """

    def __init__(self, custom_config: Optional[Dict[str, Any]] = None):
        """初始化机器人引擎

        Args:
            custom_config: 自定义配置，会覆盖默认配置
        """
        self._stop_event = threading.Event()
        self._custom_config = custom_config or {}
        self._songs_completed = 0
        self._last_submit_time = 0.0

        # 加载配置
        self._config = load_config()
        self._config.update(self._custom_config)

        # 各子模块（延迟初始化）
        self._adb: Optional[ADBTools] = None
        self._ocr: Optional[OCREngine] = None
        self._screens: Optional[ScreenSource] = None
        self._page_recognizer: Optional[PageRecognizer] = None
        self._score_entrance: Optional[ScoreEntrance] = None
        self._scorer: Optional[SongScorer] = None

        # 评分流程状态
        self._in_rating_flow = False

    def _init_modules(self) -> None:
        """初始化所有子模块"""
        self._adb = ADBTools(self._config["adb_path"], self._config["device_id"])
        self._ocr = OCREngine()
        self._adb.connect()

        # 所有模块共享同一个屏幕帧来源，共享 OCR 结果缓存
        self._screens = ScreenSource(self._ocr, self._adb)

        self._page_recognizer = PageRecognizer(self._ocr, self._adb, self._screens)
        self._score_entrance = ScoreEntrance(self._ocr, self._adb, self._screens)
        self._scorer = SongScorer(
            self._ocr,
            self._adb,
            self._config.get("min_star", 2),
            self._config.get("max_star", 4),
            self._screens,
        )

    def stop(self) -> None:
        """停止评分任务"""
        self._stop_event.set()

    def is_stopped(self) -> bool:
        """检查是否已停止"""
        return self._stop_event.is_set()

    @staticmethod
    def cleanup_screenshots() -> None:
        """清理截图缓存文件"""
        folder = "screenshots"
        if not os.path.exists(folder):
            return

        images = glob.glob(os.path.join(folder, "*.png"))
        if not images:
            return

        print(f"[INFO] 正在清理缓存，共发现 {len(images)} 张图片...")
        for img in images:
            try:
                os.remove(img)
            except Exception as e:
                print(f"[WARNING] 无法删除 {img}: {e}")
        print("[INFO] 截图清理完成。")

    def run(self, progress_callback: Optional[Callable] = None) -> None:
        """运行评分任务

        【主循环流程】
        1. 截取一帧屏幕并做一次 OCR
        2. 识别当前页面类型
        3. 根据页面类型执行相应操作
        4. 重复直到用户停止或任务完成

        Args:
            progress_callback: 进度回调函数，签名: (已完成数, 总数, 消息)
        """
        self._stop_event.clear()
        self._songs_completed = 0
        self._last_submit_time = 0.0

        # 初始化模块
        self._init_modules()

        print("[INFO] 正在识别当前页面...")
        consecutive_errors = 0
        unknown_streak = 0

        # 主循环
        while True:
            if self.is_stopped():
                print("[INFO] 用户停止了任务")
                return

            try:
                # 截取并识别当前页面（一帧只做一次 OCR）
                screen = self._screens.grab()
                if screen is None:
                    print("[WARNING] 截图失败，1秒后重试")
                    time.sleep(1)
                    continue

                page_info = self._page_recognizer.recognize_current_page(screen)
                page_type = page_info["type"]
                print(
                    f"[INFO] 识别页面: {page_type.value} "
                    f"(累计OCR {self._screens.ocr_count} 次)"
                )

                # 根据页面类型执行相应操作
                if page_type == PageType.ENTRY_PAGE:
                    unknown_streak = 0
                    self._handle_entry_page(page_info)

                elif page_type == PageType.RATING_POPUP:
                    unknown_streak = 0
                    self._handle_rating_popup(page_info, progress_callback)

                elif page_type == PageType.RATING_DIMENSION:
                    unknown_streak = 0
                    self._handle_rating_dimension(page_info, progress_callback)

                elif page_type == PageType.RATING_COMPLETE:
                    unknown_streak = 0
                    self._handle_rating_complete(page_info, progress_callback)

                elif page_type == PageType.MUSIC_PLAYING:
                    unknown_streak = 0
                    self._handle_music_playing(progress_callback)

                else:
                    # 未知页面：逐步延长等待时间，避免高频空转
                    unknown_streak += 1
                    wait = min(1.0 + unknown_streak, 5.0)
                    print(f"[INFO] 未知页面，尝试向上滑动... (第 {unknown_streak} 次)")
                    self._score_entrance.swipe_up(1)
                    time.sleep(wait)

                consecutive_errors = 0

            except Exception as e:
                consecutive_errors += 1
                print(f"[ERROR] 运行异常({consecutive_errors}/5): {e}")
                if consecutive_errors >= 5:
                    print("[ERROR] 连续异常达到上限，停止任务")
                    return
                time.sleep(2)

    def _handle_entry_page(self, page_info: Dict[str, Any]) -> None:
        """处理入口页面"""
        entry_info = page_info.get("info", {})
        entry_pos = entry_info.get("entry_pos")

        if not entry_pos:
            print("[WARNING] 未定位到评分入口坐标")
            time.sleep(1)
            return

        print("[INFO] 检测到评分入口页面，点击进入...")
        self._score_entrance.enter_rating(entry_pos, entry_info.get("entry_type", ""))
        time.sleep(2)
        self._in_rating_flow = True

    def _handle_rating_popup(
        self, page_info: Dict[str, Any], progress_callback: Optional[Callable]
    ) -> None:
        """处理评分弹窗"""
        print("[INFO] 检测到评分弹窗，执行评分...")
        self._run_rating_cycle(page_info, progress_callback)

    def _handle_rating_dimension(
        self, page_info: Dict[str, Any], progress_callback: Optional[Callable]
    ) -> None:
        """处理维度评分页面（评分流程中断后恢复）"""
        print("[INFO] 检测到评分维度页面，继续评分...")
        self._run_rating_cycle(page_info, progress_callback)

    def _run_rating_cycle(
        self, page_info: Dict[str, Any], progress_callback: Optional[Callable]
    ) -> None:
        """执行完整评分周期并更新进度"""
        rating_result = self._scorer.full_rating_cycle(
            page_info, progress_callback, screen=page_info.get("screen")
        )
        if rating_result["submitted"]:
            self._register_song(progress_callback)
        else:
            print("[WARNING] 评分未提交，退出")
            self._stop_event.set()

    def _handle_rating_complete(
        self, page_info: Dict[str, Any], progress_callback: Optional[Callable]
    ) -> None:
        """处理评分完成页面（提交按钮已高亮）

        刚提交完时页面可能还没跳转，此时不做重复点击，避免重复计数。
        """
        if time.time() - self._last_submit_time < 3:
            time.sleep(1)
            return

        print("[INFO] 检测到评分完成页面，点击提交...")
        if self._scorer.submit_rating(page_info.get("screen")):
            self._register_song(progress_callback)
        else:
            print("[WARNING] 未找到提交按钮，等待页面变化...")
            time.sleep(1)

    def _handle_music_playing(self, progress_callback: Optional[Callable]) -> None:
        """处理听歌中状态

        听歌页面的进度条/歌词会持续变化，画面哈希去重无法命中，
        因此这里固定间隔轮询，避免每秒都跑一次 OCR。
        """
        if not self._in_rating_flow:
            print("[INFO] 正在听歌，等待评分弹窗...")
            if progress_callback:
                progress_callback(self._songs_completed, 0, "听歌中...")
        time.sleep(7)

    def _register_song(self, progress_callback: Optional[Callable]) -> None:
        """记录一首完成评分的歌曲"""
        self._songs_completed += 1
        self._last_submit_time = time.time()
        self._in_rating_flow = False
        print(f"[INFO] 评分完成，累计 {self._songs_completed} 首，等待下一首")
        if progress_callback:
            progress_callback(
                self._songs_completed, 0, f"已完成 {self._songs_completed} 首"
            )
        time.sleep(2)


def run_task(
    progress_callback: Optional[Callable] = None,
    custom_config: Optional[Dict[str, Any]] = None,
) -> None:
    """运行评分任务的便捷函数

    Args:
        progress_callback: 进度回调函数
        custom_config: 自定义配置
    """
    engine = BotEngine(custom_config)
    engine.run(progress_callback)


def stop_task() -> None:
    """停止当前运行的评分任务（需要通过全局方式调用）"""
    global _engine
    if "_engine" in globals() and _engine:
        _engine.stop()


# 全局引擎实例（用于stop_task函数）
_engine: Optional[BotEngine] = None


def _set_global_engine(engine: BotEngine) -> None:
    global _engine
    _engine = engine
