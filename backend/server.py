# -*- coding: utf-8 -*-
"""远程桌面插件后端。

职责边界很明确：**本进程（Windows 服务，Session 0）不抓屏、不注入输入**——Windows 把
服务隔离在没有桌面的 Session 0，抓出来是空的。真正的采集与注入在用户会话里的
agent（agent/desktop_agent.py）完成，这里只做三件事：鉴权、转发、把单帧拼成 MJPEG 流。

端点：
  GET  /status              代理是否在线、屏幕尺寸、自启状态
  GET  /stream               MJPEG 流（?token= 鉴权，<img> 不支持自定义请求头）
  POST /input               转发键鼠事件到代理
  POST /agent/install|uninstall|start   管理开机自启
"""

import os
import io
import sys
import time
import json
import threading
import urllib.request
import urllib.error

from flask import Blueprint, request, jsonify, Response, g

try:
    from . import autostart
except Exception:                       # 兼容按文件路径直接加载的情形
    import autostart

AGENT_HOST = '127.0.0.1'
AGENT_PORT = 18921
AGENT_TIMEOUT = 5.0

# 三档预设：帧率 / JPEG 质量 / 分辨率缩放 / 灰度 / 画面静止时跳过推帧
PRESETS = {
    'smooth':   dict(fps=15, quality=72, scale=1.0,  gray=False, skip_still=False),
    'balanced': dict(fps=8,  quality=60, scale=1.0,  gray=False, skip_still=True),
    'saver':    dict(fps=3,  quality=42, scale=0.7,  gray=False, skip_still=True),
}
PLACEHOLDER_CACHE = {}

# 用户有输入后的「活跃窗口」：期间强制推帧、且让代理跳过静止检测，保证画图/拖拽实时可见。
# 超过窗口无输入，再恢复静止跳过以省流量。
ACTIVE_WINDOW = 2.0
_last_input_ts = 0.0


def _agent_url(path):
    return 'http://%s:%d%s' % (AGENT_HOST, AGENT_PORT, path)


def _agent_get(path, timeout=AGENT_TIMEOUT):
    req = urllib.request.Request(_agent_url(path))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers), r.status


def _placeholder_jpeg(text='桌面代理未连接'):
    """代理掉线时的占位帧。

    这里**不让流中断**：流一断，前端 <img> 就会 error 并触发重连风暴；持续喂占位帧
    能保持连接，等代理回来自然恢复。占位帧降到 1fps，几乎不吃流量。
    """
    cached = PLACEHOLDER_CACHE.get(text)
    if cached:
        return cached
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGB', (800, 450), (22, 27, 34))
        d = ImageDraw.Draw(img)
        font = None
        for fp in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf',
                   r'C:\Windows\Fonts\arial.ttf'):
            if os.path.isfile(fp):
                try:
                    font = ImageFont.truetype(fp, 22)
                    break
                except Exception:
                    continue
        tw = d.textlength(text, font=font) if font else len(text) * 11
        d.text(((800 - tw) / 2, 215), text, fill=(150, 160, 172), font=font)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=45)
        data = buf.getvalue()
    except Exception:
        data = b''
    PLACEHOLDER_CACHE[text] = data
    return data


def _preset(name):
    return PRESETS.get(str(name or '').lower()) or PRESETS['balanced']


def create_blueprint(host):
    bp = Blueprint('ext_remote', __name__)
    logger = host.logger

    def _stream_auth():
        """/stream 用 <img> 加载，带不了 Authorization 头，故支持 ?token=。"""
        token = (request.args.get('token') or '').strip()
        if not token:
            auth = request.headers.get('Authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else auth
        user = host.auth_user(token) if token else None
        if user is None:
            return None
        g.user_id, g.role, g.username = user
        return user

    @bp.route('/status', methods=['GET'])
    @host.login_required
    def status():
        online = False
        info = None
        err = ''
        try:
            body, _, _ = _agent_get('/info', timeout=2.0)
            info = json.loads(body.decode('utf-8', 'replace'))
            online = bool(info.get('ok'))
        except urllib.error.URLError:
            err = '桌面代理未运行'
        except Exception as e:
            err = '桌面代理不可达: %s' % e
        try:
            installed = autostart.is_installed()
        except Exception:
            installed = False
        return jsonify({
            'success': True,
            'online': online,
            'info': info,
            'error': err,
            'autostart': installed,
            'presets': PRESETS,
            'agent': {'host': AGENT_HOST, 'port': AGENT_PORT},
        })

    @bp.route('/stream', methods=['GET'])
    def stream():
        if _stream_auth() is None:
            return jsonify({'success': False, 'message': '未授权'}), 401
        preset = _preset(request.args.get('preset'))

        def _num(name, default, lo, hi, cast):
            v = request.args.get(name)
            if v is None or str(v).strip() == '':
                return default
            try:
                return max(lo, min(hi, cast(v)))
            except Exception:
                return default

        fps = _num('fps', preset['fps'], 1, 30, float)
        quality = _num('q', preset['quality'], 10, 95, int)
        scale = _num('scale', preset['scale'], 0.3, 1.0, float)
        gray = (str(request.args.get('gray',
                    '1' if preset['gray'] else '0')).lower() in ('1', 'true', 'yes'))
        skip_still = (str(request.args.get('still',
                          '1' if preset['skip_still'] else '0')).lower()
                      in ('1', 'true', 'yes'))
        interval = 1.0 / max(0.5, fps)

        def gen():
            last_hash = None
            miss = 0
            try:
                while True:
                    # 活跃窗口内：强制推帧，且让代理跳过静止检测（still_thr=0），
                    # 这样画图/拖拽时即使只是细线变化也能实时刷新，不必切走再切回。
                    active = (time.time() - _last_input_ts) < ACTIVE_WINDOW
                    data = None
                    fhash = 'offline'
                    try:
                        parts = ['scale=%.3f' % scale, 'q=%d' % quality,
                                 'gray=%s' % ('1' if gray else '0')]
                        if active:
                            parts.append('still_thr=0')
                        q = '/frame?' + '&'.join(parts)
                        body, headers, _ = _agent_get(q, timeout=max(2.0, interval * 3))
                        data = body
                        fhash = headers.get('X-Frame-Hash') or 'f%d' % time.time()
                        miss = 0
                    except Exception:
                        miss += 1
                        # 连续失败才降级为占位帧：偶发单帧抓取失败不该让用户看到闪烁
                        if miss >= 3:
                            data = _placeholder_jpeg()
                            fhash = 'offline'
                    if data:
                        if skip_still and not active and fhash == last_hash:
                            time.sleep(interval)
                            continue
                        last_hash = fhash
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n'
                               b'Content-Length: ' + str(len(data)).encode() +
                               b'\r\n\r\n' + data + b'\r\n')
                    # 掉线时把节奏放慢，别空转烧 CPU，也别用占位帧刷流量
                    time.sleep(interval if fhash != 'offline' else 1.0)
            except GeneratorExit:
                return

        return Response(gen(),
                        mimetype='multipart/x-mixed-replace; boundary=frame',
                        headers={'Cache-Control': 'no-store',
                                 'X-Accel-Buffering': 'no'})

    @bp.route('/input', methods=['POST'])
    @host.login_required
    def input_ep():
        payload = request.get_json(force=True, silent=True) or {}
        events = payload if isinstance(payload, list) else [payload]
        sent = 0
        last_err = None
        for ev in events:
            if not isinstance(ev, dict) or not ev.get('type'):
                continue
            try:
                req = urllib.request.Request(
                    _agent_url('/input'),
                    data=json.dumps(ev).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST')
                with urllib.request.urlopen(req, timeout=3.0) as r:
                    r.read()
                sent += 1
            except Exception as e:
                last_err = str(e)
                break
        if last_err and sent == 0:
            return jsonify({'success': False, 'message': '桌面代理未响应: %s' % last_err}), 502
        # 记录最近一次输入时间，流生成器据此在「活跃窗口」内强制推帧（实时可见）。
        global _last_input_ts
        _last_input_ts = time.time()
        return jsonify({'success': True, 'sent': sent, 'error': last_err})

    @bp.route('/agent/install', methods=['POST'])
    @host.login_required
    def agent_install():
        ok, msg = autostart.install()
        if ok:
            autostart.start_now()
        try:
            logger.info('远程桌面：安装自启 %s (%s)', ok, msg)
        except Exception:
            pass
        return jsonify({'success': ok, 'message': msg,
                        'autostart': autostart.is_installed()})

    @bp.route('/agent/uninstall', methods=['POST'])
    @host.login_required
    def agent_uninstall():
        ok, msg = autostart.uninstall()
        return jsonify({'success': ok, 'message': msg,
                        'autostart': autostart.is_installed()})

    @bp.route('/agent/start', methods=['POST'])
    @host.login_required
    def agent_start():
        ok, msg = autostart.start_now()
        return jsonify({'success': ok, 'message': msg})

    return bp
