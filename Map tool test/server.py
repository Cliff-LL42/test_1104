#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRPG 地图工坊 · 本地服务器
========================================
功能：
  1. 把网站文件发布到 http://localhost:8000
  2. 提供「房间」实时同步接口，多人不同网络也能看到同一张地图
     （HTTP 长轮询，只依赖 Python 标准库，不需要安装任何东西）

用法：
  python server.py                  # 默认 8000 端口
  python server.py --port 8080
  python server.py --public-url https://xxx.trycloudflare.com
"""

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 64 * 1024 * 1024          # 背景图最大 64MB（base64 后）
CLIENT_TIMEOUT = 25                  # 秒：超过这个时间没轮询就算离线

DEFAULT_ROOM = ''
PUBLIC_URL = ''
PORT = 8000

mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('image/png', '.png')


# --------------------------------------------------------------------------
# 房间数据
# --------------------------------------------------------------------------
class Room:
    def __init__(self):
        self.cond = threading.Condition()
        self.ver = 0
        self.tokens = {}      # id -> 棋子对象
        self.strokes = {}     # id -> 标注对象
        self.grid = None      # 网格设置
        self.bounds = {'cols': 24, 'rows': 16}   # 地图范围（格）
        self.bg = None        # 背景图元信息 {id, w, h, name}
        self.members = {}     # 客户端 id -> {name, color, role, joined, seen}
        self.host = ''        # 房主 cid
        self.blocked = set()  # 被踢出的 cid
        self.dragging = {}    # token_id -> {cid, name, color, ts}

    def touch(self, cid, name=''):
        """记录一次在线心跳，顺便更新昵称"""
        if not cid or cid in self.blocked:
            return
        now = time.time()
        member = self.members.get(cid)
        if member is None:
            idx = int(hashlib.sha1(cid.encode('utf-8')).hexdigest()[:6], 16) % len(MEMBER_COLORS)
            member = self.members[cid] = {
                'name': name or '玩家',
                'color': MEMBER_COLORS[idx],
                'role': 'editor',
                'joined': now,
                'seen': now,
            }
        member['seen'] = now
        if name:
            member['name'] = name
        self.ensure_host()

    def ensure_host(self):
        """房主离开 / 长时间掉线时，移交给最早在线的成员"""
        now = time.time()
        host_member = self.members.get(self.host)
        if host_member and (now - host_member['seen']) < HOST_TIMEOUT:
            return
        online = [c for c, m in self.members.items()
                  if now - m['seen'] < CLIENT_TIMEOUT]
        if online:
            self.host = online[0]
        elif self.host not in self.members:
            self.host = next(iter(self.members), '')

    def set_role(self, cid, role):
        member = self.members.get(cid)
        if member and role in ('editor', 'viewer') and cid != self.host:
            member['role'] = role
            return True
        return False

    def kick(self, cid):
        if cid and cid != self.host:
            self.blocked.add(cid)
            self.members.pop(cid, None)
            for tid in [k for k, v in self.dragging.items() if v['cid'] == cid]:
                del self.dragging[tid]
            self.ensure_host()
            return True
        return False

    def set_drag(self, cid, token_id):
        """记录“谁在拖动哪个棋子”，token_id 为空表示松手"""
        member = self.members.get(cid)
        for tid in [k for k, v in self.dragging.items() if v['cid'] == cid]:
            if tid != token_id:
                del self.dragging[tid]
        if not token_id:
            return
        now = time.time()
        self.dragging[token_id] = {
            'cid': cid,
            'name': (member or {}).get('name', '玩家'),
            'color': (member or {}).get('color', '#8b9aab'),
            'ts': now,
        }

    def role_of(self, cid):
        if cid == self.host:
            return 'host'
        return (self.members.get(cid) or {}).get('role', 'editor')

    def leave(self, cid):
        self.members.pop(cid, None)
        for tid in [k for k, v in self.dragging.items() if v['cid'] == cid]:
            del self.dragging[tid]
        self.ensure_host()

    def snapshot(self, viewer=''):
        now = time.time()
        members = []
        for cid in list(self.members.keys()):
            member = self.members[cid]
            if now - member['seen'] > MEMBER_TTL:
                del self.members[cid]
                continue
            members.append({
                'id': cid,
                'name': member['name'],
                'color': member['color'],
                'role': 'host' if cid == self.host else member['role'],
                'online': (now - member['seen']) < CLIENT_TIMEOUT,
            })
        members.sort(key=lambda m: (m['role'] != 'host', not m['online'], m['name'] or ''))
        dragging = [
            dict(tokenId=tid, cid=v['cid'], name=v['name'], color=v['color'])
            for tid, v in self.dragging.items()
            if now - v['ts'] < 12
        ]
        return {
            'ver': self.ver,
            'tokens': list(self.tokens.values()),
            'strokes': list(self.strokes.values()),
            'grid': self.grid,
            'bounds': self.bounds,
            'bg': self.bg,
            'users': sum(1 for m in members if m['online']),
            'members': members,
            'host': self.host,
            'dragging': dragging,
            'you': {
                'cid': viewer,
                'role': self.role_of(viewer) if viewer else 'editor',
                'kicked': viewer in self.blocked,
            },
        }

    def apply(self, ops):
        """把客户端的增量合并进来（后写入者生效）"""
        for item in ops.get('tokensUpsert') or []:
            if isinstance(item, dict) and item.get('id'):
                self.tokens[item['id']] = item
        for tid in ops.get('tokensDel') or []:
            self.tokens.pop(tid, None)
        for item in ops.get('strokesUpsert') or []:
            if isinstance(item, dict) and item.get('id'):
                self.strokes[item['id']] = item
        for sid in ops.get('strokesDel') or []:
            self.strokes.pop(sid, None)
        if isinstance(ops.get('grid'), dict):
            self.grid = ops['grid']
        if isinstance(ops.get('bounds'), dict):
            cols = int(ops['bounds'].get('cols') or 0)
            rows = int(ops['bounds'].get('rows') or 0)
            if 4 <= cols <= 200 and 4 <= rows <= 200:
                self.bounds = {'cols': cols, 'rows': rows}
        if isinstance(ops.get('bg'), dict) and ops['bg'].get('id'):
            self.bg = ops['bg']
        if ops.get('bgClear'):
            self.bg = None
        self.ver += 1
        self.cond.notify_all()


ROOMS = {}
ROOMS_LOCK = threading.Lock()
BG_STORE = {}          # id -> {data, w, h, name}

MEMBER_COLORS = ['#ff6b6b', '#4aa8ff', '#3ddc97', '#ffd166',
                 '#c77dff', '#ff9f68', '#5ee7df', '#a0e57c']
MEMBER_TTL = 300       # 秒：离线超过 5 分钟从列表里移除
HOST_TIMEOUT = 60      # 秒：房主掉线超过这个时间，房主自动移交给其他在线成员


def get_room(name):
    with ROOMS_LOCK:
        room = ROOMS.get(name)
        if room is None:
            room = ROOMS[name] = Room()
        return room


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if not ip.startswith('127.') and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = 'TRPGMap/2.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具方法 ----------
    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def send_text(self, text, code=200, ctype='text/plain; charset=utf-8'):
        self._send(code, text.encode('utf-8'), ctype)

    def redirect(self, location):
        try:
            self.send_response(302)
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def read_body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return None
        if length > MAX_BODY:
            raise ValueError('too large')
        raw = b''
        while len(raw) < length:
            chunk = self.rfile.read(length - len(raw))
            if not chunk:
                break
            raw += chunk
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return None

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)

        try:
            if path == '/api/info':
                self.send_json({
                    'room': DEFAULT_ROOM,
                    'publicUrl': PUBLIC_URL,
                    'lan': lan_ips(),
                    'port': PORT,
                })
                return

            if path == '/api/state':
                self.api_state(query)
                return

            if path == '/api/bg':
                bg_id = (query.get('id') or [''])[0]
                meta = BG_STORE.get(bg_id)
                if not meta:
                    self.send_json({'error': 'not found'}, 404)
                else:
                    self.send_json(meta)
                return

            # 首页：自动进入默认房间
            if path == '/':
                if 'room' not in query and DEFAULT_ROOM:
                    self.redirect('/?room=' + DEFAULT_ROOM)
                    return
                path = '/index.html'

            self.serve_file(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as err:                       # noqa: BLE001
            self.send_json({'error': str(err)}, 500)

    def serve_file(self, path):
        rel = unquote(path).lstrip('/')          # 支持中文文件名
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            self.send_text('404 Not Found', 404)
            return
        with open(full, 'rb') as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype == 'application/javascript':
            ctype += '; charset=utf-8'
        self._send(200, data, ctype)

    def api_state(self, query):
        room_name = (query.get('room') or [''])[0] or DEFAULT_ROOM
        cid = (query.get('cid') or [''])[0]
        name = (query.get('name') or [''])[0][:20]
        try:
            known_ver = int((query.get('ver') or ['-1'])[0])
        except ValueError:
            known_ver = -1
        try:
            wait = float((query.get('wait') or ['20'])[0])
        except ValueError:
            wait = 20.0
        wait = max(0.0, min(wait, 25.0))

        room = get_room(room_name)
        with room.cond:
            room.touch(cid, name)
            deadline = time.time() + wait
            while room.ver <= known_ver:
                left = deadline - time.time()
                if left <= 0:
                    break
                room.cond.wait(timeout=left)
                room.touch(cid, '')       # 只刷新心跳，避免把已改的新昵称覆盖回旧值
            snap = room.snapshot(cid)
        self.send_json(snap)

    # ---------- POST ----------
    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)

        try:
            if path == '/api/apply':
                payload = self.read_body()
                if not isinstance(payload, dict):
                    self.send_json({'error': 'bad body'}, 400)
                    return
                room_name = (query.get('room') or [''])[0] or DEFAULT_ROOM
                cid = (query.get('cid') or [''])[0]
                name = (query.get('name') or [''])[0][:20]
                room = get_room(room_name)
                with room.cond:
                    room.touch(cid, name)
                    # 只读观众 / 被踢出的客户端不接受任何改动
                    if room.role_of(cid) != 'viewer' and cid not in room.blocked:
                        room.apply(payload)
                    snap = room.snapshot(cid)
                self.send_json(snap)
                return

            if path == '/api/drag':
                payload = self.read_body() or {}
                room_name = (query.get('room') or [''])[0] or DEFAULT_ROOM
                cid = str(payload.get('cid') or (query.get('cid') or [''])[0])
                token_id = payload.get('tokenId')
                room = get_room(room_name)
                with room.cond:
                    room.touch(cid, str(payload.get('name') or '')[:20])
                    room.set_drag(cid, token_id or None)
                    room.ver += 1
                    room.cond.notify_all()
                    snap = room.snapshot(cid)
                self.send_json(snap)
                return

            if path == '/api/kick':
                payload = self.read_body() or {}
                room_name = (query.get('room') or [''])[0] or DEFAULT_ROOM
                host = str(payload.get('hostCid') or '')
                target = str(payload.get('cid') or '')
                room = get_room(room_name)
                with room.cond:
                    ok = (host == room.host and room.kick(target))
                    if ok:
                        room.ver += 1
                        room.cond.notify_all()
                    snap = room.snapshot(host)
                self.send_json({'ok': ok, 'snapshot': snap})
                return

            if path == '/api/role':
                payload = self.read_body() or {}
                room_name = (query.get('room') or [''])[0] or DEFAULT_ROOM
                host = str(payload.get('hostCid') or '')
                target = str(payload.get('cid') or '')
                role = str(payload.get('role') or '')
                room = get_room(room_name)
                with room.cond:
                    ok = (host == room.host and room.set_role(target, role))
                    if ok:
                        room.ver += 1
                        room.cond.notify_all()
                    snap = room.snapshot(host)
                self.send_json({'ok': ok, 'snapshot': snap})
                return

            if path == '/api/leave':
                room_name = (query.get('room') or [''])[0] or DEFAULT_ROOM
                cid = (query.get('cid') or [''])[0]
                room = get_room(room_name)
                with room.cond:
                    room.leave(cid)
                    room.ver += 1
                    room.cond.notify_all()
                self.send_json({'ok': True})
                return

            if path == '/api/rename':
                payload = self.read_body() or {}
                room_name = (query.get('room') or [''])[0] or DEFAULT_ROOM
                cid = str(payload.get('cid') or '')
                name = str(payload.get('name') or '')[:20]
                room = get_room(room_name)
                with room.cond:
                    before = room.members.get(cid, {}).get('name')
                    room.touch(cid, name)
                    if room.members.get(cid, {}).get('name') != before:
                        room.ver += 1
                        room.cond.notify_all()
                    snap = room.snapshot(cid)
                self.send_json(snap)
                return

            if path == '/api/bg':
                payload = self.read_body()
                if not isinstance(payload, dict) or not payload.get('data'):
                    self.send_json({'error': 'bad body'}, 400)
                    return
                data = payload['data']
                bg_id = hashlib.sha1(data.encode('utf-8')).hexdigest()[:12]
                BG_STORE[bg_id] = {
                    'data': data,
                    'w': int(payload.get('w') or 0),
                    'h': int(payload.get('h') or 0),
                    'name': str(payload.get('name') or ''),
                }
                self.send_json({'id': bg_id})
                return

            self.send_json({'error': 'not found'}, 404)
        except ValueError:
            self.send_json({'error': 'body too large'}, 413)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as err:                       # noqa: BLE001
            self.send_json({'error': str(err)}, 500)

    def do_HEAD(self):
        self.do_GET()


class QuietServer(ThreadingHTTPServer):
    """关闭时浏览器断开连接属于正常现象，不必打印异常堆栈"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    global DEFAULT_ROOM, PUBLIC_URL, PORT
    parser = argparse.ArgumentParser(description='TRPG 地图工坊本地服务器')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--room', default='')
    parser.add_argument('--public-url', default='')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--open', action='store_true', help='启动后自动打开浏览器')
    args = parser.parse_args()

    PORT = args.port
    PUBLIC_URL = args.public_url.rstrip('/')
    DEFAULT_ROOM = args.room or ''.join(
        secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(4))

    httpd = QuietServer((args.bind, PORT), Handler)
    httpd.daemon_threads = True

    print('=' * 62)
    print('  TRPG 地图工坊 · 服务器已启动')
    print('=' * 62)
    print('  房间号    : ' + DEFAULT_ROOM)
    print('  本机打开  : http://localhost:%d/' % PORT)
    for ip in lan_ips():
        print('  局域网    : http://%s:%d/    (同一 WiFi 可用)' % (ip, PORT))
    if PUBLIC_URL:
        print('  公网分享  : %s/' % PUBLIC_URL)
        print('             把上面这个链接发给任何网络的朋友')
    else:
        print('  公网分享  : 未开启（双击「启动并分享到公网.bat」即可生成）')
    print('-' * 62)
    print('  关闭此窗口即可停止服务。')
    print('=' * 62)
    print()

    if args.open:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open('http://localhost:%d/' % PORT)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止。')


if __name__ == '__main__':
    main()
