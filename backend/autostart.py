# -*- coding: utf-8 -*-
"""桌面代理的开机自启（Windows 计划任务）。

用「登录时触发」的计划任务，而不是服务自启：计划任务跑在**用户会话**里，能拿到桌面；
写成服务则会被放进 Session 0，什么都抓不到——这正是整件事的起因。

任务以最高权限（/rl highest）运行：部分窗口（如以管理员身份启动的程序）需要提权
进程才能注入输入。若用户账户不是管理员，schtasks 会失败，此时回退为不提权。
"""

import os
import sys
import subprocess

TASK_NAME = 'DboxRemoteDesktopAgent'


def agent_pythonw():
    """agent 的解释器：必须是**本 venv** 的 pythonw。

    agent 依赖 Pillow / pywin32，系统 python 里不一定有；用 pythonw 则完全无窗口，
    不会在任务栏闪一个黑框。
    """
    d = os.path.dirname(sys.executable or '')
    candidates = [
        os.path.join(d, 'pythonw.exe'),
        os.path.join(d, 'python.exe'),
        sys.executable or 'python',
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return sys.executable or 'python'


def agent_script():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, 'agent', 'desktop_agent.py')


def _run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace')
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def _quoted_cmd():
    return '"%s" "%s"' % (agent_pythonw(), agent_script())


def is_installed():
    rc, out = _run(['schtasks', '/query', '/tn', TASK_NAME, '/fo', 'LIST'])
    return rc == 0 and TASK_NAME.lower() in (out or '').lower()


def interactive_user():
    """当前已登录到桌面的交互用户（形如 `DZMIN G\\dzming`）。

    服务自身是 LocalSystem，而 schtasks 不指定 /ru 时任务就建给**调用者**——
    那样 agent 会在 Session 0 里随开机启动，等于绕一圈又回到「看不到桌面」。
    所以要显式把任务挂到真正坐在电脑前的那个账户上。

    用 explorer.exe 的属主来判定：资源管理器只在有交互会话时存在，比读注册表
    「最后登录用户」可靠（后者在多用户/远程桌面场景下容易指错人）。
    """
    try:
        import psutil
        for p in psutil.process_iter(['name', 'username']):
            try:
                nm = (p.info.get('name') or '').lower()
                if nm == 'explorer.exe' and p.info.get('username'):
                    return p.info['username']
            except Exception:
                continue
    except Exception:
        pass
    return None


def install():
    """创建「登录时触发」的计划任务（已存在则覆盖）。"""
    if not os.path.isfile(agent_script()):
        return False, '找不到 agent 脚本: %s' % agent_script()
    user = interactive_user()
    if not user:
        return False, ('没有检测到已登录的桌面会话（explorer.exe 未运行），'
                       '无法创建自启任务。请先登录到桌面再点安装。')
    cmd = ['schtasks', '/create', '/tn', TASK_NAME, '/tr', _quoted_cmd(),
           '/sc', 'ONLOGON', '/ru', user, '/f']
    # **/rl HIGHEST 是必需的，不是可选项**。UIPI（用户界面特权隔离）会阻止低完整性
    # 进程向完整性更高的窗口注入输入：标准权限启动的 agent 抓屏完全正常、SendInput
    # 也返回成功，但画面就是纹丝不动（实测连续三个坐标全无反应）。提权后立刻生效。
    # 计划任务场景里提权**不会弹 UAC**——由任务计划程序服务直接提升，无需用户确认，
    # 所以这么做没有交互上的副作用（这一点最初判断错了，曾错误地去掉它）。
    rc, out = _run(cmd + ['/rl', 'HIGHEST', '/it'])
    if rc != 0:                 # 个别系统不接受 /it 与 /ru 同时出现
        rc, out = _run(cmd + ['/rl', 'HIGHEST'])
    if rc != 0:                 # 非管理员账户：退到标准权限（此时输入可能受 UIPI 限制）
        rc, out = _run(cmd)
    if rc != 0:
        return False, (out or '').strip() or 'schtasks 创建失败'
    return True, '已设置为「%s」登录后自动启动（最高权限）' % user


def uninstall():
    rc, out = _run(['schtasks', '/delete', '/tn', TASK_NAME, '/f'])
    if rc != 0 and 'not exist' not in (out or '').lower():
        return False, (out or '').strip() or 'schtasks 删除失败'
    return True, '已取消开机自启'


def launch_in_user_session():
    """用**当前交互用户的令牌**直接启动 agent（需要 SE_TCB_NAME，服务正好是 SYSTEM）。

    为什么不能只靠 schtasks /run：实测即使任务标了「Interactive only」，由
    schtasks /run 触发的进程拿到的令牌仍与用户登录会话的交互令牌不等价——
    抓屏完全正常（GDI 读桌面 DC），但 SendInput 毫无效果，表现就是
    「画面出来了、点击没反应」。而**登录时自动触发**的那一次确实是交互会话，
    所以开机自启继续用计划任务，只有「立即启动」必须走下面这条路。
    """
    try:
        import win32process
        import win32security
        import win32ts
        sess = win32ts.WTSGetActiveConsoleSessionId()
        if sess is None or sess == 0xFFFFFFFF:
            return False, '没有活动的控制台会话'
        tok = win32ts.WTSQueryUserToken(sess)
        primary = win32security.DuplicateTokenEx(
            tok, win32security.MAXIMUM_ALLOWED, None,
            win32security.SecurityImpersonation, win32security.TokenPrimary)
        si = win32process.STARTUPINFO()
        si.dwFlags = win32process.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                       # SW_HIDE：别在任务栏闪窗口
        _h, _t, pid, _tid = win32process.CreateProcessAsUser(
            primary, None, _quoted_cmd(), None, None, 0, 0, None, None, si)
        return True, '已在用户会话启动（pid=%s）' % pid
    except Exception as e:
        return False, 'CreateProcessAsUser 失败：%s' % e


def start_now():
    """立即启动。

    借道任务计划程序，因为任务里登记了 `/ru <用户>`（拿到桌面）与 `/rl HIGHEST`
    （拿到足够的完整性级别注入输入）——两者缺一不可。
    不能直接用 subprocess.Popen：调用方是 LocalSystem，子进程会落在 Session 0。
    """
    rc, out = _run(['schtasks', '/run', '/tn', TASK_NAME])
    if rc == 0:
        return True, '已触发启动，稍等一两秒'
    ok, msg = launch_in_user_session()          # 兜底：用用户令牌直接起
    if ok:
        return True, msg
    return False, '触发失败：%s；用户令牌启动也失败：%s' % ((out or '').strip(), msg)
