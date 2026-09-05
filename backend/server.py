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

# 各用户「当前正在看的屏幕区域」（ROI，虚拟屏坐标），由前端 POST /viewport 上报。
# 单独开接口而不是塞进 /stream 参数：平移/缩放是连续的，不可能每次变化都重启流。
_viewports = {}
# 各用户运行时画质配置（分辨率/质量/fps/灰度/静止/光标），由前端 POST /cfg 热更新；
# /stream 生成器逐拍读取，改这些参数不再需要重建流。
_stream_cfg = {}


# ---------------------------------------------------------------- 输入 WebSocket 服务
# 根治高频输入（滚轮/拖拽）的排队卡顿：HTTP 下**每个**输入事件都是一次 POST，要
# 浏览器 → 主服务 → 扩展宿主 → 代理 三跳；滚轮 30ms 节流就是 33 req/s，手机网络下
# RTT 几十毫秒 + 同域并发上限排队 → 输入延迟雪崩（「滚了半天画面才动」）。
# WS 一次握手后每个事件只是一个帧，实测 50 条仅 3.3ms（0.065ms/条），比 HTTP 快 2~3 个数量级。
#
# 两个必须绕开的坑（均已实测确认）：
#  1. 不能走主服务代理：_proxy_to_extensions_host 是普通 HTTP 转发，不处理 Upgrade: websocket。
#  2. 不能挂在 Flask 路由上：Werkzeug 3.1.6 在 HTTP 解析层就拒绝 Upgrade 请求（400，路由不执行）。
# 故用 wsproto 在**独立端口 + 独立线程**起 WS 服务，与现有 WSGI 服务互不影响。
INPUT_WS_PORT_BASE = 8094
_input_ws_port = 0          # 实际监听端口，由 /status 下发给前端


def _ws_handle(conn, auth_user):
    """单个 WS 连接的处理循环。"""
    from wsproto import WSConnection, ConnectionType
    from wsproto.events import (Request, AcceptConnection, TextMessage,
                                CloseConnection, RejectConnection, Ping, Pong)
    ws = WSConnection(ConnectionType.SERVER)
    authed = False
    try:
        conn.settimeout(300)
        while True:
            data = conn.recv(65536)
            if not data:
                break
            ws.receive_data(data)
            for ev in ws.events():
                if isinstance(ev, Request):
                    # 鉴权：WS 握手不能带自定义请求头，token 只能走 query（同 /stream?token=）
                    tok = ''
                    try:
                        from urllib.parse import urlparse, parse_qs
                        tok = (parse_qs(urlparse(ev.target).query).get('token') or [''])[0]
                    except Exception:
                        tok = ''
                    if not tok or not auth_user(tok):
                        try:
                            conn.sendall(ws.send(RejectConnection()))
                        except Exception:
                            pass
                        return
                    conn.sendall(ws.send(AcceptConnection()))
                    authed = True
                elif isinstance(ev, TextMessage):
                    if not authed:
                        return
                    try:
                        payload = json.loads(ev.data)
                    except Exception:
                        continue
                    events = payload if isinstance(payload, list) else [payload]
                    events = [e for e in events if isinstance(e, dict) and e.get('type')]
                    if events:
                        _agent_post_input_impl(events)
                        _touch_input()
                elif isinstance(ev, Ping):
                    try:
                        conn.sendall(ws.send(Pong(payload=ev.payload)))
                    except Exception:
                        pass
                elif isinstance(ev, CloseConnection):
                    try:
                        conn.sendall(ws.send(ev.response()))
                    except Exception:
                        pass
                    return
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _start_input_ws(auth_user):
    """启动输入用的 WebSocket 服务（独立线程 + 独立端口）。失败静默降级：前端仍走 HTTP。"""
    global _input_ws_port
    if _input_ws_port:
        return
    try:
        from wsproto import WSConnection            # noqa: F401  仅用于确认依赖可用
    except Exception:
        return
    import socket
    srv = None
    port = INPUT_WS_PORT_BASE
    for _ in range(12):                             # 端口被占就顺延，最多试 12 个
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('0.0.0.0', port))
            srv.listen(32)
            break
        except Exception:
            try:
                srv.close()
            except Exception:
                pass
            srv = None
            port += 1
    if srv is None:
        return
    _input_ws_port = port

    def _accept_loop():
        while True:
            try:
                c, _a = srv.accept()
            except Exception:
                return
            threading.Thread(target=_ws_handle, args=(c, auth_user), daemon=True).start()

    threading.Thread(target=_accept_loop, daemon=True).start()


def _agent_post_input_impl(events):
    """把输入事件转发给代理，返回 (sent, last_err)。HTTP 与 WebSocket 两条通道共用。

    优先「一次 POST 整个数组」：新版代理的 /input 接受数组，高频输入（滚轮 30ms 节流
    就是 33 次/秒）下能显著减少本机往返。旧版代理不认数组会返回 400，此时退回逐条转发，
    保证 agent 没重启时输入也不会整体失效。
    """
    try:
        req = urllib.request.Request(
            _agent_url('/input'),
            data=json.dumps(events).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=3.0) as r:
            r.read()
        return len(events), None
    except urllib.error.HTTPError:
        pass                                   # 多半是旧版代理不认数组 → 逐条重试
    except Exception as e:
        return 0, str(e)
    sent = 0
    last_err = None
    for ev in events:
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
    return sent, last_err


def _touch_input():
    """记录最近一次输入时间。流生成器据此在「活跃窗口」内强制推帧、并让代理跳过
    静止检测，保证画图/拖拽实时可见。HTTP 与 WebSocket 两条输入通道都必须调用。"""
    global _last_input_ts
    _last_input_ts = time.time()


def _clamp(v, lo, hi, default, cast=float):
    """通用区间裁剪：越界或不可转换时退回 default（与 _num 同语义，但参数直传）。"""
    try:
        return max(lo, min(hi, cast(v)))
    except Exception:
        return default


def _parse_agent_rect(v):
    """解析代理回的 X-Frame-Rect: x,y,w,h；缺失或非法返回 None（表示整帧）。"""
    try:
        parts = [int(x) for x in str(v).split(',')]
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            return tuple(parts)
    except Exception:
        pass
    return None


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
        # 下发输入用 WebSocket 的端口（独立 WS 服务，非本 WSGI 端口）。
        # 为 0 表示服务未起来（缺依赖/端口全被占），前端会走 HTTP 降级。
        wport = _input_ws_port
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
            'ws_port': wport,
        })

    @bp.route('/stream', methods=['GET'])
    def stream():
        if _stream_auth() is None:
            return jsonify({'success': False, 'message': '未授权'}), 401
        # uid 必须**在请求上下文里**取，不能放进 gen()：
        # 流式响应的生成器被消费时上下文可能已弹出，此时访问 g 抛的是 RuntimeError，
        # 而 getattr 的默认值只吞 AttributeError——整条流会被打成 500，
        # 客户端除了「重连中」什么也看不到。
        uid = getattr(g, 'user_id', None)
        preset = _preset(request.args.get('preset'))

        def gen():
            miss = 0
            first = True
            last_sig = None
            try:
                while True:
                    # 参数热更新：逐拍从 per-uid 运行时配置读取，改变分辨率/质量等不再
                    # 需要重建流。分辨率(scale)或 ROI 变化会重建前端画布坐标系，必须下一
                    # 拍强制关键帧，否则差分补丁会贴到旧尺寸画布上 → 黑框/错位。
                    live = _stream_cfg.get(uid) or {}
                    fps = _clamp(live.get('fps', preset['fps']), 1, 30, preset['fps'], float)
                    quality = _clamp(live.get('q', preset['quality']), 10, 95, preset['quality'], int)
                    scale = _clamp(live.get('scale', preset['scale']), 0.3, 1.0, preset['scale'], float)
                    gray = bool(live.get('gray', preset['gray']))
                    cursor = bool(live.get('cursor', True))
                    skip_still = bool(live.get('still', preset['skip_still']))
                    interval = 1.0 / max(0.5, fps)
                    # 活跃窗口内：强制推帧（still_thr=0），保证画图/拖拽实时可见
                    active = (time.time() - _last_input_ts) < ACTIVE_WINDOW
                    rect = _viewports.get(uid)          # 用户当前视口（ROI），可能为 None
                    # ROI 模式强制原生分辨率：区域本来就小，再降采样只会白白变糊
                    agent_scale = 1.0 if rect else scale
                    sig = (round(agent_scale, 3), rect)  # 影响画布坐标系的参数签名
                    parts = ['scale=%.3f' % agent_scale, 'q=%d' % quality,
                             'gray=%s' % ('1' if gray else '0'),
                             'cursor=%s' % ('1' if cursor else '0'),
                             'delta=1']
                    if first or sig != last_sig:
                        # 首拍、或分辨率/ROI 变化：强制关键帧重建画布坐标系，杜绝黑框
                        parts.append('kf=1')
                        first = False
                    last_sig = sig
                    if active:
                        parts.append('still_thr=0')
                    if rect:
                        parts.append('rx=%d' % rect[0])
                        parts.append('ry=%d' % rect[1])
                        parts.append('rw=%d' % rect[2])
                        parts.append('rh=%d' % rect[3])
                    # 整帧请求必须无条件发出：ROI 只是附加 rx/ry/rw/rh 参数，不能把请求
                    # 关在 if rect 里——否则默认（非 ROI）模式下整帧请求被跳过，前端永远
                    # 等不到首帧、卡在「正在连接」。
                    req = urllib.request.Request(_agent_url('/frame?' + '&'.join(parts)))
                    try:
                        with urllib.request.urlopen(req, timeout=max(2.0, interval * 3)) as resp:
                            if resp.headers.get('X-Frame-Empty') == '1':
                                miss = 0
                                time.sleep(interval)
                                continue
                            data = resp.read()
                            miss = 0
                    except Exception:
                        miss += 1
                        # 连续失败才降级为占位帧：偶发单帧抓取失败不该让用户看到闪烁
                        if miss >= 3:
                            ph = _placeholder_jpeg()
                            data = (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                                    + str(len(ph)).encode() + b'\r\n\r\n' + ph + b'\r\n')
                        else:
                            time.sleep(interval)
                            continue
                    if data:
                        yield data
                    # 掉线时把节奏放慢，别空转烧 CPU，也别用占位帧刷流量
                    time.sleep(interval if miss == 0 else 1.0)
            except GeneratorExit:
                return

        return Response(gen(),
                        mimetype='multipart/x-mixed-replace; boundary=frame',
                        headers={'Cache-Control': 'no-store',
                                 'X-Accel-Buffering': 'no'})

    def _agent_post_input(events):
        return _agent_post_input_impl(events)

    @bp.route('/input', methods=['POST'])
    @host.login_required
    def input_ep():
        payload = request.get_json(force=True, silent=True) or {}
        events = payload if isinstance(payload, list) else [payload]
        events = [e for e in events if isinstance(e, dict) and e.get('type')]
        sent, last_err = (0, None)
        if events:
            sent, last_err = _agent_post_input(events)
        if last_err and sent == 0:
            return jsonify({'success': False, 'message': '桌面代理未响应: %s' % last_err}), 502
        _touch_input()
        return jsonify({'success': True, 'sent': sent, 'error': last_err})

    # 输入通道的 WebSocket 由 _start_input_ws() 在独立线程/端口提供（见模块级实现）。
    # 这里**不能**用 Flask 路由 + simple_websocket：实测 Werkzeug 3.1.6 会在 HTTP 解析层
    # 直接拒绝 Upgrade 请求（返回 400，路由根本不执行），握手到不了应用层。
    _start_input_ws(host.auth_user)

    @bp.route('/viewport', methods=['POST'])
    @host.login_required
    def viewport_ep():
        """前端上报「当前正在看的屏幕区域」（ROI，虚拟屏坐标）。

        单独开接口而不是塞进 /stream 的查询参数：平移/缩放是连续的，
        ROI 每帧都在变，不可能每次变化都重启流（重建流会打断正在看的画面）。
        /stream 的生成器每帧读取这里的最新值下发给代理。

        body: {"x":int,"y":int,"w":int,"h":int}；传 {"none":true} 或任一字段非法 → 清除 ROI，退回整帧。
        """
        payload = request.get_json(force=True, silent=True) or {}
        uid = getattr(g, 'user_id', None)
        rect = None
        if not payload.get('none'):
            try:
                x = max(0, int(payload.get('x')))
                y = max(0, int(payload.get('y')))
                w = int(payload.get('w'))
                h = int(payload.get('h'))
                if w > 0 and h > 0:
                    rect = (x, y, w, h)
            except Exception:
                rect = None          # 非法一律退回整帧，绝不让流挂掉
        if rect is None:
            _viewports.pop(uid, None)
        else:
            _viewports[uid] = rect
        return jsonify({'success': True, 'roi': list(rect) if rect else None})

    @bp.route('/cfg', methods=['POST'])
    @host.login_required
    def cfg_ep():
        """前端实时调整画质参数（分辨率/质量/fps/灰度/静止/光标），热更新、不重建流。
        分辨率(scale)或 ROI 变化由 /stream 生成器检测后强制关键帧，避免断流黑框。
        """
        payload = request.get_json(force=True, silent=True) or {}
        uid = getattr(g, 'user_id', None)
        cur = dict(_stream_cfg.get(uid) or {})
        B = PRESETS['balanced']
        if 'fps' in payload:
            cur['fps'] = _clamp(payload.get('fps'), 1, 30, B['fps'], float)
        if 'q' in payload:
            cur['q'] = _clamp(payload.get('q'), 10, 95, B['quality'], int)
        if 'scale' in payload:
            # 前端传百分比(30-100)，内部存 0.3-1.0，与 /stream 的 scale 口径一致
            cur['scale'] = _clamp(payload.get('scale'), 30, 100, 100, float) / 100.0
        if 'gray' in payload:
            cur['gray'] = bool(payload.get('gray'))
        if 'cursor' in payload:
            cur['cursor'] = bool(payload.get('cursor'))
        if 'still' in payload:
            cur['still'] = bool(payload.get('still'))
        if 'delta' in payload:
            cur['delta'] = bool(payload.get('delta'))
        _stream_cfg[uid] = cur
        return jsonify({'success': True, 'cfg': {
            'scale': round(cur.get('scale', 1.0) * 100),
            'q': cur.get('q'), 'fps': cur.get('fps'),
            'gray': cur.get('gray'), 'cursor': cur.get('cursor'),
            'still': cur.get('still'), 'delta': cur.get('delta')}})

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
