import time
import random
from typing import Dict, Optional, Any, Callable, Tuple

from .ocr_engine import OCREngine
from .adb_tools import ADBTools
from .screen import Screen, ScreenSource


class SongScorer:
    """歌曲评分执行器

    职责：完成音乐APP评分流程中的所有评分操作。

    【评分流程说明】
    音乐APP的评分分为两个阶段：

    ┌─────────────────────────────────────────────────────────┐
    │ 阶段1: 总评分弹窗                                        │
    │  ┌─────────────────────────────────────────────────┐   │
    │  │           🎵 请评定这首歌曲                       │   │
    │  │           ★ ★ ★ ☆ ☆                            │   │
    │  │           [2星] [3星] [4星] [5星]                 │   │
    │  └─────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────┘
                            ↓ (点击星级后自动进入)
    ┌─────────────────────────────────────────────────────────┐
    │ 阶段2: 维度评分页面                                      │
    │  ┌─────────────────────────────────────────────────┐   │
    │  │  旋律: ★ ★ ★ ☆ ☆  [点击选择星级]                │   │
    │  │  演唱: ★ ★ ★ ★ ☆                              │   │
    │  │  歌词: ★ ★ ☆ ☆ ☆                              │   │
    │  │                                                │   │
    │  │           [提交并评下一首]                       │   │
    │  └─────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────┘

    【星级评分机制】
    - 星级范围: 1-5星
    - 可配置范围: min_star 到 max_star（如2-4星）
    - 每个维度随机选择配置范围内的星级

    【性能要点】
    所有文字定位都复用 `Screen` 的 OCR 结果，
    一个评分周期只需要 2~3 次 OCR，而不是此前的二十多次。
    """

    # 评分维度列表：旋律（曲调）、演唱（歌声）、歌词（词内容）
    DIMENSIONS = ["旋律", "演唱", "歌词"]

    # 维度之间的Y轴间距（像素）和允许误差
    DIMENSION_Y_SPACING = 80
    DIMENSION_Y_TOLERANCE = 15

    # 维度评分星级权重：2、3、4星权重相近，5星权重最低
    DIMENSION_STAR_WEIGHTS = {2: 30, 3: 33, 4: 30, 5: 7}

    # 【重要】星级位置配置
    #
    # 【阶段1 - 弹窗中的星级位置】
    # 弹窗中"请评定"文字下方会显示星级评分选项。
    # 这些offset是相对于"请评定"文字位置的偏移量。
    # 由于星级是横向排列的，所以只需要调整X坐标。
    #
    #  offset: X轴偏移量（正值表示向右移动）
    #  注意：这里用负数表示星级选项在文字左侧
    STAR_POSITIONS_POPUP = {
        2: -100,  # 2星：向左偏移100像素
        3: -20,  # 3星：向左偏移20像素
        4: 70,  # 4星：向右偏移70像素
        5: 150,  # 5星：向右偏移150像素
    }

    # 【阶段2 - 维度页面中的星级位置】
    # 维度页面中，每个维度文字（如"旋律"）右侧会有5颗星星，
    # 需要点击对应的星星位置来选择星级。
    # 这些offset是相对于维度文字位置的X轴偏移量。
    #
    #  界面布局示意（以"旋律"维度为例）：
    #  ┌──────────────────────────────────────────────────┐
    #  │  旋律: ★ ★ ★ ★ ★                             │
    #  │  ↑      ↑  ↑  ↑  ↑                               │
    #  │ 文字   1星 2星 3星 4星 5星                        │
    #  │       │   │   │   │                              │
    #  │       └───┴───┴───┴───→ X轴偏移量                │
    #  └──────────────────────────────────────────────────┘
    #
    #  实际坐标 = 维度文字X坐标 + offset
    STAR_POSITIONS_DIMENSION = {
        2: 315,  # 点击第2颗星
        3: 385,  # 点击第3颗星
        4: 455,  # 点击第4颗星
        5: 525,  # 点击第5颗星
    }

    def __init__(
        self,
        ocr: OCREngine,
        adb: ADBTools,
        min_star: int = 2,
        max_star: int = 5,
        screens: Optional[ScreenSource] = None,
    ):
        """初始化评分器

        Args:
            ocr: OCR引擎实例，用于识别界面文字和位置
            adb: ADB工具实例，用于执行点击操作
            min_star: 最低评分星级（默认2星）
            max_star: 最高评分星级（默认5星）
            screens: 共享的屏幕帧来源；不传则内部新建
        """
        self.ocr = ocr
        self.adb = adb
        self.min_star = min_star
        self.max_star = max_star
        self.screens = screens or ScreenSource(ocr, adb)

    def _validate_dimension_layout(
        self, positions: Dict[str, Tuple[int, int]]
    ) -> bool:
        """验证识别到的维度坐标是否符合真实的维度间距规律

        【验证逻辑】
        真正的评分维度页面上，"旋律"、"演唱"、"歌词"三个维度
        在Y轴上的间距固定约80像素。识别到两个以上维度时，
        至少需要有一对相邻维度的间距符合该规律。
        只识别到一个维度时无法校验间距，交给调用方处理。

        Args:
            positions: 维度名称到坐标的映射

        Returns:
            True 如果布局可信，否则 False
        """
        if not positions:
            return False

        if len(positions) < 2:
            return True

        y_values = sorted(pos[1] for pos in positions.values())
        for i in range(len(y_values) - 1):
            gap = y_values[i + 1] - y_values[i]
            if abs(gap - self.DIMENSION_Y_SPACING) <= self.DIMENSION_Y_TOLERANCE:
                return True
        return False

    def rate_overall(
        self,
        rating_pos: Optional[tuple] = None,
        screen: Optional[Screen] = None,
    ) -> int:
        """执行阶段1：总评分（弹窗中的星级选择）

        【工作原理】
        1. 定位"请评定"文字的位置
        2. 根据随机生成的星级，计算点击位置
        3. 点击对应位置完成星级选择

        【点击坐标计算】
        点击位置 = (文字X坐标 + X轴偏移量, 文字Y坐标 + 100)
        - X轴偏移量：决定点击哪颗星
        - Y轴偏移量(+100)：因为星级在文字下方约100像素处

        Args:
            rating_pos: "请评定"文字的坐标 (x, y)，如果为None则自动查找
            screen: 当前屏幕帧，用于自动查找文字位置

        Returns:
            选择的星级数（2-5）
        """
        star = random.randint(self.min_star, self.max_star)
        offset = self.STAR_POSITIONS_POPUP.get(star, 70)

        if rating_pos is None:
            if screen is None:
                screen = self.screens.grab()
            if screen is not None:
                rating_pos = screen.find("请评定")

        if rating_pos:
            self.adb.tap(rating_pos[0] + offset, rating_pos[1] + 100)
        else:
            print("[WARNING] 未找到'请评定'文字，跳过总评分")

        return star

    def rate_dimensions(self) -> Dict[str, Optional[int]]:
        """执行阶段2：维度评分（旋律/演唱/歌词）

        【工作原理】
        1. 向上滑动屏幕，展开维度评分区域
        2. 截取一帧并对三个维度做一次统一 OCR
        3. 根据随机生成的星级，计算并点击对应位置
        4. 每个维度独立评分

        【坐标计算详解】
        对于每个维度（如"旋律"）：
        1. OCR识别返回文字中心坐标 (dim_x, dim_y)
        2. 星级选项在该文字右侧水平排列
        3. 点击位置 = (dim_x + 星位偏移量, dim_y)

        Returns:
            包含各维度评分的字典：
            {
                "旋律": 3,   # 3星
                "演唱": 4,   # 4星
                "歌词": 2    # 2星
            }
            未识别到的维度值为 None
        """
        results: Dict[str, Optional[int]] = {dim: None for dim in self.DIMENSIONS}

        # 【关键步骤】滑动到维度评分区域
        # 维度区域初始不在可见范围，需要从当前位置向上滑动
        # 起始点 (360, 1000)：屏幕中下方
        # 结束点 (360, 250)：屏幕中上方
        # 持续时间 500ms：自然的滑动速度
        self.adb.swipe(360, 1000, 360, 250, 500)
        time.sleep(1)

        # 截取滑动后的屏幕：整帧只做一次 OCR
        screen = self.screens.grab(force=True)
        if screen is None:
            print("[WARNING] 截图失败，跳过维度评分")
            return results

        # 一次性定位所有维度
        positions = {}
        for dim in self.DIMENSIONS:
            pos = screen.find(dim)
            if pos:
                positions[dim] = pos

        # 【布局验证】避免把歌词正文里的"歌词"误认为评分维度
        if not self._validate_dimension_layout(positions):
            print("[WARNING] 维度布局验证失败，可能是误识别")
            return results

        # 遍历每个维度进行评分
        for dim, dim_pos in positions.items():
            # 【加权随机星级选择】
            # 维度评分固定为2-5星，5星权重最低，2/3/4星权重相近
            stars = list(self.DIMENSION_STAR_WEIGHTS.keys())
            weights = list(self.DIMENSION_STAR_WEIGHTS.values())
            dim_star = random.choices(stars, weights=weights, k=1)[0]

            # 【计算点击位置】
            # 最终点击坐标 = 维度文字X + 星级偏移量
            star_offset = self.STAR_POSITIONS_DIMENSION.get(dim_star, 315)
            tap_x = dim_pos[0] + star_offset

            # 点击该位置
            self.adb.tap(tap_x, dim_pos[1] + 3)
            results[dim] = dim_star
            time.sleep(0.5)  # 等待评分动画

        return results

    def submit_rating(self, screen: Optional[Screen] = None) -> bool:
        """提交评分

        【工作原理】
        1. 获取当前屏幕帧（未传入则重新截取）
        2. 查找"提交并评下一首"按钮
        3. 点击按钮完成提交

        Args:
            screen: 已有的屏幕帧；星级点击之后界面会变化，
                    默认重新截取以获取按钮的真实可用状态

        Returns:
            是否提交成功
        """
        if screen is None:
            screen = self.screens.grab(force=True)

        if screen is None:
            return False

        submit_pos = screen.find("提交并评下一首")
        if submit_pos:
            self.adb.tap(submit_pos[0], submit_pos[1])
            return True

        return False

    def full_rating_cycle(
        self,
        page_info: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None,
        screen: Optional[Screen] = None,
    ) -> Dict[str, Any]:
        """执行完整的评分周期

        【完整评分流程】
        ┌────────────────────────────────────────────────────────┐
        │                                                        │
        │  1. 定位"请评定"文字位置                                │
        │           ↓                                            │
        │  2. 执行总评分（阶段1）                                 │
        │           ↓                                            │
        │  3. 执行维度评分（阶段2）                               │
        │      - 旋律: 随机2-4星                                  │
        │      - 演唱: 随机2-4星                                  │
        │      - 歌词: 随机2-4星                                  │
        │           ↓                                            │
        │  4. 点击提交按钮                                        │
        │           ↓                                            │
        │  5. 返回评分结果                                        │
        │                                                        │
        └────────────────────────────────────────────────────────┘

        Args:
            page_info: 页面识别器返回的页面信息字典
            progress_callback: 进度回调函数，用于更新UI
            screen: 当前屏幕帧，复用页面识别时的 OCR 结果

        Returns:
            包含完整评分结果的字典：
            {
                "overall_star": 3,        # 总评分星级
                "dimensions": {          # 各维度评分
                    "旋律": 4,
                    "演唱": 3,
                    "歌词": 2
                },
                "submitted": True         # 是否提交成功
            }
        """
        results = {"overall_star": None, "dimensions": {}, "submitted": False}

        page_info = page_info or {}
        if screen is None:
            screen = page_info.get("screen")

        # 获取"请评定"文字位置
        rating_pos = page_info.get("info", {}).get("rating_trigger_pos")
        if rating_pos is None and screen is not None:
            rating_pos = screen.find("请评定")

        # 【阶段1】执行总评分
        if rating_pos:
            results["overall_star"] = self.rate_overall(rating_pos)
            print(f"已点击总评分 ({results['overall_star']}星)")

        # 【阶段2】执行维度评分
        results["dimensions"] = self.rate_dimensions()

        # 打印各维度评分结果
        for dim, star in results["dimensions"].items():
            if star:
                print(f"已评分: {dim} ({star}星)")

        # 更新进度
        if progress_callback:
            progress_callback(0, 0, "提交评分中...")

        # 【阶段3】提交评分：星级点击后按钮状态会变化，需要重新截取
        results["submitted"] = self.submit_rating()

        return results

    def rate_if_on_rating_page(
        self,
        page_info: Dict[str, Any],
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """条件评分：根据页面类型决定是否执行评分

        【使用场景】
        在主循环中判断当前页面是否为评分相关页面，
        如果是则执行评分，不是则跳过。

        Args:
            page_info: 页面识别器返回的页面信息
            progress_callback: 进度回调函数

        Returns:
            评分结果或跳过提示
        """
        page_type = page_info["type"]

        if page_type.value == "rating_popup":
            print("检测到评分弹窗，执行评分...")
            return self.full_rating_cycle(page_info, progress_callback)

        return {"action": "none", "page_type": page_type.value}
