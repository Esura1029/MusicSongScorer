from enum import Enum
from typing import Dict, Optional, Any, Tuple

from .ocr_engine import OCREngine
from .adb_tools import ADBTools
from .screen import Screen, ScreenSource


class PageType(Enum):
    """页面类型枚举

    定义音乐APP应用中可能出现的各种页面状态。
    这些状态用于状态机的状态转换判断。
    """

    UNKNOWN = "unknown"  # 未知页面
    ENTRY_PAGE = "entry_page"  # 评分入口页面（显示"评定今日歌曲"等按钮）
    MUSIC_PLAYING = "music_playing"  # 听歌中页面
    RATING_POPUP = "rating_popup"  # 评分弹窗页面（"请评定"）
    RATING_DIMENSION = "rating_dimension"  # 评分维度页面（旋律/演唱/歌词）
    RATING_COMPLETE = "rating_complete"  # 评分完成页面（显示"提交"按钮）


class PageRecognizer:
    """页面识别器

    负责识别当前屏幕属于哪种页面类型，并提取页面中的关键信息。
    使用OCR技术检测页面上的关键文字来判断页面状态。

    页面识别流程：
    1. 截取当前屏幕（一帧只做一次 OCR）
    2. 依次检测各类页面的特征文字
    3. 返回页面类型及提取到的相关信息

    【性能要点】
    识别过程中所有文字查询都走 `Screen` 的内存缓存，
    不再对同一张截图重复调用 PaddleOCR。
    """

    RATING_TRIGGER_TEXT = "请评定"  # 评分弹窗的特征文字
    SONG_DIMENSIONS = ["旋律", "演唱", "歌词"]  # 维度页面会出现的三个维度
    DIMENSION_Y_SPACING = 80  # 维度之间的Y轴间距（像素）
    DIMENSION_Y_TOLERANCE = 15  # Y轴间距允许的误差（像素）
    SUBMIT_TEXT = "提交并评下一首"  # 提交按钮文字
    ENTRY_BUTTONS = ["评定今日歌曲", "继续评定"]  # 入口按钮可能出现的文字
    PLAYING_INDICATORS = ["聆听"]  # 听歌中页面的特征文字
    SUBMIT_BUTTON_BRIGHTNESS_THRESHOLD = 100  # 按钮高亮阈值（0-255）

    def __init__(
        self,
        ocr: OCREngine,
        adb: ADBTools,
        screens: Optional[ScreenSource] = None,
    ):
        """初始化页面识别器

        Args:
            ocr: OCR引擎实例，用于文字识别
            adb: ADB工具实例，用于屏幕截图
            screens: 共享的屏幕帧来源；不传则内部新建
        """
        self.ocr = ocr
        self.adb = adb
        self.screens = screens or ScreenSource(ocr, adb)

    def grab(self, force: bool = False) -> Optional[Screen]:
        """截取一帧（画面未变化时会复用上一帧的识别结果）"""
        return self.screens.grab(force=force)

    def recognize_current_page(
        self, screen: Optional[Screen] = None
    ) -> Dict[str, Any]:
        """识别当前页面类型并提取相关信息

        这是页面识别的核心方法，会：
        1. 确保有截图
        2. 检测页面类型
        3. 提取该页面相关的关键信息

        Args:
            screen: 已有的屏幕帧，如果为None则自动截取

        Returns:
            包含以下键的字典：
            - type: PageType枚举值，表示页面类型
            - info: dict，页面特有的信息
            - screen: Screen对象，后续操作复用同一份 OCR 结果
        """
        if screen is None:
            screen = self.screens.grab()

        if screen is None:
            return {"type": PageType.UNKNOWN, "info": {}, "screen": None}

        page_type = self._detect_page_type(screen)
        info = self._collect_page_info(screen, page_type)

        return {"type": page_type, "info": info, "screen": screen}

    def _detect_page_type(self, screen: Screen) -> PageType:
        """检测页面类型

        按照优先级依次检测各类页面：
        1. 评分弹窗 - 优先级最高，用户正在操作
        2. 评分完成 - 提交按钮高亮（可点击）
        3. 评分维度 - 评分流程中的页面（提交按钮灰色 / 使用Y轴间距验证）
        4. 听歌中 - 正常播放状态
        5. 入口页面 - 可以开始评分的入口
        6. 未知页面 - 未能识别

        Args:
            screen: 屏幕帧（已包含 OCR 结果）

        Returns:
            识别到的页面类型
        """
        if screen.contains(self.RATING_TRIGGER_TEXT):
            return PageType.RATING_POPUP

        # 提交按钮的分支必须先于维度验证：
        # 完成页上维度文字仍然存在，如果先验证维度就永远识别不到"可提交"状态。
        # 只有按钮高亮（可点击）时才判定为评分完成。
        submit_pos = screen.find(self.SUBMIT_TEXT)
        if submit_pos:
            if self._is_submit_button_active(screen, submit_pos):
                return PageType.RATING_COMPLETE
            return PageType.RATING_DIMENSION

        # 使用Y轴间距验证来检测维度页面，避免误识别
        if self._validate_dimensions(screen):
            return PageType.RATING_DIMENSION

        for indicator in self.PLAYING_INDICATORS:
            if screen.contains(indicator):
                return PageType.MUSIC_PLAYING

        if screen.find_any(self.ENTRY_BUTTONS):
            return PageType.ENTRY_PAGE

        return PageType.UNKNOWN

    def _validate_dimensions(self, screen: Screen) -> bool:
        """验证截图中是否存在真正的评分维度

        # 【验证逻辑】
        # 真正的评分维度页面中，"旋律"、"演唱"、"歌词"三个维度
        # 在Y轴上的间距是固定的（约80像素）。
        # - 识别到 2 个以上维度：必须存在符合间距规律的相邻两行；
        # - 只识别到 1 个维度：要求画面中同时存在提交按钮作为佐证，
        #   避免把歌词正文里的"歌词"二字误判为维度页面。

        Args:
            screen: 屏幕帧

        Returns:
            True 如果验证通过（是真正的维度页面），否则 False
        """
        dim_positions = {}
        for dim in self.SONG_DIMENSIONS:
            pos = screen.find(dim)
            if pos:
                dim_positions[dim] = pos

        if not dim_positions:
            return False

        if len(dim_positions) == 1:
            return screen.find(self.SUBMIT_TEXT) is not None

        y_values = sorted(pos[1] for pos in dim_positions.values())
        for i in range(len(y_values) - 1):
            gap = y_values[i + 1] - y_values[i]
            if abs(gap - self.DIMENSION_Y_SPACING) <= self.DIMENSION_Y_TOLERANCE:
                return True
        return False

    def _is_submit_button_active(
        self, screen: Screen, submit_pos: Tuple[int, int]
    ) -> bool:
        """检测提交按钮是否高亮（可点击状态）

        通过采样按钮区域的像素颜色来判断按钮是否处于激活状态。
        灰色按钮（不可点击）与亮色按钮（可点击）的像素亮度有明显差异。

        Args:
            screen: 屏幕帧
            submit_pos: 已定位到的提交按钮文字中心坐标

        Returns:
            True 如果按钮高亮（可点击），False 如果按钮是灰色（不可点击）
        """
        bx, by = submit_pos
        btn_width = 150
        btn_height = 40

        sample_points = [
            (bx - btn_width // 2, by),  # 按钮中心
            (bx - btn_width // 3, by),  # 偏左
            (bx + btn_width // 3, by),  # 偏右
            (bx - btn_width // 2, by + btn_height // 3),  # 下方偏左
            (bx + btn_width // 3, by + btn_height // 3),  # 下方偏右
        ]

        total_brightness = 0
        sample_count = 0

        for sx, sy in sample_points:
            brightness = screen.brightness(max(0, sx), max(0, sy))
            if brightness is not None:
                total_brightness += brightness
                sample_count += 1

        if sample_count == 0:
            return False

        avg_brightness = total_brightness // sample_count
        print(
            f"[DEBUG] 提交按钮平均亮度: {avg_brightness} (阈值: {self.SUBMIT_BUTTON_BRIGHTNESS_THRESHOLD})"
        )

        return avg_brightness > self.SUBMIT_BUTTON_BRIGHTNESS_THRESHOLD

    def _collect_page_info(
        self, screen: Screen, page_type: PageType
    ) -> Dict[str, Any]:
        """根据页面类型收集该页面的关键信息

        不同页面需要提取不同类型的信息：
        - 评分弹窗：需要获取"请评定"文字的位置
        - 维度页面：需要获取各维度文字的位置
        - 入口页面：需要获取入口按钮的位置和类型

        Args:
            screen: 屏幕帧
            page_type: 已识别的页面类型

        Returns:
            包含页面特有信息的字典
        """
        info = {}

        if page_type == PageType.RATING_POPUP:
            pos = screen.find(self.RATING_TRIGGER_TEXT)
            if pos:
                info["rating_trigger_pos"] = pos

        elif page_type == PageType.RATING_DIMENSION:
            dims = []
            for dim in self.SONG_DIMENSIONS:
                pos = screen.find(dim)
                if pos:
                    dims.append({"name": dim, "pos": pos})
            info["dimensions"] = dims
            submit_pos = screen.find(self.SUBMIT_TEXT)
            if submit_pos:
                info["submit_pos"] = submit_pos

        elif page_type == PageType.RATING_COMPLETE:
            pos = screen.find(self.SUBMIT_TEXT)
            if pos:
                info["submit_pos"] = pos

        elif page_type == PageType.ENTRY_PAGE:
            line = screen.find_any(self.ENTRY_BUTTONS)
            if line:
                info["entry_pos"] = line.center
                info["entry_type"] = line.text

        return info

    def is_rating_page(self, screen: Optional[Screen] = None) -> bool:
        """判断当前是否在评分相关页面

        Args:
            screen: 屏幕帧，如果为None则自动截取

        Returns:
            是否在评分页面（弹窗/维度/完成）
        """
        page_info = self.recognize_current_page(screen)
        return page_info["type"] in [
            PageType.RATING_POPUP,
            PageType.RATING_DIMENSION,
            PageType.RATING_COMPLETE,
        ]

    def is_entry_page(self, screen: Optional[Screen] = None) -> bool:
        """判断当前是否为评分入口页面

        Args:
            screen: 屏幕帧，如果为None则自动截取

        Returns:
            是否为入口页面
        """
        page_info = self.recognize_current_page(screen)
        return page_info["type"] == PageType.ENTRY_PAGE

    def is_music_playing(self, screen: Optional[Screen] = None) -> bool:
        """判断当前是否在听歌中

        Args:
            screen: 屏幕帧，如果为None则自动截取

        Returns:
            是否在听歌中
        """
        page_info = self.recognize_current_page(screen)
        return page_info["type"] == PageType.MUSIC_PLAYING
