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
    # 不加 /rl HIGHEST：提权会触发 UAC，而非交互场景下没人能点那个弹窗，
    # 任务会静默失败。默认权限足以操作绝大多数窗口。
    rc, out = _run(cmd + ['/it'])
    if rc != 0:                 # 个别系统不接受 /it 与 /ru 组合
        rc, out = _run(cmd)
    if rc != 0:                 # 兜底：不带用户（大概率建给 SYSTEM，会失败，但留个明确报错）
        rc, out = _run(['schtasks', '/create', '/tn', TASK_NAME, '/tr', _quoted_cmd(),
                        '/sc', 'ONLOGON', '/f'])
    if rc != 0:
        return False, (out or '').strip() or 'schtasks 创建失败'
    return True, '已设置为「%s」登录后自动启动' % user


def uninstall():
    rc, out = _run(['schtasks', '/delete', '/tn', TASK_NAME, '/f'])
    if rc != 0 and 'not exist' not in (out or '').lower():
        return False, (out or '').strip() or 'schtasks 删除失败'
    return True, '已取消开机自启'


def start_now():
    """立即触发一次（不必等下次登录）。

    **不能**用 subprocess.Popen 直接拉起 agent：调用方是 LocalSystem 服务，子进程
    依旧落在 Session 0，抓不到桌面，看起来「启动成功」却永远连不上。必须借道任务
    计划程序，由它按任务里登记的用户身份在**用户会话**里启动。
    """
    rc, out = _run(['schtasks', '/run', '/tn', TASK_NAME])
    if rc != 0:
        return False, '触发失败：%s' % ((out or '').strip() or 'schtasks /run 返回 %d' % rc)
    return True, '已触发启动，稍等一两秒'
