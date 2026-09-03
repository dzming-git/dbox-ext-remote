#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dbox 远程桌面 · 桌面代理（agent）。

必须运行在**用户登录会话**里：Windows 把服务隔离到 Session 0，那里没有用户的桌面，
截图只会拿到空白。所以采集与输入注入都由本进程完成，插件后端（服务内）只做鉴权与转发。

对外只监听 127.0.0.1:18921，不对外暴露；单实例（端口被占说明已有实例在跑）。

端点：
  GET  /info                        → 屏幕尺寸、会话、进程信息（后端探活用）
  GET  /frame?scale=&q=&gray=       → 一帧 JPEG；响应头 X-Frame-Hash 供后端做静止检测
  POST /input                       → 注入鼠标/键盘事件
"""

import os
import io
import sys
import time
import json
import ctypes
import hashlib
import threading
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- DPI 感知：必须在创建任何窗口/DC 之前设置 ----
# 本机若开了显示缩放（如 125%，物理 1920×1080 被虚化成 1536×864），未声明感知的
# 进程里 GetSystemMetrics 返回虚化尺寸，但 SendInput 的绝对坐标始终按**物理屏**
# 归一化。于是：抓屏只截到 1536 宽（显示不全）+ 鼠标点击按 1.25 倍偏移（固定偏差），
# 两个症状同源。声明感知后两者都回到物理尺寸，偏差消失。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

AGENT_PORT = 18921
AGENT_HOST = '127.0.0.1'

# ---------------------------------------------------------------- 屏幕捕获


class ScreenCapture(object):
    """GDI BitBlt 抓屏。

    不用 PIL.ImageGrab：它每次调用都创建/销毁 DC，连续抓屏时开销明显。
    这里复用 DC 与位图，并把光标画进画面——BitBlt 抓到的是桌面内容，**不含鼠标指针**，
    不补画的话远程端完全看不到自己操作到哪儿了。
    """

    SRCCOPY = 0x00CC0020
    CURSOR_SHOWING = 0x00000001
    DI_NORMAL = 0x0003

    def __init__(self):
        import win32api
        import win32gui
        import win32ui
        self._win32api = win32api
        self._win32gui = win32gui
        self._win32ui = win32ui
        self._lock = threading.Lock()
        self._dcs = None
        self.cursor_on = True
        self._setup(win32api, win32gui, win32ui)
        # GDI 专属抓屏线程：HTTP 每请求一个线程，而 win32ui 的 DC/位图对象**绑在创建它的线程上**，
        # 跨线程复用会直接 BitBlt failed（实测：A 线程建的 DC，B 线程用必挂）。
        # 故所有 GDI 调用都收敛到这一个线程里做，DC 在哪建就在哪用，彻底规避线程亲和问题。
        # 顺带修掉一个既有隐患：分辨率变化时 refresh_if_changed→_setup 会在请求线程重建整屏 DC，
        # 之后其它线程的整屏抓取全部永久失败（远程画面就此卡死）。
        self._jobs = __import__('queue').Queue()
        self._worker = threading.Thread(target=self._worker_loop, name='gdi-capture')
        self._worker.daemon = True
        self._worker.start()

    def _setup(self, win32api, win32gui, win32ui):
        # 虚拟屏（多显示器合并后的整块画布）；单显示器时这几个值与主屏一致
        left = win32api.GetSystemMetrics(76)    # SM_XVIRTUALSCREEN
        top = win32api.GetSystemMetrics(77)     # SM_YVIRTUALSCREEN
        width = win32api.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        height = win32api.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:           # 极少数环境取不到虚拟屏，回落主屏
            left, top = 0, 0
            width = win32api.GetSystemMetrics(0)
            height = win32api.GetSystemMetrics(1)
        self._release()
        self.left, self.top = left, top
        self.width, self.height = width, height
        hwnd = win32gui.GetDesktopWindow()
        windc = win32gui.GetWindowDC(hwnd)
        srcdc = win32ui.CreateDCFromHandle(windc)
        memdc = srcdc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(srcdc, width, height)
        memdc.SelectObject(bmp)
        self._dcs = (windc, srcdc, memdc, bmp)

    def _release(self):
        if not self._dcs:
            return
        windc, srcdc, memdc, bmp = self._dcs
        self._dcs = None
        try:
            self._win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            memdc.DeleteDC()
            srcdc.DeleteDC()
        except Exception:
            pass
        try:
            self._win32gui.ReleaseDC(self._win32gui.GetDesktopWindow(), windc)
        except Exception:
            pass

    def _worker_loop(self):
        """抓屏线程主循环：所有 GDI 调用都在这里发生。"""
        while True:
            job = self._jobs.get()
            if job is None:
                return
            rect, slot = job
            try:
                slot['img'] = self._grab_now(rect)
            except Exception as e:
                slot['err'] = e
            slot['evt'].set()

    def grab(self, rect=None):
        """取一帧 PIL Image（RGB）。可从任意线程调用，实际 GDI 工作交给抓屏线程。

        rect=(x, y, w, h) 为虚拟屏坐标时只抓这一块（省流量：用户放大看局部时，
        整屏采集+编码的绝大多数像素都被浪费）。rect 为 None 时整屏。
        """
        if threading.current_thread() is self._worker:
            return self._grab_now(rect)          # 已在抓屏线程：直接做，避免自锁
        slot = {'evt': threading.Event(), 'img': None, 'err': None}
        self._jobs.put((rect, slot))
        if not slot['evt'].wait(15):
            raise RuntimeError('capture timeout')
        if slot['err'] is not None:
            raise slot['err']
        return slot['img']

    def _grab_now(self, rect=None):
        """真正的 GDI 抓取，**只允许在 self._worker 线程内调用**。"""
        from PIL import Image
        with self._lock:
            self.refresh_if_changed()
            if rect is None:
                # 整屏位图长期复用；若 BitBlt 失败（DC 已失效）就在本线程重建后重试一次。
                # 重建必须在这里做：在别的线程重建会让这个 DC 对所有其它线程失效。
                for attempt in (0, 1):
                    try:
                        _, srcdc, memdc, bmp = self._dcs
                        memdc.BitBlt((0, 0), (self.width, self.height),
                                     srcdc, (self.left, self.top), self.SRCCOPY)
                        break
                    except Exception:
                        if attempt:
                            raise
                        self._setup(self._win32api, self._win32gui, self._win32ui)
                if self.cursor_on:
                    self._draw_cursor(memdc)
                bits = bmp.GetBitmapBits(True)
                w, h = self.width, self.height
            else:
                rx, ry, rw, rh = rect
                srcdc = self._dcs[1]
                # ROI 每次现建现用：不能复用整屏位图（GetBitmapBits 会把整块 8MB 全拷出来，
                # 只抓一小块时这个拷贝才是主要开销），而 ROI 尺寸随视口频繁变化、缓存收益有限，
                # 现建现用还顺带彻底避开「跨线程复用 DC」这一整类问题。
                memdc = srcdc.CreateCompatibleDC()
                bmp = self._win32ui.CreateBitmap()
                try:
                    bmp.CreateCompatibleBitmap(srcdc, rw, rh)
                    memdc.SelectObject(bmp)
                    memdc.BitBlt((0, 0), (rw, rh), srcdc,
                                 (self.left + rx, self.top + ry), self.SRCCOPY)
                    if self.cursor_on:
                        # 指针是屏幕坐标，抓子区域时要减掉区域原点才是位图内坐标
                        self._draw_cursor(memdc, offset=(-rx, -ry), size=(rw, rh))
                    bits = bmp.GetBitmapBits(True)
                finally:
                    try:
                        self._win32gui.DeleteObject(bmp.GetHandle())
                    except Exception:
                        pass
                    try:
                        memdc.DeleteDC()
                    except Exception:
                        pass
                w, h = rw, rh
        # frombuffer 是零拷贝视图，必须 copy 后才能安全复用底层 DC
        return Image.frombuffer('RGB', (w, h), bits, 'raw', 'BGRX', 0, 1).copy()

    def refresh_if_changed(self):
        """分辨率/显示器布局变了要重建 DC，否则画面会被裁掉或拉伸。"""
        try:
            w = self._win32api.GetSystemMetrics(78) or self._win32api.GetSystemMetrics(0)
            h = self._win32api.GetSystemMetrics(79) or self._win32api.GetSystemMetrics(1)
            if w != self.width or h != self.height:
                self._setup(self._win32api, self._win32gui, self._win32ui)
                return True
        except Exception:
            pass
        return False

    def _draw_cursor(self, memdc, offset=(0, 0), size=None):
        """把指针画进位图。抓 ROI 时 offset=(-rx,-ry)、size=(rw,rh)，
        指针落在区域外就不画（避免把图标画到错误的相对位置）。"""
        try:
            info = self._win32gui.GetCursorInfo()
            if not info or len(info) < 3:
                return
            flags, hcursor, pos = info[0], info[1], info[2]
            if not (flags & self.CURSOR_SHOWING) or not hcursor:
                return
            x, y = pos[0] - self.left, pos[1] - self.top
            if not (0 <= x < self.width and 0 <= y < self.height):
                return                                  # 指针在虚拟屏外
            x += offset[0]
            y += offset[1]
            w, h = size or (self.width, self.height)
            if not (0 <= x < w and 0 <= y < h):
                return                                  # 指针不在本次抓取的区域内
            self._win32gui.DrawIconEx(memdc.GetHandleOutput(), x, y, hcursor,
                                      0, 0, 0, None, self.DI_NORMAL)
        except Exception:
            pass

    def close(self):
        try:
            self._jobs.put(None)          # 通知抓屏线程退出
        except Exception:
            pass
        self._release()


# ---------------------------------------------------------------- 输入注入

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_MAP = {
    'enter': 0x0D, 'backspace': 0x08, 'tab': 0x09, 'escape': 0x1B, 'esc': 0x1B,
    'delete': 0x2E, 'insert': 0x2D, 'home': 0x24, 'end': 0x23,
    'pageup': 0x21, 'pagedown': 0x22, 'space': 0x20,
    'arrowleft': 0x25, 'arrowup': 0x26, 'arrowright': 0x27, 'arrowdown': 0x28,
    'shift': 0x10, 'control': 0x11, 'ctrl': 0x11, 'alt': 0x12, 'meta': 0x5B,
    'win': 0x5B, 'capslock': 0x14, 'printscreen': 0x2C, 'apps': 0x5D,
}
for _i in range(1, 13):
    VK_MAP['f%d' % _i] = 0x6F + _i


class InputInjector(object):
    """用 SendInput 注入输入。

    键盘走 KEYEVENTF_UNICODE：直接发字符而非虚拟键码，中文等输入法内容也能正确上屏，
    不必维护一整套键盘布局映射表。
    """

    def __init__(self, screen):
        self.screen = screen
        self._u = ctypes.windll.user32
        self._ptr = (ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
                     else ctypes.c_ulong)

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                        ('mouseData', ctypes.c_ulong), ('dwFlags', ctypes.c_ulong),
                        ('time', ctypes.c_ulong), ('dwExtraInfo', self._ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort),
                        ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                        ('dwExtraInfo', self._ptr)]

        class UNION(ctypes.Union):
            _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [('type', ctypes.c_ulong), ('u', UNION)]

        class POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

        self.MOUSEINPUT = MOUSEINPUT
        self.KEYBDINPUT = KEYBDINPUT
        self.INPUT = INPUT
        self._lock = threading.Lock()
        # 系统/主屏 DPI（125% 缩放 = 120）。用它推出虚拟化尺寸：SendInput 的绝对坐标
        # 归一化恒定按**虚拟化虚拟屏**（不受进程 DPI 感知影响），而 screen.width/height
        # 已是 DPI 感知后的物理尺寸，两者差一个缩放比，必须换算，否则点击按 1/scale
        # 成比例偏移（125% 下偏 0.8 倍）。
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except Exception:
            dpi = 96
        self._scale = max(1.0, dpi / 96.0)
        self.cursor_on = True          # 是否在抓屏画面里绘制鼠标指针（面板可切换）

    def _send(self, items):
        arr = (self.INPUT * len(items))(*items)
        n = self._u.SendInput(len(items), arr, ctypes.sizeof(self.INPUT))
        # SendInput 返回「实际插入的事件数」。为 0 即被拒绝——最常见原因是 UIPI：
        # 低完整性进程无法向完整性更高的窗口注入输入。它**不抛异常**，若不检查就
        # 会静默吞掉，调用方以为注入成功，表现正是「点击无响应却查不出哪里错」。
        if not n:
            raise RuntimeError('SendInput 被拒绝（err=%s，通常是目标窗口权限更高）'
                               % ctypes.GetLastError())
        return n

    def _mi(self, dx, dy, data, flags):
        i = self.INPUT()
        i.type = INPUT_MOUSE
        i.u.mi.dx = dx
        i.u.mi.dy = dy
        i.u.mi.mouseData = data
        i.u.mi.dwFlags = flags
        return i

    def _ki(self, vk, scan, flags):
        i = self.INPUT()
        i.type = INPUT_KEYBOARD
        i.u.ki.wVk = vk
        i.u.ki.wScan = scan
        i.u.ki.dwFlags = flags
        return i

    def _abs(self, x, y):
        """像素坐标 → SendInput 的 0..65535 归一化绝对坐标（按物理主屏尺寸）。

        **不加 MOUSEEVENTF_VIRTUALDESK**：本进程已 SetProcessDpiAwareness(2)，
        无 VIRTUALDESK 时绝对坐标按「物理主屏」归一化，分母用物理宽高即精确、整屏可达；
        加 VIRTUALDESK 反会被 Windows 锁死到「虚拟化虚拟屏」，125% 缩放下右/底约 0.8×
        区域物理不可达、画出来整条偏移（这是此前点击偏 0.8 倍的根因）。
        """
        vw = max(1, self.screen.width - 1)
        vh = max(1, self.screen.height - 1)
        nx = int(max(0, min(65535, x * 65535 // vw)))
        ny = int(max(0, min(65535, y * 65535 // vh)))
        return nx, ny

    def mouse_move(self, x, y):
        # 关键：必须让光标通过「注入事件」真正移动到目标点，才能派发 WM_MOUSEMOVE。
        # 画图（画图 / Photoshop 等）靠 WM_MOUSEMOVE 的实时位置连续落点；只 SetCursorPos
        # 移动光标而不发事件 → 目标窗口收不到中间点，只会把按下点连到抬起点画成直线
        # （纯文本拖选靠轮询光标位置所以看似正常，正是这个坑最隐蔽的地方）。
        #
        # 做法：先发「绝对坐标移动」事件（MOUSEEVENTF_ABSOLUTE，不加 VIRTUALDESK，
        # DPI 感知下精确映射到物理主屏）→ 光标从旧位置真的移到目标点 → 必发 WM_MOUSEMOVE
        # 且坐标正确；随后 SetCursorPos 同点精确归位（双保险，消除任何量化抖动）。
        # 注意顺序：绝对移动必须在 SetCursorPos 之前，否则光标已停在目标点、OS 判定无位移
        # 照样不派发 WM_MOUSEMOVE（be6e5bd 那版就是死在这个顺序上）。
        nx, ny = self._abs(int(x), int(y))
        self._send([self._mi(nx, ny, 0,
                              MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)])   # 先注入移动，必发 WM_MOUSEMOVE
        self._u.SetCursorPos(int(x), int(y))                              # 同点归位，双保险

    def _btn_flags(self, button, down):
        b = (button or 'left').lower()
        if b == 'right':
            return MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP
        if b == 'middle':
            return MOUSEEVENTF_MIDDLEDOWN if down else MOUSEEVENTF_MIDDLEUP
        return MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP

    def mouse_button(self, x, y, button, down, clicks=1):
        self.mouse_move(x, y)
        f = self._btn_flags(button, down)
        self._send([self._mi(0, 0, 0, f)])

    def mouse_click(self, x, y, button, clicks=1):
        self.mouse_move(x, y)
        down = self._btn_flags(button, True)
        up = self._btn_flags(button, False)
        items = []
        for _ in range(max(1, min(int(clicks or 1), 3))):
            items.append(self._mi(0, 0, 0, down))
            items.append(self._mi(0, 0, 0, up))
        self._send(items)

    def wheel(self, dx, dy):
        items = []
        if dy:
            items.append(self._mi(0, 0, int(dy), MOUSEEVENTF_WHEEL))
        if dx:
            items.append(self._mi(0, 0, int(dx), MOUSEEVENTF_HWHEEL))
        if items:
            self._send(items)

    def _key_vk(self, vk, up):
        self._send([self._ki(vk, 0, KEYEVENTF_KEYUP if up else 0)])

    def _key_unicode(self, ch):
        for c in str(ch):
            code = ord(c)
            # UTF-16 代理对：SendInput 按 UTF-16 码元发送
            units = [code] if code < 0x10000 else self._surrogate(code)
            for u in units:
                self._send([self._ki(0, u, KEYEVENTF_UNICODE),
                            self._ki(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])

    @staticmethod
    def _surrogate(code):
        code -= 0x10000
        return [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]

    def key_combo(self, key, ctrl=False, alt=False, shift=False, meta=False):
        mods = []
        if ctrl:
            mods.append(VK_MAP['control'])
        if alt:
            mods.append(VK_MAP['alt'])
        if shift:
            mods.append(VK_MAP['shift'])
        if meta:
            mods.append(VK_MAP['meta'])
        for vk in mods:
            self._key_vk(vk, False)
        k = str(key or '')
        if len(k) == 1:
            self._key_unicode(k)
        else:
            vk = VK_MAP.get(k.lower())
            if vk is None:
                try:
                    vk = ord(k.upper()[0])
                except Exception:
                    vk = None
            if vk:
                self._key_vk(vk, False)
                self._key_vk(vk, True)
        for vk in reversed(mods):
            self._key_vk(vk, True)

    def text(self, s):
        self._key_unicode(s)

    def dispatch(self, ev):
        """把前端事件分派到具体动作。所有操作在同一把锁下串行，避免并发乱序。"""
        t = (ev.get('type') or '').lower()
        with self._lock:
            if t == 'mouse_move':
                self.mouse_move(int(ev.get('x', 0)), int(ev.get('y', 0)))
            elif t == 'mouse_down':
                self.mouse_button(int(ev.get('x', 0)), int(ev.get('y', 0)),
                                  ev.get('button'), True)
            elif t == 'mouse_up':
                self.mouse_button(int(ev.get('x', 0)), int(ev.get('y', 0)),
                                  ev.get('button'), False)
            elif t == 'mouse_click':
                self.mouse_click(int(ev.get('x', 0)), int(ev.get('y', 0)),
                                 ev.get('button'), int(ev.get('clicks', 1)))
            elif t == 'wheel':
                self.wheel(int(ev.get('dx', 0)), int(ev.get('dy', 0)))
            elif t == 'key':
                self.key_combo(ev.get('key', ''), bool(ev.get('ctrl')),
                               bool(ev.get('alt')), bool(ev.get('shift')),
                               bool(ev.get('meta')))
            elif t == 'text':
                self.text(ev.get('text', ''))
            else:
                raise ValueError('未知输入类型: %s' % t)


# ---------------------------------------------------------------- HTTP 服务

class AgentState(object):
    def __init__(self):
        self.screen = ScreenCapture()
        self.input = InputInjector(self.screen)
        self.started_at = time.time()

    def session_id(self):
        try:
            return os.getpid()
        except Exception:
            return 0


_STATE = None

# ---- 静止检测 ----
# 把画面降采样成 64×36 的灰度指纹再比「平均像素差」，而不是比对整帧 md5：
# 桌面上几乎总会有秒级变化（任务栏时钟、加载动画、光标闪烁），精确比 hash 的话
# 这些微小变化会让「静止不推帧」永远不生效，省流量档位就只剩降帧率和降画质可用。
STILL_THRESHOLD = 2.5          # 0-255 的平均差异，低于此即视为画面没动
_SIG_W, _SIG_H = 64, 36
_last = {'data': None, 'hash': None, 'sig': None, 'key': None}


def _signature(img):
    return img.convert('L').resize((_SIG_W, _SIG_H), 1)     # 1 = Image.BILINEAR


def _is_still(sig, threshold):
    prev = _last.get('sig')
    if prev is None:
        return False
    try:
        from PIL import ImageChops
        hist = ImageChops.difference(prev, sig).histogram()
        total = sum(i * c for i, c in enumerate(hist))
        return (total / float(_SIG_W * _SIG_H)) < threshold
    except Exception:
        return False


def integrity_level():
    """当前进程的完整性级别（SID 的 RID）：0x1000=Low 0x2000=Medium 0x3000=High。

    UIPI 规则——低完整性进程无法向完整性更高的窗口注入输入，SendInput 会直接返回 0。
    agent 若跑在比目标程序更低的级别上，就会「命令都发出去了、画面纹丝不动」，
    是这个插件最难自查的一类故障，所以必须能查到。
    """
    try:
        import win32api
        import win32security
        tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                             win32security.TOKEN_QUERY)
        # 24 = TokenIntegrityLevel。pywin32 返回的是 PyTOKEN_MANDATORY_LABEL，
        # 真正要的 SID 在 .Label.Sid（不同版本偶有差异，逐级回退取）
        info = win32security.GetTokenInformation(tok, 24)
        lab = getattr(info, 'Label', info)
        sid = getattr(lab, 'Sid', lab)
        if isinstance(sid, (tuple, list)):
            sid = sid[0]
        return win32security.ConvertSidToStringSid(sid)     # 形如 S-1-16-12288(High)
    except Exception as e:
        return 'err:%s' % e


def winstation_info():
    """当前进程所在的窗口站与桌面。

    SendInput **只在交互式窗口站 WinSta0\\Default 上生效**。计划任务若以非交互方式
    启动，进程会被放进别的窗口站，此时抓屏照样能出画面（GDI 读的是桌面 DC），
    但注入完全无效——「画面正常、点击毫无反应」正是这个组合。
    """
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32

        def _name(h):
            buf = ctypes.create_unicode_buffer(256)
            n = ctypes.c_ulong(0)
            if u.GetUserObjectInformationW(h, 2, buf, 512, ctypes.byref(n)):
                return buf.value
            return '?'

        return {
            'winsta': _name(u.GetProcessWindowStation()),
            'desktop': _name(u.GetThreadDesktop(k.GetCurrentThreadId())),
        }
    except Exception as e:
        return {'err': str(e)}


def encode_frame(scale, quality, gray, still_thr=STILL_THRESHOLD, cursor=True, rect=None):
    # rect 非 None 时只抓/只编码这一块：指纹也随之只覆盖该区域，
    # 静止检测天然变成「只检测这个区域的变化」，无需改动 _is_still。
    _STATE.screen.cursor_on = cursor
    img = _STATE.screen.grab(rect)
    sig = _signature(img)
    rk = tuple(rect) if rect else None
    # 缓存 key 必须带上 rect：否则 ROI 移动后会拿旧区域的帧冒充新区域
    key = (round(scale, 3), int(quality), bool(gray), bool(cursor), rk)
    # 画面没动且参数没变 → 复用上一帧，连 JPEG 编码都省掉；
    # hash 不变，后端据此跳过推帧（省掉这一帧的全部流量）
    if _last['data'] is not None and _last['key'] == key and _is_still(sig, still_thr):
        return _last['data'], _last['hash'], rk
    if gray:
        img = img.convert('L')            # 单通道 JPEG：比转 RGB 再存省约三成
    if scale and scale < 0.999:
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        img = img.resize((w, h), 3)       # 3 = Image.LANCZOS
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=int(quality), optimize=False)
    data = buf.getvalue()
    h = hashlib.md5(data).hexdigest()
    _last.update({'data': data, 'hash': h, 'sig': sig, 'key': key})
    return data, h, rk


def parse_rect(qs, screen):
    """解析 ROI 矩形（虚拟屏坐标）。

    缺参 / 非法 / 几乎覆盖全屏 → 返回 None（退化为原来的整帧路径）。
    返回 None 的情况绝不能报错，否则老客户端或未上报视口时会整条流挂掉。
    """
    try:
        rx = int(float(qs.get('rx', '')))
        ry = int(float(qs.get('ry', '')))
        rw = int(float(qs.get('rw', '')))
        rh = int(float(qs.get('rh', '')))
    except Exception:
        return None
    if rw <= 0 or rh <= 0:
        return None
    sw, sh = screen.width, screen.height
    if rx < 0:
        rx = 0
    if ry < 0:
        ry = 0
    if rx >= sw or ry >= sh:
        return None
    rw = min(rw, sw - rx)
    rh = min(rh, sh - ry)
    if rw < 8 or rh < 8:              # 太小没有意义，也避免建 1px 位图
        return None
    if rw * rh >= sw * sh * 0.92:     # 几乎全屏：直接整帧，省掉 ROI 的额外位图与判断
        return None
    return (rx, ry, rw, rh)


class Handler(BaseHTTPRequestHandler):
    server_version = 'DboxRemoteAgent/1.0'

    def log_message(self, fmt, *args):
        pass    # 静默：agent 常驻后台，写日志到 stdout 无意义（pythonw 下还没有控制台）

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        qs = {}
        if '?' in self.path:
            from urllib.parse import parse_qs
            qs = {k: v[0] for k, v in parse_qs(self.path.split('?', 1)[1]).items()}
        if path == '/info':
            s = _STATE.screen
            self._json({
                'ok': True,
                'screen': {'w': s.width, 'h': s.height, 'left': s.left, 'top': s.top},
                'pid': os.getpid(),
                'session_id': _STATE.session_id(),
                'started_at': _STATE.started_at,
                'uptime': round(time.time() - _STATE.started_at, 1),
            })
            return
        if path == '/cursor':
            try:
                import win32gui
                x, y = win32gui.GetCursorPos()
                self._json({'ok': True, 'x': x, 'y': y})
            except Exception as e:
                self._json({'ok': False, 'err': str(e)})
            return
        if path == '/diag':
            info = {
                'ok': True, 'pid': os.getpid(),
                'integrity': integrity_level(),
                'winstation': winstation_info(),
                'screen': {'w': _STATE.screen.width, 'h': _STATE.screen.height},
            }
            try:
                import win32ts
                info['session'] = win32ts.ProcessIdToSessionId(os.getpid())
            except Exception:
                info['session'] = '?'
            # 标定 SetCursorPos 的坐标系：发若干坐标，读回 GetCursorPos（无外部干扰）
            try:
                import win32gui, time as _t, ctypes as _ct
                u = _ct.windll.user32
                probes = []
                for (sx, sy) in [(500, 300), (1000, 540), (1536, 864), (1920, 1080)]:
                    u.SetCursorPos(int(sx), int(sy))
                    _t.sleep(0.12)
                    probes.append({'set': [sx, sy], 'got': list(win32gui.GetCursorPos())})
                info['cursorprobe'] = probes
            except Exception as e:
                info['cursorprobe_err'] = str(e)
            # 实测一次 SendInput：移到鼠标**当前**位置，不改变任何东西，只为取返回值
            try:
                import win32gui
                x, y = win32gui.GetCursorPos()
                inj = _STATE.input
                nx, ny = inj._abs(x, y)
                one = inj._mi(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)
                arr = (inj.INPUT * 1)(one)
                info['sendinput_ret'] = inj._u.SendInput(
                    1, arr, ctypes.sizeof(inj.INPUT))
                info['sendinput_err'] = ctypes.GetLastError()
            except Exception as e:
                info['sendinput_error'] = str(e)
            self._json(info)
            return
        if path == '/frame':
            try:
                scale = float(qs.get('scale', 1.0))
                quality = int(qs.get('q', 60))
                gray = str(qs.get('gray', '0')) in ('1', 'true', 'yes')
                cursor = str(qs.get('cursor', '1')) in ('1', 'true', 'yes')
                still_thr = float(qs.get('still_thr', STILL_THRESHOLD))
            except Exception:
                scale, quality, gray, cursor, still_thr = 1.0, 60, False, True, STILL_THRESHOLD
            scale = max(0.2, min(1.0, scale))
            quality = max(10, min(95, quality))
            still_thr = max(0.0, min(64.0, still_thr))
            rect = parse_rect(qs, _STATE.screen)
            try:
                data, fhash, srect = encode_frame(scale, quality, gray, still_thr, cursor, rect)
            except Exception as e:
                self._json({'ok': False, 'error': 'grab failed: %s' % e}, 500)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Frame-Hash', fhash)
            if srect:
                # 实际服务的区域（可能被夹取过）：客户端必须按这个值定位补丁，不能用自己上报的
                self.send_header('X-Frame-Rect', '%d,%d,%d,%d' % srect)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({'ok': False, 'error': 'not found'}, 404)

    def do_POST(self):
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        if path != '/input':
            self._json({'ok': False, 'error': 'not found'}, 404)
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n) if n > 0 else b'{}'
            ev = json.loads(raw.decode('utf-8', 'replace'))
        except Exception as e:
            self._json({'ok': False, 'error': 'bad json: %s' % e}, 400)
            return
        # 支持数组：后端的高频输入（滚轮 30ms 节流 ≈ 33 次/秒）可以整批一次送达，
        # 省掉逐条的 HTTP 往返。旧版后端逐条发单事件时行为不变。
        try:
            if isinstance(ev, list):
                for one in ev:
                    _STATE.input.dispatch(one)
            else:
                _STATE.input.dispatch(ev)
            self._json({'ok': True})
        except Exception as e:
            self._json({'ok': False, 'error': str(e)}, 400)


def main():
    global _STATE
    ap = argparse.ArgumentParser(description='Dbox 远程桌面代理')
    ap.add_argument('--port', type=int, default=AGENT_PORT)
    ap.add_argument('--probe', action='store_true', help='只探测是否已有实例在跑')
    args = ap.parse_args()

    import urllib.request
    if args.probe:
        try:
            with urllib.request.urlopen('http://%s:%d/info' % (AGENT_HOST, args.port),
                                        timeout=1.5) as r:
                sys.stdout.write(r.read().decode('utf-8', 'replace'))
            return 0
        except Exception:
            return 1

    _STATE = AgentState()
    try:
        srv = ThreadingHTTPServer((AGENT_HOST, args.port), Handler)
    except OSError:
        # 端口被占 = 已有实例在服务，直接退出，不抢
        sys.stderr.write('agent already running on port %d\n' % args.port)
        return 2
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
