# -*- coding: utf-8 -*-
"""桌面代理的「会话守护」：开机即就绪，登录/锁屏/注销自动跟随，全程无需人手点按。

为什么必须有这一层（也是用户被锁在外面的根因）：
  agent 要出画面、要能注入键鼠，就必须跑在**当前真正显示画面的那个桌面**上——
  GDI 抓的是当前桌面的 DC，SendInput 也只作用于当前桌面。原实现用「登录时触发」
  的计划任务启动它，于是：
    · 必须有人登录才有画面 → 重启后停在登录界面 = 完全连不上，人被锁在门外；
    · 「立即启动」走 schtasks /run，拿到的令牌与登录会话不等价，画面出得来、
      点击没反应，于是要点好几次才碰巧成功。

而 dbox-extensions 本身就是 AUTO_START 的 LocalSystem 服务——开机即运行，早于任何
登录。守护就该长在这里：它盯着控制台会话，把 agent 投递到当前正在显示的桌面：

    已登录          → WinSta0\\Default
    登录界面 / 锁屏  → WinSta0\\Winlogon   （安全桌面；「重启后仍能连」的关键）

令牌怎么来：取服务自己的 SYSTEM 令牌，把 TokenSessionId 改成目标会话后派生子进程
（需 SeTcbPrivilege，LocalSystem 正好有）。不用 WTSQueryUserToken——它在没人登录时
直接失败，正是「登录界面连不上」的死穴。由 SYSTEM 令牌派生的进程是最高完整性级别，
也不受 UIPI 限制（低完整性进程发出的输入会被丢弃，表现为「命令发了、画面不动」）。
"""

import os
import time
import json
import threading
import urllib.request

try:
    from . import autostart
except Exception:                       # 兼容按文件路径直接加载的情形
    import autostart

AGENT_PORT = 18921
DESK_DEFAULT = 'WinSta0\\Default'
DESK_WINLOGON = 'WinSta0\\Winlogon'
_NO_SESSION = 0xFFFFFFFF

TICK = 5.0                 # 常态巡检间隔
# 刚拉起后给 agent 的就绪时间。必须覆盖「pythonw 启动 + 导入 Pillow/pywin32 +
# 绑定端口」的全过程，给短了会在它起来之前就判为失败并杀掉（见 _tick 注释）。
RELAUNCH_GRACE = 10.0
BACKOFF_MAX = 60.0         # 连续失败时的最大退避
# 杀掉旧 agent 后等待端口释放的上限。agent 是「端口被占即退出」的单实例，
# 端口没真正释放就拉新的，新进程必然 bind 失败退出（表现＝反复重启都失败）。
PORT_FREE_TIMEOUT = 8.0
PENDING_CHECK_EVERY = 30.0 # 待办自启任务的重试间隔

_lock = threading.Lock()
_state = {
    'data_dir': None,
    'logger': None,
    'running': False,
    'launches': 0,
    'last_launch': 0.0,
    'last_err': '',
    'next_try': 0.0,
    'target': None,
    'kicks': 0,
    'fail_streak': 0,      # 连续失败次数，用于真正的指数退避
    'pending_checked': 0.0,
}


# ---------------------------------------------------------------- 基础工具
def _log(msg):
    lg = _state.get('logger')
    try:
        if lg:
            lg.info('远程桌面守护：%s', msg)
    except Exception:
        pass


def _flag_path():
    d = _state.get('data_dir')
    return os.path.join(d, 'agent_disabled') if d else None


def is_enabled():
    p = _flag_path()
    try:
        return not (p and os.path.isfile(p))
    except Exception:
        return True


def set_enabled(on):
    p = _flag_path()
    try:
        if p:
            if on:
                if os.path.isfile(p):
                    os.remove(p)
            else:
                with open(p, 'w', encoding='utf-8') as f:
                    f.write('1')
    except Exception:
        pass
    return bool(on)


def _enable_privileges():
    """启用改会话 / 调试所需特权（LocalSystem 持有，默认未必开启）。"""
    try:
        import win32api
        import win32security
        h = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY)
        for name in ('SeTcbPrivilege', 'SeDebugPrivilege',
                     'SeAssignPrimaryTokenPrivilege', 'SeIncreaseQuotaPrivilege'):
            try:
                luid = win32security.LookupPrivilegeValue(None, name)
                win32security.AdjustTokenPrivileges(
                    h, False, [(luid, win32security.SE_PRIVILEGE_ENABLED)])
            except Exception:
                continue
    except Exception:
        pass


# ---------------------------------------------------------------- 环境判定
def console_session():
    """当前物理控制台所在的会话；无人值守/取不到时返回 None。"""
    try:
        import win32ts
        s = int(win32ts.WTSGetActiveConsoleSessionId())
    except Exception:
        return None
    if s < 0 or s == _NO_SESSION:
        return None
    return s


def _logonui_running():
    """LogonUI.exe 在跑 = 当前显示的是登录/锁屏界面（安全桌面）。"""
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            try:
                if (p.info.get('name') or '').lower() == 'logonui.exe':
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _user_logged_in(sess):
    """该会话是否已有用户登录（没人登录时 WTSQueryUserToken 直接抛异常）。"""
    try:
        import win32ts
        t = win32ts.WTSQueryUserToken(sess)
        try:
            t.Close()
        except Exception:
            pass
        return True
    except Exception:
        return False


def desired_target():
    """agent 应该在的位置：(session, desktop)；时机未到返回 None。"""
    sess = console_session()
    if sess is None:
        return None
    if _logonui_running() or not _user_logged_in(sess):
        return (sess, DESK_WINLOGON)
    return (sess, DESK_DEFAULT)


def agent_info():
    """探测本机 agent；不在线返回 None。"""
    try:
        with urllib.request.urlopen(
                'http://127.0.0.1:%d/info' % AGENT_PORT, timeout=1.5) as r:
            d = json.loads(r.read().decode('utf-8', 'replace'))
        return d if d.get('ok') else None
    except Exception:
        return None


def _port_open(port=AGENT_PORT, timeout=0.5):
    """agent 端口是否仍在监听（＝旧实例是否还占着）。"""
    s = None
    try:
        import socket
        s = socket.socket()
        s.settimeout(timeout)
        return s.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def _wait_port_free(timeout=PORT_FREE_TIMEOUT):
    """等旧 agent 真正释放端口，返回是否已空闲。

    这是「反复重启/安装都失败」的根因所在：taskkill 返回 ≠ 端口已释放，
    旧实现只固定 sleep 0.4s 就拉起新进程，而 agent 启动时 bind 失败会**直接退出**
    （desktop_agent.main：OSError → return 2）。于是每次重投都是
    「杀掉 → 端口还没放 → 新的起来就死」，用户看到的便是永远重启不成功。
    """
    end = time.time() + timeout
    while time.time() < end:
        if not _port_open():
            return True
        time.sleep(0.25)
    return not _port_open()


def _in_right_place(info, target):
    """agent 是否已经跑在目标「会话 + 桌面」上。

    桌面名比对只取最后一段（agent 上报的是 'Default' / 'Winlogon'），
    避免 WinSta0 前缀写法差异造成误判、进而每轮都杀掉重投。
    """
    if not info or not target:
        return False
    sess, desk = target
    try:
        if int(info.get('session') or -1) != int(sess):
            return False
    except Exception:
        return False
    want = desk.rsplit('\\', 1)[-1].lower()
    got = str(info.get('desktop') or '').rsplit('\\', 1)[-1].lower()
    if not got:
        # 老版 agent 不上报桌面 → 位置无法判定。**此时绝不能杀**：它正在正常出画面，
        # 杀了就是人为制造断连（实测踩过：新版上线后旧版被误杀、新的又没起来，直接失联）。
        # 按「位置正确」处理，等下次自然重启换上新版本即可。
        return True
    return got == want


# ---------------------------------------------------------------- 投递 agent
def _cmdline():
    return '"%s" "%s"' % (autostart.agent_pythonw(), autostart.agent_script())


# 令牌与进程创建全部走 ctypes 直调 Win32，不用 pywin32 的 DuplicateTokenEx 包装：
# 实测那个包装在 4 参 / 5 参两种写法下一律返回 1346（ERROR_BAD_IMPERSONATION_LEVEL），
# 同样的调用换成 ctypes 后正常。这种「包装层参数错位」最难查，务必保持直调。
import ctypes
from ctypes import wintypes

_advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

_MAXIMUM_ALLOWED = 0x02000000
_TOKEN_ALL_ACCESS = 0x000F01FF
_SECURITY_IMPERSONATION = 2        # SecurityImpersonation
_TOKEN_PRIMARY = 1                 # TokenPrimary
_TOKEN_SESSION_ID = 12             # TokenSessionId
_STARTF_USESHOWWINDOW = 0x00000001


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR),
        ('lpDesktop', wintypes.LPWSTR), ('lpTitle', wintypes.LPWSTR),
        ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
        ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD),
        ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD),
        ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
        ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
        ('lpReserved2', ctypes.c_void_p),
        ('hStdInput', wintypes.HANDLE), ('hStdOutput', wintypes.HANDLE),
        ('hStdError', wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
                ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]


# **必须显式声明签名**：ctypes 默认把返回值当 32 位 int，而 HANDLE 在 64 位进程里是
# 64 位值——GetCurrentProcess() 返回的伪句柄 0xFFFFFFFFFFFFFFFF 会被截成 0xFFFFFFFF，
# 后续 OpenProcessToken / CreateProcessAsUserW 一律报「句柄无效」(WinError 6)。
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
_advapi32.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
_advapi32.SetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD]
_advapi32.CreateProcessAsUserW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                           wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.POINTER(_STARTUPINFO),
                                           ctypes.POINTER(_PROCESS_INFORMATION)]
_advapi32.CreateProcessAsUserW.restype = wintypes.BOOL


def _hval(tok):
    """句柄对象 → 句柄值（HANDLE 取 .value，PyHANDLE 可直接 int）。"""
    v = getattr(tok, 'value', None)
    if isinstance(v, int):
        return v
    try:
        return int(tok)
    except Exception:
        return 0


def _win_err(where):
    try:
        return '%s: %s' % (where, ctypes.WinError(ctypes.get_last_error()))
    except Exception:
        return where


def _dup_system_token_for_session(sess):
    """把本进程（服务，LocalSystem）的令牌复制成主令牌，并把会话号改到目标会话。

    **必须先复制**：直接改本进程令牌的 TokenSessionId 会把整个服务进程挪到别的
    会话，那是灾难。复制出来的句柄随便改，用完关掉即可。
    """
    cur = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(),
                                      _TOKEN_ALL_ACCESS, ctypes.byref(cur)):
        raise RuntimeError(_win_err('OpenProcessToken'))
    try:
        new = wintypes.HANDLE()
        if not _advapi32.DuplicateTokenEx(
                cur, _MAXIMUM_ALLOWED, None, _SECURITY_IMPERSONATION,
                _TOKEN_PRIMARY, ctypes.byref(new)):
            raise RuntimeError(_win_err('DuplicateTokenEx'))
        sid = wintypes.DWORD(int(sess))
        if not _advapi32.SetTokenInformation(new, _TOKEN_SESSION_ID,
                                             ctypes.byref(sid), ctypes.sizeof(sid)):
            _kernel32.CloseHandle(new)
            raise RuntimeError(_win_err('SetTokenInformation(TokenSessionId)'))
        return new.value
    finally:
        try:
            _kernel32.CloseHandle(cur)
        except Exception:
            pass


def _user_token(sess):
    """兜底：该会话已登录用户的令牌（没人登录时不可用）。WTSQueryUserToken 返回的
    已是主令牌，可直接使用，无需再复制。

    **必须返回句柄对象本身，不能 _hval() 成 int 后丢掉对象**：PyHANDLE 析构时自动
    CloseHandle，句柄会被立刻关掉，后续 CreateProcessAsUserW 报「句柄无效」(WinError 6)。
    """
    try:
        import win32ts
        return win32ts.WTSQueryUserToken(sess)
    except Exception:
        return None


def _launch_with(token, desktop, cmd=None):
    """以给定令牌在指定桌面拉起进程，返回 pid。cmd 缺省为 agent 命令行。"""
    h = _hval(token)
    if not h:
        raise RuntimeError('无效令牌')
    si = _STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop
    si.dwFlags = _STARTF_USESHOWWINDOW
    si.wShowWindow = 0                       # SW_HIDE：别在任务栏闪窗口
    pi = _PROCESS_INFORMATION()
    # 命令行必须是可写缓冲：CreateProcessAsUserW 允许就地修改它
    cmd = ctypes.create_unicode_buffer(cmd or _cmdline())
    ok = _advapi32.CreateProcessAsUserW(
        h, None, cmd, None, None, False, 0, None,
        os.path.dirname(autostart.agent_script()),
        ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise RuntimeError(_win_err('CreateProcessAsUserW'))
    try:
        _kernel32.CloseHandle(pi.hThread)
        _kernel32.CloseHandle(pi.hProcess)
    except Exception:
        pass
    return pi.dwProcessId


def launch(sess, desktop):
    """把 agent 投到目标会话 + 桌面。SYSTEM 令牌改写会话是主力（登录前后都成立，
    也是「登录界面能连」的关键）；已登录用户令牌仅作兜底。"""
    errs = []
    tok = 0
    try:
        tok = _dup_system_token_for_session(sess)
        pid = _launch_with(tok, desktop)
        return pid, 'system-in-session'
    except Exception as e:
        errs.append('system-in-session: %s' % e)
    finally:
        if tok:
            try:
                _kernel32.CloseHandle(tok)
            except Exception:
                pass
    # 兜底：登录界面/锁屏没有用户令牌，直接跳过（不是错误）
    if desktop != DESK_WINLOGON:
        utok = _user_token(sess)
        if utok:
            try:
                pid = _launch_with(utok, desktop)
                return pid, 'user-token'
            except Exception as e:
                errs.append('user-token: %s' % e)
        else:
            errs.append('user-token: 该会话没有已登录用户')
    raise RuntimeError('；'.join(errs) or '无可用令牌')


# ---------------------------------------------------------------- 守护主循环
def _tick():
    target = desired_target()
    _state['target'] = [target[0], target[1]] if target else None
    if target is None:
        return                                   # 还没到登录界面/无控制台会话
    info = agent_info()
    if _in_right_place(info, target):
        _state['fail_streak'] = 0                # 位置正确，清掉退避
        return                                   # 已在正确的会话+桌面，别动它
    now = time.time()
    if now < _state['next_try']:
        return
    # 刚拉起过 → 一律给它完整的就绪时间，**不看 info 是否已上报**。
    # 旧写法 `if info and ...` 恰恰在 agent 尚未就绪（info 为 None）时跳过这段保护，
    # 于是刚创建的进程下一轮就被杀掉，陷入「杀—起—杀」风暴：
    # 表现就是「重启后要等很久才碰巧连上」，锁屏/解锁切换时尤其明显。
    if (now - _state['last_launch']) < RELAUNCH_GRACE:
        return
    # 位置不对（切了会话 / 锁屏 / 解锁 / 首次开机）→ 杀掉再投
    try:
        autostart._kill_existing()
    except Exception:
        pass
    # 必须等端口真正释放，而不是固定 sleep：agent 是「端口被占即退出」的单实例
    if not _wait_port_free():
        _state['fail_streak'] += 1
        _state['last_err'] = '旧 agent 未释放端口 %s' % AGENT_PORT
        _state['next_try'] = time.time() + TICK
        _log('投递中止：%s' % _state['last_err'])
        return
    try:
        pid, how = launch(target[0], target[1])
    except Exception as e:
        _state['fail_streak'] += 1
        _state['last_err'] = str(e)
        # 真正的指数退避（旧表达式 (now-last_launch)*0 + TICK 恒等于 TICK，
        # 等于完全没有退避，失败时会以 5s 间隔无脑重试，进一步加剧重启风暴）
        _state['next_try'] = time.time() + min(
            BACKOFF_MAX, TICK * (2 ** min(_state['fail_streak'] - 1, 4)))
        _log('投递失败（session=%s desktop=%s）：%s' % (target[0], target[1], e))
        return
    _state['launches'] += 1
    _state['last_launch'] = time.time()
    _state['last_err'] = ''
    _state['fail_streak'] = 0
    _state['next_try'] = time.time() + RELAUNCH_GRACE
    _log('已投递 pid=%s 到 session=%s desktop=%s（方式：%s）'
         % (pid, target[0], target[1], how))


def _flush_pending_autostart():
    """补建「待办」的登录自启任务。

    场景：用户在登录界面点过「安装」，但当时还没有桌面会话（explorer.exe 未运行），
    任务建不了。守护开机即运行、远早于登录，正好负责在用户真正登录到桌面后补上，
    用户不必再记着回来点一次。
    """
    now = time.time()
    if now - _state['pending_checked'] < PENDING_CHECK_EVERY:
        return
    _state['pending_checked'] = now
    try:
        if not autostart.has_pending():
            return
        if not autostart.interactive_user():
            return                        # 还没登录到桌面，下轮再试
        ok, msg = autostart.install()
        _log('补建自启任务%s：%s' % ('成功' if ok else '失败', msg))
    except Exception as e:
        _log('补建自启任务异常：%s' % e)


def _loop():
    _enable_privileges()
    while True:
        try:
            if is_enabled():
                _tick()
                _flush_pending_autostart()
        except Exception as e:
            _state['last_err'] = str(e)
        time.sleep(TICK)


def kick():
    """立刻重投一次（面板「重启代理」/ 用户点了启动）。清掉退避，下一轮马上执行。"""
    _state['next_try'] = 0.0
    _state['last_launch'] = 0.0
    _state['kicks'] += 1
    target = desired_target()
    if target is None:
        return False, '当前没有可投递的控制台会话（机器可能还未到登录界面）'
    try:
        autostart._kill_existing()
    except Exception:
        pass
    # 同 _tick：等端口真正释放，否则新 agent 一起就因端口被占退出，
    # 用户点「重启代理」看到的永远是失败。
    if not _wait_port_free():
        return False, '旧 agent 未退出，端口 %s 仍被占用' % AGENT_PORT
    try:
        pid, how = launch(target[0], target[1])
    except Exception as e:
        return False, str(e)
    _state['launches'] += 1
    _state['last_launch'] = time.time()
    _state['next_try'] = time.time() + RELAUNCH_GRACE
    _state['last_err'] = ''
    _log('手动重投 pid=%s（%s）' % (pid, how))
    return True, '已投递 pid=%s（%s）' % (pid, how)


def start(data_dir=None, logger=None):
    """启动守护线程（进程内只起一次）。"""
    with _lock:
        if _state['running']:
            return
        _state['data_dir'] = data_dir
        _state['logger'] = logger
        _state['running'] = True
        try:
            autostart.set_data_dir(data_dir)   # 供「待办自启」标记落盘
        except Exception:
            pass
    threading.Thread(target=_loop, daemon=True).start()
    _log('守护已启动（data_dir=%s）' % data_dir)


def snapshot():
    return {
        'enabled': is_enabled(),
        'running': bool(_state['running']),
        'launches': _state['launches'],
        'last_err': _state['last_err'],
        'target': _state['target'],
    }
