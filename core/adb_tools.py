import os
import random
import subprocess
import sys
from typing import List


class ADBTools:
    """ADB设备交互工具类

    封装与Android模拟器通过ADB协议进行交互的所有操作，
    包括连接设备、截图、点击、滑动等基本操作。

    【截图说明】
    优先使用 `exec-out screencap -p` 直接获取 PNG 字节流，
    避免"截图到 /sdcard → pull 回本地 → 再解码"的三次数据搬运；
    若设备的 exec-out 二进制输出不可靠，会自动回退到文件方式。
    """

    # Windows 下隐藏子进程窗口，非 Windows 平台为 0
    _CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def __init__(self, adb_path: str, device_id: str):
        """初始化ADB工具

        Args:
            adb_path: adb.exe文件的完整路径
            device_id: 目标设备ID，如 "127.0.0.1:16416"
        """
        self.adb_path = adb_path
        self.device_id = device_id
        self._exec_out_ok = True
        self._screenshot_dir = os.path.join(os.path.abspath("."), "screenshots")

    @staticmethod
    def get_resource_path(relative_path: str) -> str:
        """获取资源文件的绝对路径（兼容PyInstaller打包）

        Args:
            relative_path: 相对路径

        Returns:
            资源文件的绝对路径
        """
        if hasattr(sys, "_MEIPASS"):
            return os.path.join(sys._MEIPASS, relative_path)
        return os.path.join(os.path.abspath("."), relative_path)

    def _run_args(
        self, args: List[str], use_device: bool = True
    ) -> subprocess.CompletedProcess:
        """执行ADB命令（参数以列表传递，避免路径含空格时解析错误）

        Args:
            args: ADB命令参数列表
            use_device: 是否附加 -s 设备参数
        """
        cmd = [self.adb_path]
        if use_device and self.device_id:
            cmd += ["-s", self.device_id]
        cmd += [str(a) for a in args]
        return subprocess.run(
            cmd,
            capture_output=True,
            creationflags=self._CREATION_FLAGS,
        )

    def connect(self) -> bool:
        """连接到指定的模拟器设备

        Returns:
            连接是否成功
        """
        print(f"[INFO] 正在连接模拟器: {self.device_id}...")
        result = self._run_args(["connect", self.device_id], use_device=False)
        return result.returncode == 0

    def disable_exec_out(self) -> None:
        """禁用 exec-out 截图方式（数据损坏时降级使用文件方式）"""
        if self._exec_out_ok:
            print("[WARNING] 已切换到文件方式截图")
            self._exec_out_ok = False

    def tap(self, x: int, y: int, offset: int = 3) -> None:
        """在指定坐标位置执行点击操作

        Args:
            x: 点击的X坐标
            y: 点击的Y坐标
            offset: 点击偏移范围，会在[-offset, +offset]范围内随机偏移
        """
        random_x = x + random.randint(-offset, offset)
        random_y = y + random.randint(-offset, offset)
        print(
            f"[DEBUG] 点击坐标: ({random_x}, {random_y}) (原始: {x}, {y}, 偏移: {random_x - x}, {random_y - y})"
        )
        self._run_args(["shell", "input", "tap", str(random_x), str(random_y)])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> None:
        """执行滑动操作

        Args:
            x1: 起始点X坐标
            y1: 起始点Y坐标
            x2: 结束点X坐标
            y2: 结束点Y坐标
            duration: 滑动持续时间（毫秒）
        """
        self._run_args(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]
        )

    def take_screenshot(self, filename: str) -> str:
        """截取当前屏幕并保存到本地（文件方式，供调试工具使用）

        Returns:
            截图文件的本地完整路径
        """
        if not os.path.exists(self._screenshot_dir):
            os.makedirs(self._screenshot_dir)
        local_path = os.path.join(self._screenshot_dir, filename)
        self._run_args(["shell", "screencap", "-p", "/sdcard/screen.png"])
        self._run_args(["pull", "/sdcard/screen.png", local_path])
        return local_path

    def save_png(self, data: bytes, filename: str = "frame.png") -> str:
        """把已有的 PNG 字节写入本地缓存文件

        用于把 exec-out 拿到的字节交给只接受文件路径的 OCR 接口，
        写入的是原始 PNG 字节，不需要重新编码。

        Returns:
            本地文件路径；写入失败返回空串
        """
        if not os.path.exists(self._screenshot_dir):
            os.makedirs(self._screenshot_dir)
        path = os.path.join(self._screenshot_dir, filename)
        try:
            with open(path, "wb") as f:
                f.write(data)
            return path
        except OSError as e:
            print(f"[WARNING] 写入截图缓存失败: {e}")
            return ""

    def capture_png(self) -> bytes:
        """获取当前屏幕的 PNG 字节

        优先走 exec-out（不落盘、只有一个子进程），失败则回退到文件方式。
        返回空字节表示截图失败。
        """
        if self._exec_out_ok:
            result = self._run_args(["exec-out", "screencap", "-p"])
            data = result.stdout or b""
            if result.returncode == 0 and data.startswith(b"\x89PNG"):
                return data
            self.disable_exec_out()

        path = self.take_screenshot("frame.png")
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as e:
            print(f"[WARNING] 读取截图失败: {e}")
            return b""
