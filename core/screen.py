"""屏幕帧模型与截图/OCR 缓存

【为什么需要这一层】
此前每一次文字查询（`PageRecognizer`、`SongScorer` 等）都会调用
`OCREngine.find_text()`，而它内部会把整张截图重新跑一遍 PaddleOCR。
识别一个页面往往要查询 10 次以上文字，等于对同一张图做了 10 多次 OCR，
这是运行时 CPU 占用高的主要原因。

现在改为：
1. 一帧画面只做 **一次** OCR，结果保存在 `Screen` 对象里；
2. 所有文字查询都在内存的识别结果上做，不再重复推理；
3. 截图走 `adb exec-out screencap -p` 直接拿 PNG 字节，省掉 pull 和重复编解码；
4. 画面未变化（PNG 字节 MD5 相同）时直接复用上一帧结果，跳过 OCR。
"""

import hashlib
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from .adb_tools import ADBTools
    from .ocr_engine import OCREngine


# OCR 返回的四点框，顺序为 左上、右上、右下、左下
Box = Sequence[Sequence[float]]


@dataclass
class TextLine:
    """一条 OCR 识别结果"""

    text: str  # 经文字过滤器处理后的文字（用于匹配）
    raw_text: str  # OCR 原始输出（用于日志）
    conf: float  # 置信度
    box: Box  # 四点框坐标

    @property
    def center(self) -> Tuple[int, int]:
        """文字区域中心点坐标"""
        return (
            int((self.box[0][0] + self.box[2][0]) / 2),
            int((self.box[0][1] + self.box[2][1]) / 2),
        )

    @property
    def x(self) -> int:
        return self.center[0]

    @property
    def y(self) -> int:
        return self.center[1]

    @property
    def width(self) -> int:
        return abs(int(self.box[1][0] - self.box[0][0]))

    @property
    def height(self) -> int:
        return abs(int(self.box[2][1] - self.box[0][1]))


class Screen:
    """一帧屏幕画面及其 OCR 结果

    同一个 `Screen` 对象上可以任意次查询文字，不会再次触发 OCR。
    """

    def __init__(self, image: np.ndarray, lines: List[TextLine], digest: str = ""):
        self.image = image
        self.lines = lines
        self.digest = digest

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    # ==================== 文字查询 ====================

    def find_line(
        self, target: str, min_conf: float = 0.6
    ) -> Optional[TextLine]:
        """查找第一条包含目标文字的结果（模糊匹配）"""
        for line in self.lines:
            if line.conf > min_conf and target in line.text:
                return line
        return None

    def find(
        self, target: str, min_conf: float = 0.6
    ) -> Optional[Tuple[int, int]]:
        """查找目标文字的中心坐标，找不到返回 None"""
        line = self.find_line(target, min_conf)
        return line.center if line else None

    def find_any(
        self, targets: Sequence[str], min_conf: float = 0.6
    ) -> Optional[TextLine]:
        """按优先级查找多个目标文字，返回第一个命中的结果"""
        for target in targets:
            line = self.find_line(target, min_conf)
            if line:
                return line
        return None

    def find_all(self, target: str, min_conf: float = 0.6) -> List[TextLine]:
        """查找所有包含目标文字的识别结果"""
        return [
            line
            for line in self.lines
            if line.conf > min_conf and target in line.text
        ]

    def contains(self, target: str, min_conf: float = 0.6) -> bool:
        """判断画面中是否存在目标文字"""
        return self.find_line(target, min_conf) is not None

    # ==================== 像素采样 ====================

    def pixel(self, x: int, y: int) -> Optional[Tuple[int, int, int]]:
        """读取指定位置的 RGB 像素，越界返回 None"""
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            return None
        r, g, b = self.image[y, x][:3]
        return int(r), int(g), int(b)

    def brightness(self, x: int, y: int) -> Optional[int]:
        """读取指定位置的亮度（0-255），越界返回 None"""
        px = self.pixel(x, y)
        if px is None:
            return None
        return (px[0] + px[1] + px[2]) // 3


class ScreenSource:
    """屏幕帧来源：负责截图、去重和 OCR

    所有模块共享同一个实例，从而共享"上一帧"缓存。
    """

    def __init__(self, ocr: "OCREngine", adb: "ADBTools"):
        self.ocr = ocr
        self.adb = adb
        self._digest = ""
        self._screen: Optional[Screen] = None
        self._ocr_count = 0  # 累计实际执行的 OCR 次数，便于观察优化效果

    @property
    def ocr_count(self) -> int:
        """已经实际执行过的 OCR 次数"""
        return self._ocr_count

    def invalidate(self) -> None:
        """丢弃缓存的上一帧（例如执行了点击/滑动之后）"""
        self._digest = ""
        self._screen = None

    def grab(self, force: bool = False) -> Optional[Screen]:
        """截取一帧并返回带 OCR 结果的 Screen

        Args:
            force: 为 True 时忽略画面哈希缓存，强制重新 OCR

        Returns:
            Screen 对象；截图或解码失败时返回 None
        """
        raw = self.adb.capture_png()
        if not raw:
            return None

        digest = hashlib.md5(raw).hexdigest()
        if not force and digest == self._digest and self._screen is not None:
            return self._screen

        image = self._decode(raw)
        if image is None:
            # 部分 ADB 版本的 exec-out 会破坏二进制数据，回退到文件方式重试一次
            self.adb.disable_exec_out()
            raw = self.adb.capture_png()
            if not raw:
                return None
            digest = hashlib.md5(raw).hexdigest()
            image = self._decode(raw)
            if image is None:
                print("[WARNING] 截图解码失败")
                return None

        # 注意：PaddleOCR 使用文件路径输入（此前验证可用的方式）。
        # 直接传 numpy 数组在 PaddlePaddle 3.x + oneDNN 组合下会触发
        # "ConvertPirAttribute2RuntimeAttribute not support" 错误。
        # 这里写入的是 exec-out 拿到的原始 PNG 字节，不需要重新编码。
        frame_path = self.adb.save_png(raw)
        if not frame_path:
            return None

        lines = self.ocr.detect(frame_path)
        self._ocr_count += 1

        screen = Screen(image, lines, digest)
        self._digest = digest
        self._screen = screen
        return screen

    @staticmethod
    def _decode(raw: bytes) -> Optional[np.ndarray]:
        """把 PNG 字节解码为 RGB numpy 数组"""
        try:
            with Image.open(io.BytesIO(raw)) as img:
                return np.asarray(img.convert("RGB"))
        except Exception:
            return None
