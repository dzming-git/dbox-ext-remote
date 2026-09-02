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


def install():
    """创建登录触发的计划任务（已存在则覆盖）。"""
    if not os.path.isfile(agent_script()):
        return False, '找不到 agent 脚本: %s' % agent_script()
    base = ['schtasks', '/create', '/tn', TASK_NAME, '/tr', _quoted_cmd(),
            '/sc', 'ONLOGON', '/f']
    rc, out = _run(base + ['/rl', 'HIGHEST'])
    if rc != 0:      # 非管理员账户：去掉提权再试一次
        rc, out = _run(base)
    if rc != 0:
        return False, (out or '').strip() or 'schtasks 创建失败'
    return True, '已设置为开机自启'


def uninstall():
    rc, out = _run(['schtasks', '/delete', '/tn', TASK_NAME, '/f'])
    if rc != 0 and 'not exist' not in (out or '').lower():
        return False, (out or '').strip() or 'schtasks 删除失败'
    return True, '已取消开机自启'


def start_now():
    """立即触发一次（不必等下次登录）。"""
    rc, out = _run(['schtasks', '/run', '/tn', TASK_NAME])
    if rc != 0:
        # 没装计划任务也能直接拉起，用于临时试用
        try:
            subprocess.Popen([agent_pythonw(), agent_script()],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return True, '已直接启动（未安装自启）'
        except Exception as e:
            return False, '启动失败: %s' % e
    return True, '已触发启动'
