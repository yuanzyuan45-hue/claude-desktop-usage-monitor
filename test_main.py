# -*- coding: utf-8 -*-
"""claude-usage-monitor 回归测试(无第三方依赖,直接 python test_main.py)。

跑法: python test_main.py      全绿输出 OK,失败时退出码 1。
"""
import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_RUNTIME_DIR = []


def setUpModule():
    """把运行期产物(状态缓存、诊断日志)重定向到临时目录。

    越界用例会真实触发 main._diag,而 _diag 写在 main.HERE 下 —— 不重定向
    的话跑一次测试就往仓库里丢一个 error.log。
    """
    import main
    d = tempfile.mkdtemp(prefix='usage_monitor_test_')
    _RUNTIME_DIR.append((main, main.HERE, d))
    main.HERE = d


def tearDownModule():
    import shutil
    for mod, old, d in _RUNTIME_DIR:
        mod.HERE = old
        shutil.rmtree(d, ignore_errors=True)
    _RUNTIME_DIR.clear()


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _snappy_literal(data, chunk=60):
    """只用 literal 段的合法 snappy 流。

    chunk=1 时每个字节前都插一个 tag 字节,原始明文不再连续出现,
    这样 extract_usage_record 才会真的走 snappy 解压分支。
    """
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        piece = data[i:i + chunk]
        i += len(piece)
        out.append((len(piece) - 1) << 2)
        out += piece
    return _varint(len(data)) + bytes(out)


def _rebuild_manifest_config():
    """claude Desktop 把第三方扩展的 IndexedDB 建在 https_claude.ai_0 下,
    这里按 main.py 真实的字节模式造一条 messageLimits 记录。"""
    now = time.time()
    parts = [b'\x22\x0eflagsFetchedAt\x4e' + struct.pack('<d', now - 60)]
    parts.append(b'\x22\x0dmessageLimits')
    parts.append(b'\x6f\x22' + _varint(12) + b'anthropic.io')
    parts.append(b'\x22\x02\x35\x68')
    parts.append(b'\x22\x0butilization\x4e' + struct.pack('<d', 0.05))
    parts.append(b'\x22\x09resets_at\x4e' + struct.pack('<d', now + 3600))
    parts.append(b'\x22\x02\x37\x64')
    parts.append(b'\x22\x0butilization\x4e' + struct.pack('<d', 0.045))
    parts.append(b'\x22\x09resets_at\x4e' + struct.pack('<d', now + 86400 * 3))
    parts.append(b'\x22\x07percent\x49' + _varint((4 << 1) ^ 0))
    return b''.join(parts)


class TestVarint(unittest.TestCase):
    def test_roundtrip(self):
        import main
        for v in (0, 1, 127, 128, 300, 2 ** 31, 2 ** 63 - 1):
            got, end = main.read_varint(_varint(v), 0)
            self.assertEqual(got, v)
            self.assertEqual(end, len(_varint(v)))

    def test_truncated_returns_partial(self):
        import main
        got, end = main.read_varint(b'\x80\x80', 0)
        self.assertEqual(got, 0)
        self.assertEqual(end, 2)


class TestSnappy(unittest.TestCase):
    def test_literal_only(self):
        import main
        payload = b'hello messageLimits world'
        self.assertEqual(main.snappy_decompress(_snappy_literal(payload)), payload)

    def test_copy_back_reference(self):
        import main
        # 目标 b'ababab':先 literal 'ab',再用 offset=2 len=4 的 copy
        body = bytearray()
        body.append((2 - 1) << 2)
        body += b'ab'
        body.append((2 << 2) | 2)      # t=2, len-1=2 -> len=3
        body += (2).to_bytes(2, 'little')
        body.append(((1) << 2) | 2)    # len-1=1 -> len=2
        body += (2).to_bytes(2, 'little')
        stream = _varint(7) + bytes(body)
        # 重叠拷贝:off=2 时逐字节取 out[-2],所以 3+2 段得到 'abababa'
        self.assertEqual(main.snappy_decompress(stream), b'abababa')

    def test_garbage_returns_none(self):
        import main
        self.assertIsNone(main.snappy_decompress(b'\xff\xff\xff'))
        # 空输入被宽松地还原成空串(不算解析失败),只有垃圾数据才算失败
        self.assertEqual(main.snappy_decompress(b''), b'')

    def test_length_mismatch_returns_none(self):
        import main
        stream = _varint(99) + bytes([(1 << 2)]) + b'x'
        self.assertIsNone(main.snappy_decompress(stream))


class TestPct(unittest.TestCase):
    def test_no_99_cap(self):
        import main
        now = time.time() + 100
        self.assertEqual(main._pct(1.0, now)['pct'], 100)
        self.assertEqual(main._pct(0.9999, now)['pct'], 100)

    def test_half_up_rounding(self):
        import main
        now = time.time() + 100
        self.assertEqual(main._pct(0.045, now)['pct'], 5)   # round() 会给 4
        self.assertEqual(main._pct(0.005, now)['pct'], 1)
        self.assertEqual(main._pct(0.0, now)['pct'], 0)

    def test_out_of_range_rejected(self):
        import main
        now = time.time() + 100
        self.assertIsNone(main._pct(1.5, now))
        self.assertIsNone(main._pct(-0.1, now))
        self.assertIsNone(main._pct(None, now))
        self.assertIsNone(main._pct(0.5, None))

    def test_bad_reset_rejected(self):
        import main
        self.assertIsNone(main._pct(0.5, 1.0))                  # 1970 年
        self.assertIsNone(main._pct(0.5, time.time() + 10 ** 9))  # 太远
        self.assertIsNotNone(main._pct(0.5, time.time() + 3600))


class TestExtract(unittest.TestCase):
    def test_plain_record(self):
        import main
        rec = main.extract_usage_record(_rebuild_manifest_config())
        self.assertIsNotNone(rec)
        self.assertEqual(rec['org'], 'anthropic.io')
        self.assertEqual(rec['session']['pct'], 5)
        self.assertEqual(rec['weekly']['pct'], 5)   # 0.045 -> 5
        self.assertEqual(rec['percent'], 4)
        self.assertIsInstance(rec['fetched_at'], float)

    def test_snappy_wrapped_record(self):
        import main
        raw = _rebuild_manifest_config()
        packed = _snappy_literal(raw, chunk=1)
        self.assertNotIn(b'messageLimits', packed)   # 明文确实被打散了
        rec = main.extract_usage_record(packed)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['session']['pct'], 5)

    def test_garbage_returns_none(self):
        import main
        self.assertIsNone(main.extract_usage_record(b'garbage-not-protobuf'))
        self.assertIsNone(main.extract_usage_record(b''))
        self.assertIsNone(main.extract_usage_record(b'messageLimits'))  # 只有标记

    def test_window_order_guard(self):
        import main
        now = time.time()
        # 故意把 7d 放在 5h 之前
        bad = (b'\x22\x0dmessageLimits' + b'\x22\x02\x37\x64'
               + b'\x22\x0butilization\x4e' + struct.pack('<d', 0.1)
               + b'\x22\x02\x35\x68'
               + b'\x22\x0butilization\x4e' + struct.pack('<d', 0.2))
        self.assertIsNone(main.extract_usage_record(bad))


class TestWal(unittest.TestCase):
    def _record(self, rtype, payload, seq=1):
        return (b'\x00' * 4 + len(payload).to_bytes(2, 'little')
                + bytes([rtype]) + payload)

    def test_full_record(self):
        import main
        out = main.wal_records(self._record(1, b'abc'))
        self.assertEqual(out, [b'abc'])

    def test_fragments_joined(self):
        import main
        buf = (self._record(2, b'ab') + self._record(3, b'cd')
               + self._record(4, b'ef'))
        self.assertEqual(main.wal_records(buf), [b'abcdef'])

    def test_truncated_tail_ignored(self):
        import main
        self.assertEqual(main.wal_records(b'\x00' * 3), [])


class TestBatchValues(unittest.TestCase):
    def test_extracts_values(self):
        import main
        rec = bytearray(b'\x00' * 12)
        rec += b'\x01' + _varint(3) + b'key' + _varint(5) + b'value'
        rec += b'\x01' + _varint(1) + b'k' + _varint(2) + b'vv'
        self.assertEqual(main.batch_values(bytes(rec)), [b'value', b'vv'])

    def test_deleted_entry_skipped(self):
        import main
        rec = bytearray(b'\x00' * 12)
        rec += b'\x00' + _varint(3) + b'key'
        rec += b'\x01' + _varint(1) + b'k' + _varint(2) + b'vv'
        self.assertEqual(main.batch_values(bytes(rec)), [b'vv'])

    def test_short_record(self):
        import main
        self.assertEqual(main.batch_values(b'\x00\x01'), [])


class TestBlockTable(unittest.TestCase):
    def _table(self, entries):
        """entries: [(key, value)] -> 合法 SSTable block(含 restart 数组)。"""
        body = bytearray()
        prev = b''
        for k, v in entries:
            shared = 0
            while shared < min(len(k), len(prev)) and k[shared] == prev[shared]:
                shared += 1
            non_shared = len(k) - shared
            body += _varint(shared) + _varint(non_shared) + _varint(len(v))
            body += k[shared:] + v
            prev = k
        body += b'\x00\x00\x00\x00'      # 1 个 restart point(偏移 0)
        body += (1).to_bytes(4, 'little')
        return bytes(body)

    def test_entries_roundtrip(self):
        import main
        blk = self._table([(b'k1', b'v1'), (b'k2', b'v2')])
        got = main._block_entries(blk)
        self.assertEqual(got, [(b'k1', b'v1'), (b'k2', b'v2')])

    def test_prefix_shared(self):
        import main
        blk = self._table([(b'abc', b'1'), (b'abd', b'2')])
        self.assertEqual([k for k, _ in main._block_entries(blk)], [b'abc', b'abd'])


class TestScannerOrdering(unittest.TestCase):
    """遍历顺序与时间无关,Scanner 必须按 fetched_at 保留最新一条。"""

    def test_keeps_newest(self):
        import main
        s = main.UsageScanner()
        s._offer({'fetched_at': 300.0, 'session': {'pct': 30}})
        s._offer({'fetched_at': 100.0, 'session': {'pct': 10}})   # 旧记录后到
        s._offer({'fetched_at': 200.0, 'session': {'pct': 20}})
        self.assertEqual(s.last['session']['pct'], 30)

    def test_newer_replaces(self):
        import main
        s = main.UsageScanner()
        s._offer({'fetched_at': 100.0})
        s._offer({'fetched_at': 200.0})
        self.assertEqual(s.last['fetched_at'], 200.0)

    def test_none_ignored(self):
        import main
        s = main.UsageScanner()
        s._offer(None)
        self.assertIsNone(s.last)
        s._offer({'fetched_at': 5.0})
        s._offer(None)
        self.assertEqual(s.last['fetched_at'], 5.0)

    def test_missing_fetched_at_tolerated(self):
        import main
        s = main.UsageScanner()
        s._offer({'session': {'pct': 7}})     # 没有 fetched_at
        self.assertIsNotNone(s.last)


class TestCache(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self._orig = main.STATE_FILE
        self.tmpdir = tempfile.mkdtemp()
        main.STATE_FILE = os.path.join(self.tmpdir, 'state.json')

    def tearDown(self):
        self.main.STATE_FILE = self._orig

    def test_roundtrip(self):
        rec = {'session': {'pct': 5}, 'weekly': None, 'note': '中文'}
        self.main.save_cached(rec)
        self.assertEqual(self.main.load_cached(), rec)

    def test_no_tmp_leftover(self):
        self.main.save_cached({'a': 1})
        self.assertEqual(sorted(os.listdir(self.tmpdir)), ['state.json'])

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.main.load_cached())

    def test_corrupt_file_returns_none(self):
        with io.open(self.main.STATE_FILE, 'w', encoding='utf-8') as f:
            f.write('{broken')
        self.assertIsNone(self.main.load_cached())

    def test_concurrent_writes_stay_valid(self):
        import threading
        errs = []

        def w(n):
            for i in range(20):
                try:
                    self.main.save_cached({'n': n, 'i': i})
                except Exception as e:
                    errs.append(e)

        ts = [threading.Thread(target=w, args=(n,)) for n in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertIsInstance(self.main.load_cached(), dict)
        self.assertEqual(sorted(os.listdir(self.tmpdir)), ['state.json'])


class TestManifest(unittest.TestCase):
    def test_version_matches(self):
        import main
        with io.open(os.path.join(HERE, 'manifest.json'), encoding='utf-8') as f:
            mf = json.load(f)
        self.assertEqual(mf['version'], main.VERSION)

    def test_required_fields(self):
        with io.open(os.path.join(HERE, 'manifest.json'), encoding='utf-8') as f:
            mf = json.load(f)
        for key in ('manifest_version', 'name', 'version', 'description',
                    'author', 'server'):
            self.assertIn(key, mf)
        self.assertIsInstance(mf['author'], dict)
        self.assertTrue(mf['author'].get('name'))
        self.assertNotEqual(mf['author']['name'], 'local user')

    def test_icon_exists(self):
        with io.open(os.path.join(HERE, 'manifest.json'), encoding='utf-8') as f:
            mf = json.load(f)
        icon = mf.get('icon')
        if icon:
            path = os.path.join(HERE, icon)
            self.assertTrue(os.path.isfile(path), 'icon 声明了但文件不存在: ' + icon)
            with open(path, 'rb') as f:
                self.assertEqual(f.read(8), b'\x89PNG\r\n\x1a\n')

    def test_entry_reference_exists(self):
        with io.open(os.path.join(HERE, 'manifest.json'), encoding='utf-8') as f:
            mf = json.load(f)
        entry = mf['server'].get('entry_point')
        self.assertTrue(os.path.isfile(os.path.join(HERE, entry)),
                        'entry_point 指向不存在的文件: ' + str(entry))


class TestReadme(unittest.TestCase):
    def test_no_dangling_image(self):
        import re
        path = os.path.join(HERE, 'README.md')
        with io.open(path, encoding='utf-8') as f:
            text = f.read()
        for m in re.finditer(r'!\[[^\]]*\]\(([^)]+)\)', text):
            target = m.group(1).split()[0]
            if target.startswith(('http://', 'https://')):
                continue
            self.assertTrue(os.path.isfile(os.path.join(HERE, target)),
                            'README 引用了不存在的本地图片: ' + target)

    def test_python_floor_is_37(self):
        path = os.path.join(HERE, 'README.md')
        with io.open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('3.10', text, 'README 不应再要求 Python 3.10+')
        self.assertIn('3.7', text)


class TestWin32Prototypes(unittest.TestCase):
    """托盘调用 Win32 时没有声明 argtypes,64 位进程下 LPARAM 会被按 32 位
    传参,窗口过程抛 OverflowError 并被 ctypes 吞掉 —— 托盘静默失效。"""

    def test_win32_prototypes_declares_lparam_widths(self):
        import ctypes
        import main
        if not hasattr(main, '_win32_prototypes'):
            self.skipTest('非 Windows 构建没有托盘代码')
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32')
        shell32 = ctypes.WinDLL('shell32')
        kernel32 = ctypes.WinDLL('kernel32')
        main._win32_prototypes(user32, shell32, kernel32, wintypes)
        args = user32.DefWindowProcW.argtypes
        self.assertIsNotNone(args, 'DefWindowProcW 必须声明 argtypes')
        self.assertEqual(len(args), 4)
        self.assertIs(args[3], wintypes.LPARAM)
        self.assertEqual(ctypes.sizeof(args[3]), ctypes.sizeof(ctypes.c_void_p),
                         'LPARAM 必须是指针宽度,否则 64 位上会溢出')
        self.assertEqual(ctypes.sizeof(args[2]), ctypes.sizeof(ctypes.c_void_p),
                         'WPARAM 必须是指针宽度')
        self.assertIs(user32.CreateWindowExW.restype, wintypes.HWND)

    def test_defwindowproc_survives_pointer_sized_lparam(self):
        """回归:未声明 argtypes 时这一句抛 ArgumentError: int too long。"""
        import ctypes
        import main
        if not hasattr(main, '_win32_prototypes'):
            self.skipTest('非 Windows 构建没有托盘代码')
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32')
        main._win32_prototypes(user32, ctypes.WinDLL('shell32'),
                              ctypes.WinDLL('kernel32'), wintypes)
        big = 0x7FFFFFFFFFF0
        user32.DefWindowProcW(None, 0x000F, 0, big)   # 不抛异常即通过


class TestDiagNoise(unittest.TestCase):
    """诊断日志必须能定位问题,同时不能被正常数据刷爆。"""

    def setUp(self):
        import main
        self.main = main
        self.tmpdir = tempfile.mkdtemp()
        self._here = main.HERE
        main.HERE = self.tmpdir
        main._DIAG_LAST[0] = None
        main._DIAG_SEEN[0] = 0

    def tearDown(self):
        self.main.HERE = self._here
        self.main._DIAG_LAST[0] = None
        self.main._DIAG_SEEN[0] = 0

    def _log(self):
        path = os.path.join(self.tmpdir, 'error.log')
        if not os.path.isfile(path):
            return ''
        with io.open(path, encoding='utf-8') as f:
            return f.read()

    def test_identical_message_is_written_once(self):
        """同一条消息 500 次只应留下个位数的行,而不是 500 行。"""
        n = 500
        for _ in range(n):
            self.main._diag('tok-dim')
        # 语义:首次写一行,之后每 _DIAG_BURST 次补一行计数
        expect = 1 + (n - 1) // self.main._DIAG_BURST
        self.assertEqual(self._log().count('tok-dim'), expect)
        self.assertLess(expect, 5, '节流比例不够')

    def test_burst_still_reports_periodically(self):
        n = self.main._DIAG_BURST * 3
        for _ in range(n):
            self.main._diag('tok-burst')
        body = self._log()
        self.assertIn('同类累计', body, '爆发后必须补计数行,否则看不出重复次数')
        self.assertGreater(body.count('tok-burst'), 1)
        self.assertLess(body.count('tok-burst'), 10)

    def test_distinct_message_resets_counter(self):
        """换成另一条消息后再回到原消息,应当重新记一行(去重按"最近一条"判断)。"""
        for _ in range(10):
            self.main._diag('tok-aaa')
        self.main._diag('tok-bbb')
        self.main._diag('tok-aaa')
        body = self._log()
        self.assertEqual(body.count('tok-aaa'), 2)
        self.assertEqual(body.count('tok-bbb'), 1)

    def test_non_usage_value_does_not_log(self):
        """indexeddb 里绝大多数 value 与用量无关,逐条记会把日志刷爆。"""
        import main
        main._NO_ML[0] = 0
        before = self._log()
        for i in range(200):
            main.extract_usage_record(b'no-limits-here-%d' % i)
        self.assertEqual(self._log(), before)
        self.assertEqual(main._NO_ML[0], 200)


class TestTrayFailureIsVisible(unittest.TestCase):
    """托盘起不来时必须留下痕迹,不能停在 'starting' 静默失业。"""

    def test_register_class_and_create_window_paths_set_state(self):
        import main
        src = io.open(os.path.join(HERE, 'main.py'), encoding='utf-8').read()
        start = src.index('def _tray_thread(')
        body = src[start:]
        self.assertNotIn("if not user32.RegisterClassW(ctypes.byref(wc)):"
                         + chr(10) + '            return', body,
                         'RegisterClassW 失败不能裸 return')
        self.assertNotIn('if not hWnd:' + chr(10) + '            return', body,
                         'CreateWindowExW 失败不能裸 return')
        self.assertIn("TRAY_STATE[0] = 'failed'", body)

    def test_every_early_return_reports_a_reason(self):
        """裸 return 等于静默失业:TRAY_STATE 停在 starting,日志里也没有线索。

        允许提前返回,但返回前必须已经置过 TRAY_STATE 并写过日志。
        """
        src = io.open(os.path.join(HERE, 'main.py'), encoding='utf-8').read()
        seg = src[src.index('def _tray_thread('):src.index('Shell_NotifyIconW(NIM_ADD')]
        lines = seg.split(chr(10))
        for i, ln in enumerate(lines):
            if ln.strip() != 'return':
                continue
            window = chr(10).join(lines[max(0, i - 6):i])
            self.assertIn('TRAY_STATE[0] = ', window,
                          '第 %d 行提前返回前没有置 TRAY_STATE:%s' % (i + 1, ln))
            self.assertIn('_diag(', window,
                          '第 %d 行提前返回前没有写日志:%s' % (i + 1, ln))

class TestToolOutputIsBilingual(unittest.TestCase):
    """工具输出、托盘提示、工具描述都要中英并存:英文用户装完不能只看懂中文。"""

    def _rec(self):
        return {'fetched_at': 1795338254605.0,
                'session': {'util': 0.36, 'pct': 36, 'reset': time.time() + 3600},
                'weekly': {'util': 0.05, 'pct': 5, 'reset': time.time() + 86400}}

    def test_usage_text_carries_both_languages(self):
        import main
        saved = main.read_usage
        main.read_usage = lambda: self._rec()
        try:
            text = main.usage_text()
        finally:
            main.read_usage = saved
        self.assertIn('已用', text)
        self.assertIn('used', text)
        self.assertIn('Session (5h)', text)
        self.assertIn('Week (7d)', text)
        self.assertIn('36%', text)
        self.assertIn('5%', text)

    def test_usage_text_no_data_path_is_bilingual(self):
        import main
        saved = main.read_usage
        main.read_usage = lambda: None
        try:
            text = main.usage_text()
        finally:
            main.read_usage = saved
        self.assertIn('发送一条消息', text)
        self.assertIn('Send a message', text)

    def test_reset_branch_is_bilingual(self):
        import main
        past = time.time() - 60
        saved = main.read_usage
        main.read_usage = lambda: {
            'fetched_at': 1795338254605.0,
            'session': {'util': 0.36, 'pct': 36, 'reset': past},
            'weekly': {'util': 0.05, 'pct': 5, 'reset': past}}
        try:
            text = main.usage_text()
        finally:
            main.read_usage = saved
        self.assertIn('已于', text)
        self.assertIn('window reset at', text)
        self.assertIn('was 36%', text)

    def test_tool_description_is_bilingual(self):
        import main
        desc = main.TOOLS[0]['description']
        self.assertIn('用量', desc)
        self.assertIn('Claude Desktop cache', desc)
        self.assertIn('Free plan', desc)

    def test_tray_tooltip_is_bilingual(self):
        src = io.open(os.path.join(HERE, 'main.py'), encoding='utf-8').read()
        self.assertIn("parts.append('会话/Session ", src)
        self.assertIn("parts.append('每周/Week ", src)
        self.assertIn('Claude 用量 Usage', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
