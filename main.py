#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude 用量监控 — Claude 桌面版 MCP 扩展(带系统托盘)

- MCP 工具 claude_usage:对话内按需查询(纯本地读取)
- 系统托盘图标:悬停显示 会话(5小时)/每周(7天) 百分比与重置时间,
  随每条消息自动刷新,0 额度消耗;超过阈值弹系统通知

只读本地文件,不联网,不需要任何凭证。
"""
import glob
import io
import json
import os
import struct
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
NL = chr(10)   # 日志里的换行;不写 '\n' 以免被某些编辑器改回 CRLF
STATE_FILE = os.path.join(HERE, 'state.json')
VERSION = '1.2.1'   # 与 manifest.json 的 version 必须一致,test_main.py 会校验

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
            # LevelDB 的 shared 前缀字节并不存在块里,块内只有 non_shared
            # 个 key 字节,所以读入量与偏移都只算 non_shared + vlen。
            if pos + non_shared + vlen > limit:
                break
            key = key[:shared] + blk[pos:pos+non_shared]
            val = blk[pos+non_shared:pos+non_shared+vlen]
            pos += non_shared + vlen
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
            if pos + non_shared + vlen > limit:
                break
            key = key[:shared] + idx[pos:pos+non_shared]
            val = idx[pos+non_shared:pos+non_shared+vlen]
            pos += non_shared + vlen
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

# 解析失败必须留痕:否则 Claude 前端一改字段顺序,用户只会看到
# "还没有读到用量数据",既分不清是缓存问题还是扩展坏了。
_DIAG_LAST = [None]
_DIAG_MAX = 256 * 1024
_DIAG_BURST = 200          # 同类消息累计超过这个条数才补一行计数,别把日志刷爆
_DIAG_SEEN = [0]
_NO_ML = [0]        # 本轮扫描里"不是用量记录"的 value 数
_NO_ML_LOGGED = [False]

def _diag(msg):
    """同一类消息只在"首次出现"和"每 _DIAG_BURST 次"时落盘。

    每次 poll 都会遍历几百条与用量无关的 value,逐条写盘实测 6 秒能写出
    130KB,error.log 会迅速滚到上限、把真正有用的那条挤掉。这里只保留
    第一次的样本量和周期性的计数,信息量够定位问题。
    """
    try:
        if _DIAG_LAST[0] == msg:
            _DIAG_SEEN[0] += 1
            if _DIAG_SEEN[0] % _DIAG_BURST:
                return
            msg = '%s(同类累计 %d 次)' % (msg, _DIAG_SEEN[0])
        else:
            _DIAG_LAST[0] = msg
            _DIAG_SEEN[0] = 1
        path = os.path.join(HERE, 'error.log')
        try:
            if os.path.getsize(path) > _DIAG_MAX:
                with io.open(path, 'r+', encoding='utf-8', errors='replace') as f:
                    tail = f.read()[-(_DIAG_MAX // 2):]
                    f.seek(0)
                    f.truncate()
                    f.write('--- error.log 超过 256KB,已保留尾部 ---' + chr(10) + tail)
        except OSError:
            pass
        with io.open(path, 'a', encoding='utf-8') as f:
            f.write('%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg) + chr(10))
    except Exception:
        pass

def _brief(val, n=48):
    if val is None:
        return 'None'
    return 'len=%d head=%s' % (len(val), val[:n].hex())

_RESET_MIN = 1.577e9                       # 2020-01-01,再早的 resets_at 不可信
_RESET_MAX_AHEAD = 400 * 86400             # 重置时间不可能比现在晚过一年

def _pct(util, reset, name='?'):
    """util(0..1) + reset(Unix 秒) -> {'util','pct','reset'}。

    不做 99 上限:被限流的那一刻就该显示 100。舍入用 int(x+0.5),
    因为 round() 是银行家舍入,0.045*100 == 4.5 会得到 4 而不是 5。
    """
    if util is None or reset is None:
        return None
    if not (0.0 <= util <= 1.0001):
        _diag('%s: utilization 越界 %r' % (name, util))
        return None
    if not (_RESET_MIN < reset < time.time() + _RESET_MAX_AHEAD):
        _diag('%s: resets_at 越界 %r' % (name, reset))
        return None
    pct = max(0, min(100, int(util * 100 + 0.5)))
    return {'util': round(util, 4), 'pct': pct, 'reset': reset}
def extract_usage_record(val):
    d = val
    if b'messageLimits' not in d:
        for s in range(16):
            d = snappy_decompress(val[s:]) or b''
            if b'messageLimits' in d:
                break
    if b'messageLimits' not in d:
        # 这不是故障而是常态:同一个库里几百条 value,只有一条带用量。
        # 逐条记会把 error.log 刷爆,真正的故障反而被挤掉。计数交给 poll()。
        _NO_ML[0] += 1
        return None
    ml = d.find(P_ML)
    i5 = d.find(P_5H, ml)
    i7 = d.find(P_7D, ml)
    if i5 == -1 or i7 == -1:
        _diag('找到 messageLimits 但缺窗口标记 i5=%d i7=%d ml 附近 %s'
              % (i5, i7, _brief(d[ml:ml + 64]) if ml >= 0 else 'ml<0'))
        return None
    if i7 < i5:
        _diag('窗口标记顺序变了(7d 在 5h 之前) i5=%d i7=%d,5h/7d 的顺序假设已失效'
              % (i5, i7))
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
    session = _pct(s_util, s_reset, 'session')
    weekly = _pct(w_util, w_reset, 'weekly')
    if session is None and weekly is None:
        _diag('窗口标记都在但利用率/重置时间解析失败 s_util=%r s_reset=%r '
              'w_util=%r w_reset=%r' % (s_util, s_reset, w_util, w_reset))
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

    def _offer(self, rec):
        """按 fetched_at 保留最新一条,而不是"最后解析到的一条"。

        遍历顺序是文件顺序 + 块内顺序,跟时间无关:WAL 里的新记录排在
        旧记录之后,而 .ldb 的块顺序也不保证时间递增。实测同一次 poll
        里最后解析到的是 10%,真实最新是 12%。
        """
        if not rec:
            return
        cur = self.last
        if cur is None or (rec.get('fetched_at') or 0) >= (cur.get('fetched_at') or 0):
            self.last = rec

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
                        self._offer(extract_usage_record(v))
            self.wal_state[path] = st
        else:
            prev = self.ldb_seen.get(path)
            self.ldb_seen[path] = size
            if prev == size:
                return
            try:
                for v in ldb_values(path):
                    self._offer(extract_usage_record(v))
            except Exception:
                pass

    def poll(self):
        d = _watch_dir()
        if not os.path.isdir(d):
            return self.last
        entries = []
        for p in (glob.glob(os.path.join(d, '*.log')) +
                  glob.glob(os.path.join(d, '*.ldb'))):
            try:
                entries.append((os.path.getmtime(p), p))
            except OSError:
                pass
        entries.sort(reverse=True)
        # WAL 全扫(通常 1-2 个);.ldb 取最新 8 个。原来一律只取 3 个,
        # compaction 后最新记录可能落在第 4 个文件里就永远读不到了。
        files = ([p for _, p in entries if p.endswith('.log')] +
                 [p for _, p in entries if p.endswith('.ldb')][:8])
        for f in files:
            self._scan_file(f)
        # 清理已消失文件的跟踪状态
        alive = set(files)
        for p in list(self.wal_state):
            if p not in alive:
                self.wal_state.pop(p, None)
        for p in list(self.ldb_seen):
            if p not in alive:
                self.ldb_seen.pop(p, None)
        if self.last is None:
            # 缓存里一条用量记录都没有:通常是从没在桌面端聊过天,属正常状态。
            # 只提示一次,避免每 2 秒一轮把 error.log 写满。
            if not _NO_ML_LOGGED[0]:
                _NO_ML_LOGGED[0] = True
                _diag('本地缓存里还没有 messageLimits 字段(已扫描 %d 个 value)。'
                      '用法:在 Claude 桌面端发一条消息,让前端把限额状态写进缓存。'
                      % _NO_ML[0])
        else:
            _NO_ML[0] = 0
            _NO_ML_LOGGED[0] = False
        return self.last

def load_cached():
    try:
        with io.open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def save_cached(rec):
    """原子写:托盘线程和 MCP 主线程会同时调它,直接覆盖写会留下
    半截 JSON 的窗口,读出来就是 None -> 又变成"还没读到用量数据"。"""
    tmp = STATE_FILE + '.tmp'
    try:
        with io.open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # Windows 上 os.replace 会因为对方句柄没带 FILE_SHARE_DELETE 而抛
        # PermissionError;并发写时重试几次即可,失败也只是少存一次。
        for attempt in range(4):
            try:
                os.replace(tmp, STATE_FILE)
                return
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.02)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
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
# 'starting' / 'ok' / 'failed' / 'stopped' — 托盘起不来时原来是完全静默的
TRAY_STATE = ['starting']

def _win32_prototypes(user32, shell32, kernel32, wintypes):
    """声明 Win32 原型。

    不声明 argtypes 时 ctypes 按 C int(32 位)传参,而 LPARAM 在 64 位
    进程里是 8 字节:窗口过程收到 lParam 稍大的消息(WM_GETMINMAXINFO
    之类)就抛 OverflowError: argument 4: int too long to convert。
    异常发生在 ctypes 回调里会被吞掉,窗口过程返回无效值,托盘于是
    静默失效。实测未声明时 error.log 里连堆栈都没有,只有 stderr 上的
    "Exception ignored while calling ctypes callback function"。
    """
    import ctypes          # _tray_thread 里的 import 是局部名,这里要自己导一次
    LRESULT = ctypes.c_ssize_t

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                  wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT

    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

def _tray_thread(poller):
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        shell32 = ctypes.WinDLL('shell32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _win32_prototypes(user32, shell32, kernel32, wintypes)

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
        ctypes.set_last_error(0)
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:            # ERROR_CLASS_ALREADY_EXISTS:热重载时会撞上
                TRAY_STATE[0] = 'failed'
                _diag('RegisterClassW 失败,lastError=%d,托盘未创建' % err)
                return
        ctypes.set_last_error(0)
        hWnd = user32.CreateWindowExW(0, 'ClaudeUsageTrayWnd', 'ClaudeUsageTray',
                                      0, 0, 0, 0, 0, None, None, hinst, None)
        if not hWnd:
            TRAY_STATE[0] = 'failed'
            _diag('CreateWindowExW 失败,lastError=%d,托盘未创建'
                  % ctypes.get_last_error())
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
            TRAY_STATE[0] = 'failed'
            _diag('Shell_NotifyIconW(NIM_ADD) 失败,托盘图标未创建')
            return
        TRAY_STATE[0] = 'ok'

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

        def poller_loop():
            while True:
                time.sleep(2)
                try:
                    poll_once()
                except Exception:
                    _diag('托盘轮询异常:' + NL + traceback.format_exc())

        threading.Thread(target=poller_loop, daemon=True).start()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        TRAY_STATE[0] = 'stopped'
    except Exception:
        TRAY_STATE[0] = 'failed'
        _diag('托盘线程异常退出:' + NL + traceback.format_exc())

# ── MCP stdio(JSON-RPC,换行分隔)──────────────────────────────────────────
TOOLS = [{
    'name': 'claude_usage',
    'description': ('读取本机 Claude 桌面版缓存的用量:会话(5小时)与每周(7天)'
                    '已用百分比、重置时间。免费账户可用。无参数。'),
    'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
}]

def usage_text():
    rec = read_usage()      # read_usage 内部已经 save_cached 过,这里不要再写一次
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
    if TRAY_STATE[0] == 'failed':
        lines.append('(提示: 系统托盘组件未启动,详情见扩展目录下 error.log)')
    return '\n'.join(lines)

_SCANNER = None
_SCAN_LOCK = threading.Lock()

def read_usage():
    """模块级单例:UsageScanner 的增量状态存在实例上,每次 new 一个
    就等于把 3.6MB 的 WAL 从头重解析一遍(实测 0.36s,毫无加速)。"""
    global _SCANNER
    with _SCAN_LOCK:
        if _SCANNER is None:
            _SCANNER = UsageScanner()
        _SCANNER.poll()
        last = _SCANNER.last
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
            'serverInfo': {'name': 'claude-usage-monitor', 'version': VERSION},
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
