#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude 用量监控 — Claude 桌面版 MCP 扩展(带系统托盘)

- MCP 工具 claude_usage:对话内按需查询(纯本地读取)
- 系统托盘图标:悬停显示 会话(5小时)/每周(7天) 百分比与重置时间,
  随每条消息自动刷新,0 额度消耗;超过阈值弹系统通知

只读本地文件,不联网,不需要任何凭证。
"""
import glob
import json
import os
import queue
import struct
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, 'state.json')

# ── LevelDB / Snappy(纯标准库)─────────────────────────────────────────────
def snappy_decompress(src):
    try:
        i = 0
        length = 0; shift = 0
        while i < len(src):
            b = src[i]; i += 1
            length |= (b & 0x7f) << shift
            if not b & 0x80: break
            shift += 7
        out = bytearray()
        n = len(src)
        while i < n and len(out) < length:
            tag = src[i]; i += 1
            t = tag & 3
            if t == 0:
                l = (tag >> 2) + 1
                if l > 60:
                    extra = l - 60
                    if i + extra > n: return None
                    l = int.from_bytes(src[i:i+extra], 'little') + 1
                    i += extra
                if i + l > n: return None
                out += src[i:i+l]; i += l
            else:
                if t == 1:
                    l = ((tag >> 2) & 0x7) + 4
                    off = ((tag >> 5) << 8) | src[i]; i += 1
                elif t == 2:
                    l = (tag >> 2) + 1
                    off = int.from_bytes(src[i:i+2], 'little'); i += 2
                else:
                    l = (tag >> 2) + 1
                    off = int.from_bytes(src[i:i+4], 'little'); i += 4
                if off == 0 or off > len(out): return None
                for _ in range(l):
                    out.append(out[-off])
        if len(out) != length:
            return None
        return bytes(out)
    except Exception:
        return None

def read_varint(buf, i):
    r = 0; s = 0
    while i < len(buf):
        b = buf[i]; i += 1
        r |= (b & 0x7f) << s
        if not b & 0x80: break
        s += 7
    return r, i

def wal_records(buf):
    out = []
    frag = bytearray()
    pos = 0; n = len(buf)
    while pos + 7 <= n:
        block_left = 32768 - (pos % 32768)
        if block_left < 7:
            pos += block_left
            continue
        length = int.from_bytes(buf[pos+4:pos+6], 'little')
        rtype = buf[pos+6]
        ds = pos + 7
        de = min(ds + length, n)
        data = buf[ds:de]
        if rtype == 1:
            out.append(data)
        elif rtype == 2:
            frag = bytearray(data)
        elif rtype == 3:
            frag += data
        elif rtype == 4:
            frag += data; out.append(bytes(frag)); frag = bytearray()
        pos = ds + length
    return out

def batch_values(rec):
    vals = []
    if len(rec) < 12:
        return vals
    i = 12
    try:
        while i < len(rec):
            t = rec[i]; i += 1
            if t == 0:
                klen, i = read_varint(rec, i)
                i += klen
                continue
            if t != 1:
                break
            klen, i = read_varint(rec, i)
            i += klen
            vlen, i = read_varint(rec, i)
            vals.append(rec[i:i+vlen])
            i += vlen
    except Exception:
        pass
    return vals

def _read_block(buf, off, size):
    if off + size + 5 > len(buf):
        return None
    raw = buf[off:off+size]
    ctype = buf[off+size]
    if ctype == 0:
        return raw
    if ctype == 1:
        return snappy_decompress(raw)
    return None

def _block_entries(blk):
    out = []
    pos = 0
    key = b''
    if len(blk) < 4:
        return out
    n_restarts = int.from_bytes(blk[-4:], 'little')
    limit = len(blk) - 4 - 4 * n_restarts
    while pos < limit:
        try:
            shared, pos = read_varint(blk, pos)
            non_shared, pos = read_varint(blk, pos)
            vlen, pos = read_varint(blk, pos)
            if pos + shared + non_shared + vlen > limit:
                break
            key = key[:shared] + blk[pos+shared:pos+shared+non_shared]
            val = blk[pos+shared+non_shared:pos+shared+non_shared+vlen]
            pos += shared + non_shared + vlen
            out.append((key, val))
        except Exception:
            break
    return out

def ldb_values(path):
    buf = open(path, 'rb').read()
    if len(buf) < 52:
        return []
    footer = buf[-48:]
    m_off, i = read_varint(footer, 0)
    m_size, i = read_varint(footer, i)
    d_off, i = read_varint(footer, i)
    d_size, i = read_varint(footer, i)
    vals = []
    idx = _read_block(buf, d_off, d_size)
    if idx is None:
        return vals
    pos = 0
    key = b''
    n_restarts = int.from_bytes(idx[-4:], 'little') if len(idx) >= 4 else 0
    limit = len(idx) - 4 - 4 * n_restarts
    while pos < limit:
        try:
            shared, pos = read_varint(idx, pos)
            non_shared, pos = read_varint(idx, pos)
            vlen, pos = read_varint(idx, pos)
            if pos + shared + non_shared + vlen > limit:
                break
            key = key[:shared] + idx[pos+shared:pos+shared+non_shared]
            val = idx[pos+shared+non_shared:pos+shared+non_shared+vlen]
            pos += shared + non_shared + vlen
            if len(val) < 2:
                continue
            b_off, j = read_varint(val, 0)
            b_size, j = read_varint(val, j)
            blk = _read_block(buf, b_off, b_size)
            if blk is None:
                continue
            for _k2, v2 in _block_entries(blk):
                vals.append(v2)
        except Exception:
            break
    return vals

def idb_values(path):
    buf = open(path, 'rb').read()
    if path.endswith('.log'):
        vals = []
        for rec in wal_records(buf):
            vals += batch_values(rec)
        return vals
    return ldb_values(path)

# ── 用量提取 ────────────────────────────────────────────────────────────────
P_UTIL = b'\x22\x0butilization\x4e'
P_RESET = b'\x22\x09resets_at\x4e'
P_PERCENT = b'\x22\x07percent\x49'
P_5H = b'\x22\x02\x35\x68'
P_7D = b'\x22\x02\x37\x64'
P_ML = b'\x22\x0dmessageLimits'
P_FETCH = b'\x22\x0eflagsFetchedAt\x4e'

def _find_dbl(d, pat, start, end):
    p = d.find(pat, start, end)
    if p == -1 or p + len(pat) + 8 > len(d):
        return None
    return struct.unpack_from('<d', d, p + len(pat))[0]

def _find_varint(d, pat, start, end):
    p = d.find(pat, start, end)
    if p == -1:
        return None
    v, _ = read_varint(d, p + len(pat))
    return (v >> 1) ^ -(v & 1)

def _pct(util, reset):
    if util is None or reset is None:
        return None
    if not (0.0 <= util <= 1.0) or not (1.75e9 < reset < 1.9e9):
        return None
    return {'util': round(util, 4), 'pct': min(99, max(0, round(util * 100))),
            'reset': reset}

def extract_usage_record(val):
    d = val
    if b'messageLimits' not in d:
        for s in range(16):
            d = snappy_decompress(val[s:]) or b''
            if b'messageLimits' in d:
                break
    if b'messageLimits' not in d:
        return None
    ml = d.find(P_ML)
    i5 = d.find(P_5H, ml)
    i7 = d.find(P_7D, ml)
    if i5 == -1 or i7 == -1 or i7 < i5:
        return None
    org = None
    k = ml + len(P_ML)
    if k < len(d) and d[k] == 0x6F:
        k += 1
        if k < len(d) and d[k] == 0x22:
            ln, k = read_varint(d, k + 1)
            org = d[k:k+ln].decode('ascii', 'replace')
    fetched = _find_dbl(d, P_FETCH, max(0, ml - 200), ml + 1)
    s_util = _find_dbl(d, P_UTIL, i5, i7)
    s_reset = _find_dbl(d, P_RESET, i5, i7)
    w_util = _find_dbl(d, P_UTIL, i7, i7 + 1500)
    w_reset = _find_dbl(d, P_RESET, i7, i7 + 1500)
    percent = _find_varint(d, P_PERCENT, i7, i7 + 6000)
    session = _pct(s_util, s_reset)
    weekly = _pct(w_util, w_reset)
    if session is None and weekly is None:
        return None
    return {'org': org, 'fetched_at': fetched, 'session': session,
            'weekly': weekly, 'percent': percent}

# ── 扫描入口(增量)────────────────────────────────────────────────────────
def _watch_dir():
    return os.path.join(os.environ.get('APPDATA', ''), 'Claude', 'IndexedDB',
                        'https_claude.ai_0.indexeddb.leveldb')

class UsageScanner:
    """增量扫描 IndexedDB:WAL 只读新增字节,.ldb 大小变化才重扫。"""
    def __init__(self):
        self.wal_state = {}        # path -> {'offset': int, 'buf': bytes}
        self.ldb_seen = {}         # path -> size
        self.last = None           # 最新记录

    def _scan_file(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if path.endswith('.log'):
            st = self.wal_state.get(path, {'offset': 0, 'buf': b''})
            if size < st['offset']:
                st = {'offset': 0, 'buf': b''}
            if size == st['offset']:
                return
            try:
                with open(path, 'rb') as f:
                    f.seek(st['offset'])
                    new = f.read(size - st['offset'])
            except OSError:
                return
            st['offset'] = size
            st['buf'] += new
            # 只解析完整的 32KB 块,余数留到下次(保持块对齐)
            full = (len(st['buf']) // 32768) * 32768
            if full:
                chunk, st['buf'] = st['buf'][:full], st['buf'][full:]
                for rec in wal_records(chunk):
                    for v in batch_values(rec):
                        rec2 = extract_usage_record(v)
                        if rec2:
                            self.last = rec2
            self.wal_state[path] = st
        else:
            prev = self.ldb_seen.get(path)
            self.ldb_seen[path] = size
            if prev == size:
                return
            try:
                for v in ldb_values(path):
                    rec2 = extract_usage_record(v)
                    if rec2:
                        self.last = rec2
            except Exception:
                pass

    def poll(self):
        d = _watch_dir()
        if not os.path.isdir(d):
            return self.last
        files = glob.glob(os.path.join(d, '*.log')) + glob.glob(os.path.join(d, '*.ldb'))
        files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                   reverse=True)
        for f in files[:3]:
            self._scan_file(f)
        # 清理已消失文件的跟踪状态
        alive = set(files[:3])
        for p in list(self.wal_state):
            if p not in alive:
                self.wal_state.pop(p, None)
        for p in list(self.ldb_seen):
            if p not in alive:
                self.ldb_seen.pop(p, None)
        return self.last

def load_cached():
    try:
        return json.load(open(STATE_FILE, encoding='utf-8'))
    except Exception:
        return None

def save_cached(rec):
    try:
        json.dump(rec, open(STATE_FILE, 'w', encoding='utf-8'))
    except Exception:
        pass

def fmt_ts(ts):
    try:
        return time.strftime('%m-%d %H:%M', time.localtime(ts))
    except Exception:
        return '?'

def make_icon(pct):
    """用 PIL 生成托盘图标:底色随用量变色,中间显示百分比数字。失败返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        if pct is None:
            color, text = (142, 142, 147), '?'
        else:
            color = ((48, 209, 88) if pct < 60
                     else (255, 214, 10) if pct < 85 else (255, 69, 58))
            text = str(pct)
        img.rounded_rectangle([4, 4, 60, 60], radius=14, fill=color + (255,))
        try:
            font = ImageFont.truetype('arialbd.ttf', 36)
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((64 - w) / 2 - bbox[0], (64 - h) / 2 - bbox[1]), text,
                  fill=(20, 20, 22), font=font)
        path = os.path.join(HERE, 'tray_icon.ico')
        img.save(path, format='ICO', sizes=[(16, 16), (32, 32), (64, 64)])
        return path
    except Exception:
        return None

# ── 系统托盘(ctypes,纯标准库)────────────────────────────────────────────
TRAY_OK = False

def _tray_thread(poller):
    global TRAY_OK
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
        NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 16

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ('cbSize', wintypes.DWORD),
                ('hWnd', wintypes.HWND),
                ('uID', wintypes.UINT),
                ('uFlags', wintypes.UINT),
                ('uCallbackMessage', wintypes.UINT),
                ('hIcon', wintypes.HICON),
                ('szTip', ctypes.c_wchar * 128),
                ('dwState', wintypes.DWORD),
                ('dwStateMask', wintypes.DWORD),
                ('szInfo', ctypes.c_wchar * 256),
                ('uVersion', wintypes.UINT),
                ('szInfoTitle', ctypes.c_wchar * 64),
                ('dwInfoFlags', wintypes.DWORD),
                ('guidItem', ctypes.c_ubyte * 16),
                ('hBalloonIcon', wintypes.HICON),
            ]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                                     wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM)
        WM_APP_TRAY = 0x8F00

        def wnd_proc(hWnd, msg, wParam, lParam):
            if msg == WM_APP_TRAY and lParam == 0x0205:   # 右键弹起:打开 Claude
                import subprocess
                subprocess.Popen('start claude://', shell=True,
                                 creationflags=0x08000000)
            return user32.DefWindowProcW(hWnd, msg, wParam, lParam)
        proc = WNDPROC(wnd_proc)

        hinst = kernel32.GetModuleHandleW(None)
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC),
                        ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                        ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                        ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                        ('lpszMenuName', wintypes.LPCWSTR),
                        ('lpszClassName', wintypes.LPCWSTR)]
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = proc
        wc.hInstance = hinst
        wc.lpszClassName = 'ClaudeUsageTrayWnd'
        if not user32.RegisterClassW(ctypes.byref(wc)):
            return
        hWnd = user32.CreateWindowExW(0, 'ClaudeUsageTrayWnd', 'ClaudeUsageTray',
                                      0, 0, 0, 0, 0, None, None, hinst, None)
        if not hWnd:
            return

        def load_icon(pct=None):
            """动态百分比图标(PIL);失败则退回 Claude 自带图标。"""
            path = make_icon(pct)
            if path:
                h = user32.LoadImageW(None, path, 1, 0, 0, 0x10)  # LR_LOADFROMFILE
                if h:
                    return h
            for pat in (os.path.join(os.environ.get('LOCALAPPDATA', ''),
                                     'AnthropicClaude', 'app-*', 'resources',
                                     'Tray-Win32.ico'),):
                hits = glob.glob(pat)
                if hits:
                    h = user32.LoadImageW(None, hits[0], 1, 0, 0, 0x10)  # LR_LOADFROMFILE
                    if h:
                        return h
            return user32.LoadIconW(None, 32512)  # IDI_APPLICATION

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hWnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        cached0 = load_cached()
        s0 = cached0.get('session') if isinstance(cached0, dict) else None
        init_pct = s0['pct'] if isinstance(s0, dict) and s0.get('pct') is not None else None
        nid.hIcon = load_icon(init_pct)
        nid.szTip = 'Claude 用量:等待数据…'
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return

        state = {'last_pct': None, 'notified': 0, 'icon_pct': init_pct,
                 'hicon': nid.hIcon}

        def modify_tip(text):
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.szTip = text[:127]
            nid.dwInfoFlags = 0
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

        def balloon(title, text):
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_INFO
            nid.szInfo = text[:255]
            nid.szInfoTitle = title[:63]
            nid.dwInfoFlags = 4   # NIIF_INFO
            nid.uVersion = 0
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            nid.dwInfoFlags = 0

        def poll_once():
            rec = poller.poll()
            if rec is None:
                return
            cached = load_cached()
            if cached is None or (rec.get('fetched_at') or 0) >= (cached.get('fetched_at') or 0):
                save_cached(rec)
            now = time.time()
            s, w = rec.get('session'), rec.get('weekly')
            parts = []
            alert = None
            icon_pct = 0
            if s:
                if s['reset'] <= now:
                    parts.append('会话已于%s重置' % fmt_ts(s['reset']))
                else:
                    parts.append('会话 %s%%(重置 %s)' % (s['pct'], fmt_ts(s['reset'])))
                    alert = s['pct']
                    icon_pct = s['pct']
            if w:
                if w['reset'] <= now:
                    parts.append('每周已于%s重置' % fmt_ts(w['reset']))
                else:
                    parts.append('每周 %s%%(重置 %s)' % (w['pct'], fmt_ts(w['reset'])))
                    if alert is None:
                        alert = w['pct']
                        icon_pct = w['pct']
            modify_tip('Claude 用量  ' + ' | '.join(parts) if parts
                       else 'Claude 用量:等待数据…')
            # 图标数字变化时重绘
            if icon_pct != state['icon_pct']:
                old = state['hicon']
                h = load_icon(icon_pct)
                if h:
                    nid.hIcon = h
                    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
                    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
                    state['icon_pct'] = icon_pct
                    if old:
                        user32.DestroyIcon(old)
            # 阈值通知:每 +5% 提醒一次,80% 起报
            if alert is not None and alert >= 80:
                step = int(alert // 5)
                if step > state['notified']:
                    state['notified'] = step
                    balloon('Claude 用量提醒', '会话(5小时)已用 %s%%' % alert)
            elif alert is not None and alert < 50:
                state['notified'] = 0

        # 先刷一次再进消息循环
        poll_once()
        poller_done = threading.Event()

        def poller_loop():
            while True:
                time.sleep(2)
                try:
                    poll_once()
                except Exception:
                    pass
                poller_done.set()

        threading.Thread(target=poller_loop, daemon=True).start()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        TRAY_OK = True
    except Exception:
        try:
            open(os.path.join(HERE, 'error.log'), 'a', encoding='utf-8').write(
                time.strftime('%Y-%m-%d %H:%M:%S ') + traceback.format_exc())
        except Exception:
            pass

# ── MCP stdio(JSON-RPC,换行分隔)──────────────────────────────────────────
TOOLS = [{
    'name': 'claude_usage',
    'description': ('读取本机 Claude 桌面版缓存的用量:会话(5小时)与每周(7天)'
                    '已用百分比、重置时间。免费账户可用。无参数。'),
    'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
}]

def usage_text():
    rec = read_usage()
    if rec:
        save_cached(rec)
    else:
        rec = load_cached()
    if not rec:
        return ('还没有读到用量数据。请在 Claude 桌面版里发送一条消息,'
                '然后再次调用本工具。')
    now = time.time()

    def win_line(name, w):
        if not w:
            return None
        if w['reset'] <= now:
            return ('%s:窗口已于 %s 重置,当前用量 0%%(发送任意一条消息后即可看到真实值;'
                    '重置前为 %s%%)' % (name, fmt_ts(w['reset']), w['pct']))
        return '%s:%s%% 已用,重置于 %s' % (name, w['pct'], fmt_ts(w['reset']))

    lines = []
    for name, w in (('会话(5小时)', rec.get('session')),
                    ('每周(7天)', rec.get('weekly'))):
        line = win_line(name, w)
        if line:
            lines.append(line)
    if not lines:
        lines.append('本地缓存中暂时没有窗口用量数据。')
    lines.append('(来自 Claude 桌面版本地缓存,随每条新消息自动更新)')
    return '\n'.join(lines)

def read_usage():
    scanner = UsageScanner()
    scanner.poll()
    last = scanner.last
    if last:
        save_cached(last)
        return last
    return load_cached()

def handle(req):
    method = req.get('method', '')
    rid = req.get('id')
    if method == 'initialize':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {
            'protocolVersion': req.get('params', {}).get('protocolVersion',
                                                         '2024-11-05'),
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'claude-usage-monitor', 'version': '1.1.0'},
        }}
    if method.startswith('notifications/'):
        return None
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': TOOLS}}
    if method == 'tools/call':
        name = req.get('params', {}).get('name', '')
        if name == 'claude_usage':
            try:
                text = usage_text()
            except Exception as e:
                text = '读取失败:%r' % (e,)
            return {'jsonrpc': '2.0', 'id': rid, 'result': {
                'content': [{'type': 'text', 'text': text}], 'isError': False}}
        return {'jsonrpc': '2.0', 'id': rid, 'result': {
            'content': [{'type': 'text', 'text': '未知工具 ' + name}],
            'isError': True}}
    if rid is not None:
        return {'jsonrpc': '2.0', 'id': rid, 'error': {
            'code': -32601, 'message': 'Method not found: ' + method}}
    return None

def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
    # 托盘线程(失败不影响工具)
    threading.Thread(target=_tray_thread, args=(UsageScanner(),),
                     daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            resp = handle(req)
        except Exception as e:
            if 'id' in req:
                resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'error': {
                    'code': -32603, 'message': repr(e)}}
            else:
                resp = None
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
            sys.stdout.flush()

if __name__ == '__main__':
    main()
