import logging
from typing import Any, List, Optional, Tuple, Callable

from paddleocr import PaddleOCR

from .screen import TextLine


logging.getLogger("ppocr").setLevel(logging.ERROR)


class OCREngine:
    """基于PaddleOCR的文字识别引擎

    封装PaddleOCR的使用，提供文字识别和定位功能。
    主要用于识别音乐APP界面中的关键文字和按钮位置。

    【兼容性说明】
    - PaddleOCR 2.x：使用 `ocr()` + 旧结果格式
    - PaddleOCR 3.x：使用 `predict()` + `rec_texts/rec_scores/rec_polys` 结果格式，
      并关闭 doc orientation / unwarp / textline orientation 三个预处理模型
      （截图永远正向，这三个模型在 3.x 默认开启，每帧都会白跑一遍）

    【已知问题自动规避】
    PaddlePaddle 3.3.x 的 oneDNN 执行器无法运行部分模型，会抛出
    "ConvertPirAttribute2RuntimeAttribute not support ..."。
    引擎会自动关闭 MKL-DNN 后重建一次，保证任务不中断。
    建议把 paddlepaddle 固定到 3.2.0 以保留 oneDNN 加速。

    【性能要点】
    `detect()` 是对一帧画面执行 OCR 的唯一入口。
    `ScreenSource` 会把它的结果缓存在 `Screen` 对象里，
    页面识别与点击定位都复用这份结果，避免对同一张截图重复推理。
    """

    def __init__(self):
        """初始化OCR引擎"""
        print("[INFO] 视觉引擎初始化中...")
        self._major = self._detect_major_version()
        self._version = self._detect_version()
        self._use_mkldnn = True
        print(f"[INFO] PaddleOCR 版本: {self._version} (API {self._major}.x)")
        self.reader = self._create_reader()

        # 文字过滤器列表
        self._filters: List[Callable[[str], str]] = []
        # 是否启用过滤器
        self._filters_enabled: bool = True
        # 注册内置过滤器：去除"暂无歌词，沉浸享受"
        self.add_filter(self._filter_no_lyrics_placeholder)

    @staticmethod
    def _detect_version() -> str:
        """获取 PaddleOCR 版本号"""
        try:
            import paddleocr

            return str(getattr(paddleocr, "__version__", "unknown"))
        except Exception:
            return "unknown"

    @classmethod
    def _detect_major_version(cls) -> int:
        """获取 PaddleOCR 主版本号（2 或 3）"""
        version = cls._detect_version()
        try:
            return int(version.split(".")[0])
        except (ValueError, IndexError):
            # 取不到版本时按 2.x 处理（旧接口兼容面更广）
            return 2

    def _create_reader(self) -> PaddleOCR:
        """按 PaddleOCR 版本创建识别器"""
        if self._major >= 3:
            kwargs = {
                "lang": "ch",
                # 截图是正向的固定界面，这三个预处理模型没有意义，关掉可省大量算力
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            }
            if not self._use_mkldnn:
                kwargs["enable_mkldnn"] = False
            try:
                return PaddleOCR(**kwargs)
            except TypeError:
                # 极少数 3.x 早期版本不接受这些参数，退回最小配置
                return PaddleOCR(lang="ch")

        # PaddleOCR 2.x
        return PaddleOCR(use_angle_cls=True, lang="ch")

    @staticmethod
    def _filter_no_lyrics_placeholder(text: str) -> str:
        """内置过滤器：去除音乐APP的无歌词占位提示文字

        将 '暂无歌词，沉浸享受' 替换为空串，避免干扰目标文字匹配，
        同时保留 '歌词' 等其他文字不受影响。
        """
        return text.replace("暂无歌词，沉浸享受", "")

    # ==================== 过滤器管理 ====================

    def add_filter(self, filter_func: Callable[[str], str]) -> None:
        """添加文字过滤器

        过滤器会在每次文字匹配前被调用，用于预处理识别到的文字。

        Args:
            filter_func: 过滤函数，接收识别到的文字字符串，返回处理后的字符串
                        例如: lambda text: text.replace(" ", "")

        Example:
            # 去除所有空格
            ocr.add_filter(lambda text: text.replace(" ", ""))
            # 去除换行符
            ocr.add_filter(lambda text: text.replace("\n", ""))
            # 统一英文字母大小写
            ocr.add_filter(lambda text: text.lower())
        """
        self._filters.append(filter_func)
        print(f"[INFO] 已添加文字过滤器: {filter_func.__name__ if hasattr(filter_func, '__name__') else 'anonymous'}")

    def remove_filter(self, filter_func: Callable[[str], str]) -> bool:
        """移除文字过滤器

        Args:
            filter_func: 要移除的过滤器函数

        Returns:
            True 如果成功移除，否则 False
        """
        if filter_func in self._filters:
            self._filters.remove(filter_func)
            print(f"[INFO] 已移除文字过滤器")
            return True
        return False

    def clear_filters(self) -> None:
        """清空所有过滤器"""
        self._filters.clear()
        print("[INFO] 已清空所有文字过滤器")

    def enable_filters(self, enabled: bool = True) -> None:
        """启用或禁用过滤器

        Args:
            enabled: True 启用过滤器，False 禁用过滤器
        """
        self._filters_enabled = enabled
        print(f"[INFO] 过滤器已{'启用' if enabled else '禁用'}")

    def get_filters(self) -> List[Callable[[str], str]]:
        """获取当前所有过滤器"""
        return self._filters.copy()

    def _apply_filters(self, text: str) -> str:
        """应用所有过滤器到文字上"""
        if not self._filters_enabled or not self._filters:
            return text

        filtered_text = text
        for filter_func in self._filters:
            try:
                filtered_text = filter_func(filtered_text)
            except Exception as e:
                print(f"[WARNING] 文字过滤器执行失败: {e}")
                continue
        return filtered_text

    # ==================== 核心OCR方法 ====================

    def _infer(self, image: Any) -> Any:
        """执行底层推理，屏蔽 2.x / 3.x 的接口差异"""
        if self._major >= 3:
            return self.reader.predict(image)
        try:
            return self.reader.ocr(image, cls=False)
        except TypeError:
            return self.reader.ocr(image)

    def detect(self, image: Any) -> List[TextLine]:
        """对一帧画面执行一次 OCR，返回全部识别结果

        Args:
            image: 截图路径（PaddleOCR 2.x/3.x 均支持）

        Returns:
            TextLine 列表；未识别到文字时返回空列表
        """
        try:
            raw_result = self._infer(image)
        except NotImplementedError as e:
            # PaddlePaddle 3.3.x + oneDNN 的已知缺陷，自动降级重试一次
            if not self._use_mkldnn or "ConvertPirAttribute" not in str(e):
                raise
            print(
                "[WARNING] 检测到 oneDNN 执行器缺陷(ConvertPirAttribute2RuntimeAttribute)，"
                "正在关闭 MKL-DNN 后重试..."
            )
            print("[WARNING] 建议执行: pip install paddlepaddle==3.2.0 以恢复 oneDNN 加速")
            self._use_mkldnn = False
            self.reader = self._create_reader()
            raw_result = self._infer(image)

        if self._major >= 3:
            return self._parse_v3(raw_result)
        return self._parse_v2(raw_result)

    def _parse_v2(self, result: Any) -> List[TextLine]:
        """解析 PaddleOCR 2.x 结果: [[box, (text, conf)], ...]"""
        if not result or not result[0]:
            return []

        lines: List[TextLine] = []
        for item in result[0]:
            try:
                box, payload = item[0], item[1]
                text, conf = payload[0], float(payload[1])
            except (TypeError, ValueError, IndexError):
                continue
            lines.append(
                TextLine(
                    text=self._apply_filters(text),
                    raw_text=text,
                    conf=conf,
                    box=box,
                )
            )
        return lines

    def _parse_v3(self, result: Any) -> List[TextLine]:
        """解析 PaddleOCR 3.x 结果

        3.x 的每个结果是一个 dict-like OCRResult，包含：
        rec_texts / rec_scores / rec_polys（或 rec_boxes）
        """
        if result is None:
            return []
        if isinstance(result, dict):
            result = [result]

        lines: List[TextLine] = []
        try:
            results = list(result)
        except TypeError:
            return []

        for res in results:
            try:
                texts = res.get("rec_texts") or []
                scores = res.get("rec_scores") or []
                polys = res.get("rec_polys")
                if polys is None:
                    polys = res.get("rec_boxes") or []
            except AttributeError:
                continue

            for text, conf, poly in zip(texts, scores, polys):
                box = self._normalize_box(poly)
                if box is None:
                    continue
                lines.append(
                    TextLine(
                        text=self._apply_filters(str(text)),
                        raw_text=str(text),
                        conf=float(conf),
                        box=box,
                    )
                )
        return lines

    @staticmethod
    def _normalize_box(poly: Any) -> Optional[list]:
        """把 3.x 的四边形 / 矩形框统一成四点框"""
        if poly is None:
            return None
        try:
            box = poly.tolist() if hasattr(poly, "tolist") else list(poly)
        except (TypeError, ValueError):
            return None

        if not box:
            return None

        # rec_boxes 形式: [x_min, y_min, x_max, y_max]
        if isinstance(box[0], (int, float)):
            if len(box) < 4:
                return None
            x1, y1, x2, y2 = box[:4]
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        return box

    def find_text(
        self, img_path: str, target: str, min_confidence: float = 0.6
    ) -> Optional[Tuple[int, int]]:
        """在截图中查找指定文字的位置

        注意：该方法是单次查询的便捷入口，每次调用都会执行一次 OCR。
        页面识别等需要多次查询的场景请使用 `ScreenSource.grab()` 返回的
        `Screen` 对象，避免重复推理。

        Args:
            img_path: 截图文件路径
            target: 要查找的目标文字（支持模糊匹配，只要包含即可）
            min_confidence: 最小置信度阈值，低于此值的识别结果将被忽略

        Returns:
            文字中心的坐标 (x, y)，如果未找到则返回 None
        """
        for line in self.detect(img_path):
            if line.conf > min_confidence and target in line.text:
                print(
                    f"[DEBUG] 识别到: {line.raw_text} -> 过滤后: "
                    f"{line.text} ({line.conf:.2f}) 坐标: {line.x}, {line.y}"
                )
                return line.center
        return None

    def find_all_text(self, img_path: str, min_confidence: float = 0.6) -> list:
        """查找截图中所有文字及其位置

        Args:
            img_path: 截图文件路径
            min_confidence: 最小置信度阈值

        Returns:
            识别结果列表，每项包含 (文字, 置信度, 坐标x, 坐标y)
        """
        return [
            (line.text, line.conf, line.x, line.y)
            for line in self.detect(img_path)
            if line.conf > min_confidence
        ]

    def find_text_exact(
        self, img_path: str, target: str, min_confidence: float = 0.6
    ) -> Optional[Tuple[int, int]]:
        """精确查找文字（完整匹配）

        Args:
            img_path: 截图文件路径
            target: 要查找的目标文字（需要完全匹配）
            min_confidence: 最小置信度阈值

        Returns:
            文字中心的坐标 (x, y)，如果未找到则返回 None
        """
        for line in self.detect(img_path):
            if line.conf > min_confidence and line.text == target:
                print(
                    f"[DEBUG] 精确识别到: {line.raw_text} -> 过滤后: "
                    f"{line.text} ({line.conf:.2f}) 坐标: {line.x}, {line.y}"
                )
                return line.center
        return None
