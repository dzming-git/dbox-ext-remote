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


_DATA_DIR = None


def set_data_dir(d):
    """由会话守护注入插件数据目录（用于存放「待办自启」标记）。"""
    global _DATA_DIR
    _DATA_DIR = d


def _pending_path():
    return os.path.join(_DATA_DIR, 'agent_autostart_pending') if _DATA_DIR else None


def has_pending():
    p = _pending_path()
    try:
        return bool(p and os.path.isfile(p))
    except Exception:
        return False


def set_pending(on):
    """登记/清除「等登录桌面后自动补建自启任务」。"""
    p = _pending_path()
    try:
        if not p:
            return False
        if on:
            with open(p, 'w', encoding='utf-8') as f:
                f.write('1')
        elif os.path.isfile(p):
            os.remove(p)
        return True
    except Exception:
        return False


def install():
    """创建「登录时触发」的计划任务（已存在则覆盖）。

    没有已登录桌面时（典型：重启后停在登录界面）**不再报失败**——schtasks 的 /ru
    需要一个具体账户，此刻确实建不了；但代理本来就由会话守护在登录后自动拉起，
    用户并不需要手动安装。这里改为登记「待办」，由守护检测到桌面会话后自动补建，
    界面只提示「已安排，登录后自动生效」，避免用户在登录界面反复点安装、反复失败。
    """
    if not os.path.isfile(agent_script()):
        return False, '找不到 agent 脚本: %s' % agent_script()
    user = interactive_user()
    if not user:
        if set_pending(True):
            return True, ('已安排：检测到桌面登录后会自动完成自启设置。'
                          '在此之前代理由会话守护在登录后自动拉起，无需手动安装。')
        return False, ('尚未检测到已登录的桌面会话（explorer.exe 未运行）。'
                       '代理会由会话守护在你登录后自动拉起，无需手动安装；'
                       '若仍要设置开机自启，请登录桌面后再点一次。')
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
    set_pending(False)                    # 真正建成后清掉待办标记
    return True, '已设置为「%s」登录后自动启动（最高权限）' % user


def _kill_existing():
    """杀掉已在运行的旧 agent，确保「重启」真的换上新代码。

    desktop_agent 是单实例（端口被占直接退出）。若旧进程一直占着 18921，
    新的修复代码就永远起不来——表现正是「明明改了、推了，画圆还是一条直线」。
    所以重启前必须先把旧进程按 pid 精确干掉（兜底再按脚本路径匹配）。"""
    try:
        import urllib.request, json
        with urllib.request.urlopen('http://127.0.0.1:18921/info', timeout=1.5) as r:
            info = json.loads(r.read().decode('utf-8', 'replace'))
        pid = info.get('pid')
    except Exception:
        pid = None
    if pid:
        _run(['taskkill', '/f', '/pid', str(pid)])
    # 兜底：/info 不可达但进程仍在（按 cmdline 里的脚本路径匹配）
    try:
        import psutil
        script = os.path.abspath(agent_script()).lower()
        for p in psutil.process_iter(['pid', 'cmdline']):
            try:
                cl = p.info.get('cmdline') or []
                hit = any(script in (a or '').lower() for a in cl)
                if hit and (pid is None or p.info.get('pid') != pid):
                    _run(['taskkill', '/f', '/pid', str(p.info['pid'])])
            except Exception:
                continue
    except Exception:
        pass


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

    启动前先杀掉旧实例：否则旧进程占着 18921 端口，新进程起不来，
    刚推上去的修复（如画圆不再连直线）就永远不生效。
    """
    _kill_existing()
    _run(['schtasks', '/end', '/tn', TASK_NAME])   # 结束可能残留的「运行中」任务实例
    rc, out = _run(['schtasks', '/run', '/tn', TASK_NAME])
    if rc == 0:
        return True, '已重启，稍等一两秒'
    ok, msg = launch_in_user_session()          # 兜底：用用户令牌直接起
    if ok:
        return True, msg
    return False, '触发失败：%s；用户令牌启动也失败：%s' % ((out or '').strip(), msg)
