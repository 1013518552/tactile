# -*- coding: utf-8 -*-
"""
tactile · 采集器（档位 1-4）

采样 100ms 一次；按用户填的间隔聚合成一条，写入 data/YYYY-MM-DD.jsonl。

档位决定采什么：
  档 0  什么都不采
  档 1  按键次数 / 鼠标位移 / 移动事件 / 活动秒数 / 最长空闲
  档 2  + 连击段数（bursts）、抖动次数（flickers）
  档 3  + 前台进程名（procs，只取进程名，不取窗口标题）
  档 4  + 鼠标路径采样（mousePath，抽稀后的坐标点）

档 5（按键明细）不在这个文件里实现——它需要单独的开关与门禁。
"""
import ctypes, ctypes.wintypes as wt, json, os, threading, time
from datetime import datetime

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

SAMPLE_MS = 100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 64 位签名
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]


def key_category(vk):
    """把虚拟键码归类，不给具体键名。鼠标键单独一类，不混进键盘。"""
    if vk in (0x01, 0x02, 0x04, 0x05, 0x06):     # 左/右/中/侧键
        return "mouse"
    if 0x30 <= vk <= 0x39:          # 0-9
        return "digit"
    if 0x41 <= vk <= 0x5A:          # A-Z
        return "letter"
    if 0x70 <= vk <= 0x87:          # F1-F24
        return "function"
    if vk in (0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
        return "modifier"           # Shift/Ctrl/Alt/Caps/Win
    if vk in (0x08, 0x09, 0x0D, 0x1B, 0x20):     # 退格/Tab/回车/Esc/空格
        return "edit"
    if 0x25 <= vk <= 0x28:          # 方向键
        return "arrow"
    return "other"


def foreground_process_name():
    """当前前台窗口所属进程的 exe 名（不读窗口标题）。取不到返回 None。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_uint(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return None
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None


class Collector:
    def __init__(self, data_dir, on_write=None):
        self.data_dir = data_dir
        self.on_write = on_write
        self.interval = 60.0
        self.tier = 1
        self.running = False
        self._thread = None
        self._prev_keys = [0] * 256
        self._prev_pos = None
        self._started_once = False
        self.last_line = None
        # 档 2 的连击状态：跨桶保留，所以放在这里初始化（不随桶重置）
        self._burst_run = 0
        self._last_key_ts = 0.0
        self._reset_bucket()

    # ---------- 对外 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self._prev_pos = None
        self._prev_keys = [0] * 256
        self._reset_bucket()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._event("collector_start")
        self._started_once = True
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._bucket_has_data():
            self._flush()
        if getattr(self, "_started_once", False):
            self._event("collector_stop")
            self._started_once = False

    def set_guard(self, enabled, procs):
        self.guard_enabled = bool(enabled)
        self.guard_procs = [p.strip().lower() for p in procs if p and p.strip()]
        print(f"[ok] 敏感窗口守卫 {'开' if self.guard_enabled else '关'}，共 {len(self.guard_procs)} 条规则", flush=True)

    def set_interval(self, seconds):
        self.interval = max(0.1, min(3600.0, float(seconds)))

    def set_tier(self, tier):
        """档 5 必须先把门禁解锁（unlock_tier5），否则最高只能到 4。"""
        t = max(0, min(5, int(tier)))
        if t >= 5 and not getattr(self, "tier5_unlocked", False):
            print("[warn] 档 5 未解锁，退回档 4", flush=True)
            t = 4
        self.tier = t
        return self.tier

    def unlock_tier5(self):
        self.tier5_unlocked = True
        print("[ok] 档 5 门禁已通过", flush=True)

    # ---------- 清空 ----------
    def clear_data(self, keep_log=False):
        import glob
        files = sorted(glob.glob(os.path.join(self.data_dir, "*.jsonl")))
        total = 0
        for f in files:
            try:
                with open(f, encoding="utf-8") as fh:
                    total += sum(1 for ln in fh if ln.strip())
                os.remove(f)
            except Exception as e:
                print("[warn] 删除失败:", f, e, flush=True)
        if keep_log:
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                path = os.path.join(self.data_dir, "clear_log.txt")
                with open(path, "a", encoding="utf-8") as fh:
                    print("%s  清空 %d 个文件 / %d 条" % (
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        len(files), total), file=fh)
            except Exception as e:
                print("[warn] 写清空日志失败:", e, flush=True)
        self.last_line = None
        return {"files": len(files), "records": total, "logged": keep_log}

    # ---------- 内部 ----------
    def _reset_bucket(self):
        self._keys = 0
        self._clicks = 0
        self._mouse_px = 0
        self._mouse_events = 0
        self._active_time = 0.0          # 真实活动时长（秒，累加三角）
        self._idle_max = 0.0
        self._bucket_start = time.time()
        # 档 2（burst 状态跨桶保留，否则细间隔下永远凑不满一段）
        self._burst_pending = 0
        self._flickers = 0
        self._last_dx = 0
        self._dx_flip_ts = 0.0
        # 档 3
        self._procs = {}
        self._last_proc_ts = 0.0
        # 档 4
        self._path = []
        self._last_path_ts = 0.0
        self._key_cats = {}
        # 档 5（只在门禁通过后才会被写入）
        self._key_detail = {}
        # 敏感窗口守卫（默认开：检测到密码管理器/网银等前台进程就暂停采集）
        self.guard_enabled = False
        self.guard_procs = []
        self.auto_paused = False
        self._last_guard_ts = 0.0

    def _bucket_has_data(self):
        return bool(self._keys or self._mouse_px or self._mouse_events
                    or self._active_time or self._path)

    def _idle_seconds(self):
        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(li)):
            return 0.0
        return (kernel32.GetTickCount() - li.dwTime) / 1000.0

    def _loop(self):
        while self.running:
            t0 = time.time()
            now_sec = int(t0)

            # 敏感窗口守卫：每秒检查一次前台进程，命中就暂停采集（连计数都不做）
            if self.guard_enabled and (t0 - self._last_guard_ts) >= 1.0:
                self._last_guard_ts = t0
                name = (foreground_process_name() or "").lower()
                hit = any(g in name or name in g for g in self.guard_procs) if name else False
                if hit and not self.auto_paused:
                    self.auto_paused = True
                    self._event("auto_pause")
                    print(f"[ok] 检测到敏感窗口 {name}，已暂停采集", flush=True)
                elif not hit and self.auto_paused:
                    self.auto_paused = False
                    self._event("auto_resume")
                    print("[ok] 离开敏感窗口，恢复采集", flush=True)

            if self.auto_paused:
                # 暂停期间不吃数据、不落盘（连时间桶也重置，避免把空白算进去）
                self._prev_pos = None
                time.sleep(SAMPLE_MS / 1000.0)
                continue

            p = POINT()
            moved = 0
            dx = 0
            if user32.GetCursorPos(ctypes.byref(p)):
                if self._prev_pos is not None:
                    dx = p.x - self._prev_pos[0]
                    dy = p.y - self._prev_pos[1]
                    moved = int((dx * dx + dy * dy) ** 0.5)
                self._prev_pos = (p.x, p.y)

            pressed = 0
            newly = []
            for vk in range(1, 255):        # 0 和 255 不是有效虚拟键，跳过
                down = 1 if (user32.GetAsyncKeyState(vk) & 0x8000) else 0
                if down and not self._prev_keys[vk]:
                    if vk in (0x01, 0x02, 0x04, 0x05, 0x06):
                        self._clicks += 1        # 鼠标键单独算，不混进按键数
                    else:
                        pressed += 1
                        newly.append(vk)
                self._prev_keys[vk] = down

            # 档 4：只记"哪一类键"，不记具体是哪个
            if self.tier >= 4 and newly:
                for vk in newly:
                    cat = key_category(vk)
                    self._key_cats[cat] = self._key_cats.get(cat, 0) + 1
            # 档 5：记具体是哪个键（需要两道门才准许打开）
            if self.tier >= 5 and newly:
                for vk in newly:
                    self._key_detail[vk] = self._key_detail.get(vk, 0) + 1

            if moved or pressed:
                self._active_time = min(self._active_time + SAMPLE_MS / 1000.0,
                                        self.interval)
                self._mouse_px += moved
                if moved:
                    self._mouse_events += 1
                self._keys += pressed

            # 档 2：连击段（状态跨桶保留，凑满 3 下才算一段，计入"即将落盘"的计数）
            if self.tier >= 2:
                if pressed:
                    if t0 - self._last_key_ts > 0.5:
                        if self._burst_run >= 3:
                            self._burst_pending += 1
                        self._burst_run = 0
                    self._burst_run += pressed
                    self._last_key_ts = t0
                elif self._burst_run >= 3 and (t0 - self._last_key_ts) > 0.5:
                    self._burst_pending += 1
                    self._burst_run = 0
                if moved and abs(dx) > 2:
                    sign = 1 if dx > 0 else -1
                    if self._last_dx and sign != self._last_dx and (t0 - self._dx_flip_ts) < 0.35:
                        self._flickers += 1
                        self._dx_flip_ts = t0
                    else:
                        if sign != self._last_dx:
                            self._dx_flip_ts = t0
                    self._last_dx = sign

            # 档 3：前台进程名（每秒最多采一次）
            if self.tier >= 3 and t0 - self._last_proc_ts >= 1.0:
                self._last_proc_ts = t0
                name = foreground_process_name()
                if name:
                    self._procs[name] = self._procs.get(name, 0) + 1

            # 档 4：鼠标路径（每 500ms 一个点，避免爆量）
            if self.tier >= 4 and moved and t0 - self._last_path_ts >= 0.5:
                self._last_path_ts = t0
                self._path.append([p.x, p.y])
                if len(self._path) > 240:      # 一个桶最多留 240 个点
                    self._path = self._path[-240:]

            idle = self._idle_seconds()
            if idle > self._idle_max:
                self._idle_max = idle

            if t0 - self._bucket_start >= self.interval:
                self._flush()
                self._reset_bucket()

            spent = (time.time() - t0) * 1000
            time.sleep(max(0.0, (SAMPLE_MS - spent) / 1000.0))

    def _flush(self):
        span = round(time.time() - self._bucket_start, 1)
        rec = {
            "t": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "span": span,
            "keys": self._keys,
            "clicks": self._clicks,
            "mousePx": self._mouse_px,
            "mouseEvents": self._mouse_events,
            "activeSec": round(min(self._active_time, span), 1),
            "idleMax": round(self._idle_max, 1),
            "tier": self.tier,
        }
        if self.tier >= 2:
            bursts = self._burst_pending + (1 if self._burst_run >= 3 else 0)
            rec["bursts"] = bursts
            rec["flickers"] = self._flickers
            self._burst_pending = 0
        if self.tier >= 3:
            rec["procs"] = sorted(self._procs.items(), key=lambda kv: -kv[1])[:6]
        if self.tier >= 4:
            rec["mousePath"] = self._path[-120:]
            rec["keyCats"] = dict(self._key_cats)
        if self.tier >= 5:
            rec["keyDetail"] = sorted(self._key_detail.items(), key=lambda kv: -kv[1])[:80]

        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, datetime.now().strftime("%Y-%m-%d") + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.last_line = rec
            if self.on_write:
                self.on_write(rec)
        except Exception as e:
            print("[warn] 写盘失败:", e, flush=True)

    def mark_recovered_if_needed(self):
        """启动时检查：上一条如果不是正常停止，补一条 recovered。
        这样关机 / 强杀 / 崩溃留下的断档，事后能解释。"""
        try:
            import glob
            files = sorted(glob.glob(os.path.join(self.data_dir, "*.jsonl")))
            if not files:
                return None
            last = files[-1]
            last_line = ""
            with open(last, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        last_line = ln
            if not last_line:
                return None
            try:
                rec = json.loads(last_line)
            except Exception:
                rec = {}
            if rec.get("event") == "collector_stop":
                return None
            self._event("recovered")
            print("[ok] 上次没有正常结束，已补记 recovered", flush=True)
            return "recovered"
        except Exception as e:
            print("[warn] 检查上次结束状态失败:", e, flush=True)
            return None

    def _event(self, kind):
        """记录采集器的启停事件，这样事后能分清"没动"和"没在跑"。"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, datetime.now().strftime("%Y-%m-%d") + ".jsonl")
            rec = {"t": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "event": kind, "tier": self.tier}
            with open(path, "a", encoding="utf-8") as f:
                print(json.dumps(rec, ensure_ascii=False), file=f)
        except Exception as e:
            print("[warn] 写事件失败:", e, flush=True)
