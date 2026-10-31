# -*- coding: utf-8 -*-
"""
tactile · 第一步：只做「关」。

这一版**不采集任何数据**。它只证明三件事：
  1. 能启动
  2. 能干净退出（不是假关）
  3. 能自证「现在还有几个进程」

热键 Ctrl+Alt+T 切换 运行/待机；关窗即退出。
"""
import ctypes, ctypes.wintypes as wt, sys, os, tkinter as tk, webbrowser
from tkinter import ttk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pythonw 没有控制台：把 stdout/stderr 接到文件，否则出问题什么都看不到
try:
    if sys.stdout is None or sys.stderr is None:
        _log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tactile.log"),
                    "a", encoding="utf-8", buffering=1)
        sys.stdout = _log
        sys.stderr = _log
except Exception:
    pass

from collector import Collector
import server as httpserver

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

APP = "tactile"
VERSION = "0.9.0"
HOTKEY_ID = 1
MOD_ALT, MOD_CONTROL, VK_T = 0x0001, 0x0002, 0x54

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]

HWND_MESSAGE = ctypes.c_void_p(-3)

# 64 位下这几个类型是 8 字节，用 wintypes 里的 c_long 会溢出
LRESULT = ctypes.c_ssize_t
WPARAM_T = ctypes.c_size_t
LPARAM_T = ctypes.c_ssize_t

user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, WPARAM_T, LPARAM_T]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.CreateWindowExW.restype = ctypes.c_void_p

class App:
    def __init__(self):
        self.counter = 0
        base = os.path.dirname(os.path.abspath(__file__))
        self.config_path = os.path.join(base, "tactile.json")
        self.cfg = self._load_config(base)
        self.data_dir = self.cfg["data_dir"]
        self.collector = Collector(self.data_dir, on_write=self._on_write)
        self.running = False
        self.root = tk.Tk()
        self.root.title(f"{APP} {VERSION}")
        self.root.geometry("430x430")
        self.root.minsize(320, 240)
        self.root.resizable(True, True)
        # 关窗口 = 收进托盘（不是退出）；退出走托盘菜单或窗口里的「退出」
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        # 可滚动容器（窗口拖小的时候，右边出现滚动条）
        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        frm = ttk.Frame(canvas, padding=12)
        win_id = canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))

        def _wheel(event):
            delta = -1 if getattr(event, "delta", 0) > 0 else 1
            canvas.yview_scroll(delta * 2, "units")
        canvas.bind_all("<MouseWheel>", _wheel)

        header = ttk.Frame(frm)
        header.pack(fill="x")
        ttk.Label(header, text="tactile", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Button(header, text="设置", width=6, command=self.open_settings).pack(side="right")
        self.status = ttk.Label(frm, text="", font=("Segoe UI", 11))
        self.status.pack(anchor="w", pady=(6, 10))

        # —— 采集控制 ——
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(0, 6))
        self.collect_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="开始采集", variable=self.collect_var,
                        command=self.toggle_collect).pack(side="left")

        row2 = ttk.Frame(frm)
        row2.pack(fill="x", pady=(0, 6))
        ttk.Label(row2, text="间隔").pack(side="left")
        self.interval_var = tk.StringVar(value=self.cfg.get("interval", "60"))
        ttk.Entry(row2, textvariable=self.interval_var, width=6).pack(side="left", padx=4)
        ttk.Label(row2, text="秒").pack(side="left")
        ttk.Button(row2, text="应用", width=6, command=self.apply_interval).pack(side="left", padx=4)

        row2b = ttk.Frame(frm)
        row2b.pack(fill="x", pady=(0, 6))
        ttk.Label(row2b, text="档位").pack(side="left")
        self.tier_var = tk.StringVar(value=self.cfg.get("tier", "1"))
        tier_box = ttk.Combobox(row2b, textvariable=self.tier_var, width=4, state="readonly",
                                values=["0", "1", "2", "3", "4", "5"])
        tier_box.pack(side="left", padx=4)
        tier_box.bind("<<ComboboxSelected>>", lambda e: self.apply_tier())
        tier_help = ttk.Label(row2b, text="各个档位的说明", foreground="#3b82f6", cursor="hand2")
        tier_help.pack(side="left", padx=8)
        tier_help.bind("<Button-1>", lambda e: self.show_tier_help())

        row2d = ttk.Frame(frm)
        row2d.pack(fill="x", pady=(0, 6))
        ttk.Label(row2d, text="规则").pack(side="left")
        rules_lbl = ttk.Label(row2d, text="提问规则（什么时候该问）…", foreground="#3b82f6", cursor="hand2")
        rules_lbl.pack(side="left", padx=4)
        rules_lbl.bind("<Button-1>", lambda e: self.edit_ask_rules())
        ttk.Label(row2d, text=f"　数据目录在「设置」里", foreground="#999").pack(side="left")

        row2c = ttk.Frame(frm)
        row2c.pack(fill="x", pady=(0, 6))
        ttk.Label(row2c, text="存到").pack(side="left")
        self.dir_var = tk.StringVar(value=self._short(self.data_dir))
        ttk.Label(row2c, textvariable=self.dir_var, foreground="#555").pack(side="left", padx=4)
        ttk.Button(row2c, text="选择…", width=7, command=self.choose_data_dir).pack(side="left")
        row2c.pack_forget()   # 已挪到「设置」里，这里不再显示

        self.count_lbl = ttk.Label(frm, text="已写 0 条", foreground="#777")
        self.count_lbl.pack(anchor="w", pady=(0, 10))

        row3 = ttk.Frame(frm)
        row3.pack(fill="x", pady=(0, 6))
        ttk.Button(row3, text="清空数据…", command=self.clear_dialog).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ttk.Button(row3, text="打开面板", command=self.open_panel).pack(side="left", expand=True, fill="x")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(0, 6))
        ttk.Button(btns, text="退出", command=self.quit).pack(fill="x")

        self.hotkey_label = ttk.Label(frm, text="", foreground="#777")
        self.hotkey_label.pack(anchor="w", pady=(6, 0))

        self.refresh()
        self.register_hotkey()

        # 上次是不是正常结束？不是就补一条 recovered（解释断档）
        try:
            self.collector.mark_recovered_if_needed()
        except Exception as e:
            print("[warn] recovered 检查失败:", e, flush=True)

        # 敏感窗口守卫
        try:
            self.collector.set_guard(
                self.cfg.get("sensitive_guard", "1") == "1",
                [p for p in (self.cfg.get("sensitive_procs") or "").split(",") if p.strip()],
            )
        except Exception as e:
            print("[warn] 守卫初始化失败:", e, flush=True)

        # 数据目录落在同步盘里就提醒
        hit = self.cloud_risk(self.cfg.get("data_dir", ""))
        if hit:
            self.status.config(text=f"⚠ 数据目录在同步盘里（{hit}），设置里可改", foreground="#c00")

        # 应用保存的档位与间隔
        # 「保留上次设置」开着时，档 5 也可以恢复（因为开这个开关时已经过了一次带风险说明的门禁）
        try:
            if self.cfg.get("remember", "1") == "1":
                saved_tier = int(self.cfg.get("tier", "1"))
                saved_interval = float(self.cfg.get("interval", "60"))
            else:
                saved_tier = 1
                saved_interval = 60.0
                self.tier_var.set("1")
                self.interval_var.set("60")
            if saved_tier >= 5:
                self.collector.unlock_tier5()
                self.tier_var.set("5")
            self.collector.set_tier(saved_tier)
            self.collector.set_interval(saved_interval)
        except Exception as e:
            print("[warn] 恢复配置失败:", e, flush=True)

        # 本地 HTTP（供我读取）
        self.port_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "port.txt")
        try:
            self.srv, self.port = httpserver.start(self, 18090, self.port_file)
        except Exception as e:
            print(f"[warn] HTTP 启动失败: {e}", flush=True)
            self.port = None
        self.hotkey_label.config(text=f"热键 {self.hotkey_name or '不可用'} ｜ 点 × 收进托盘")

        self.setup_tray()

    def _load_config(self, base):
        import json as _json
        default_dir = os.path.join(base, "data")
        cfg = {
            "data_dir": default_dir,
            "interval": "60",
            "tier": "0",
            "sound_start": "1_soft_up",
            "sound_stop": "stop_1_soft_up",
            "autostart": "0",
            "remember": "1",
            "sensitive_guard": "1",
            "sensitive_procs": ("1password.exe,keepass.exe,bitwarden.exe,lastpass.exe,"
                                "alipay.exe,icbc.exe,ccb.exe,abchina.exe,boc.exe,unionpay.exe"),
            "ask_rules": {
                "enabled": True,
                "abnormal": {"enabled": True, "ratio": "3", "window_minutes": "60"},
                "blank": {"enabled": True, "minutes": "10"},
                "switch": {"enabled": True, "procs": "3", "minutes": "5"},
            },
        }
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, encoding="utf-8") as f:
                    saved = _json.load(f)
                for k in ("data_dir", "interval", "tier", "sound_start", "sound_stop",
                          "autostart", "remember", "sensitive_guard", "sensitive_procs"):
                    if isinstance(saved.get(k), str) and saved[k]:
                        cfg[k] = saved[k]
                if isinstance(saved.get("ask_rules"), dict):
                    cfg["ask_rules"] = saved["ask_rules"]
        except Exception as e:
            print("[warn] 读配置失败:", e, flush=True)
        return cfg

    def _save_config(self):
        import json as _json
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(_json.dumps(self.cfg, ensure_ascii=False, indent=2))
        except Exception as e:
            print("[warn] 写配置失败:", e, flush=True)

    def get_ask_rules(self):
        """给所有 AI 读的「什么时候该问」规则。用户可改（见「提问规则…」）。"""
        r = self.cfg.get("ask_rules") or {}
        enabled = bool(r.get("enabled", True))
        abnormal = r.get("abnormal") or {}
        blank = r.get("blank") or {}
        switch = r.get("switch") or {}

        def num(d, k, default):
            try:
                return float(d.get(k, default))
            except (TypeError, ValueError):
                return default

        return {
            "enabled": enabled,
            "rules": [
                {
                    "id": "abnormal",
                    "enabled": enabled and bool(abnormal.get("enabled", True)),
                    "name": "反常",
                    "when": "某个指标明显偏离个人基线",
                    "threshold": {
                        "ratio": num(abnormal, "ratio", 3.0),
                        "window_minutes": int(num(abnormal, "window_minutes", 60)),
                    },
                },
                {
                    "id": "blank",
                    "enabled": enabled and bool(blank.get("enabled", True)),
                    "name": "空白",
                    "when": "采集器在跑，但连续一段时间没有任何记录",
                    "threshold": {"minutes": int(num(blank, "minutes", 10))},
                },
                {
                    "id": "switch",
                    "enabled": enabled and bool(switch.get("enabled", True)),
                    "name": "切换",
                    "when": "短时间内在多个程序之间来回跳",
                    "threshold": {
                        "procs": int(num(switch, "procs", 3)),
                        "minutes": int(num(switch, "minutes", 5)),
                    },
                },
            ],
            "how_to_use": (
                "读 /stats 时先读这份规则；命中任一条 enabled=true 的规则，"
                "就在回答的开头问一个具体问题（带着线索问，不要开放式提问）。"
                "用户可以在 tactile 窗口的「提问规则…」里改阈值或关掉。"
            ),
        }

    def edit_ask_rules(self):
        """让用户改规则：开关 + 阈值。"""
        r = self.cfg.setdefault("ask_rules", {})
        win = tk.Toplevel(self.root)
        win.title("提问规则")
        win.geometry("460x330")
        win.resizable(False, False)
        win.transient(self.root)

        ttk.Label(win, text="什么时候该问（所有 AI 共用这份规则）",
                  font=("Segoe UI", 11, "bold"), padding=(14, 12, 14, 6)).pack(anchor="w")

        vars_ = {}
        rows = [
            ("abnormal", "反常", "指标偏离基线", "倍", "ratio"),
            ("blank", "空白", "采集器在跑但没记录", "分钟", "minutes"),
            ("switch", "切换", "短时间在多程序间跳", "程序数", "procs"),
        ]
        for key, name, desc, unit, field in rows:
            sub = r.setdefault(key, {})
            row = ttk.Frame(win, padding=(14, 4))
            row.pack(fill="x")
            v = tk.BooleanVar(value=bool(sub.get("enabled", True)))
            ttk.Checkbutton(row, text=f"{name}（{desc}）", variable=v).pack(side="left")
            e = ttk.Entry(row, width=6)
            e.insert(0, str(sub.get(field, "")))
            e.pack(side="right")
            ttk.Label(row, text=unit).pack(side="right", padx=4)
            vars_[key] = (v, e, field)

        def save():
            for key, (v, e, field) in vars_.items():
                sub = r.setdefault(key, {})
                sub["enabled"] = bool(v.get())
                sub[field] = e.get().strip() or sub.get(field, "")
            self._save_config()
            self.status.config(text="提问规则已保存", foreground="#0a0")
            print("[ok] 提问规则已保存", flush=True)
            win.destroy()

        ttk.Button(win, text="保存", command=save).pack(fill="x", padx=14, pady=14)
        ttk.Label(win, text="改完立刻生效；/rules 端点会返回新值。",
                  foreground="#777", padding=(14, 0, 14, 10)).pack(anchor="w")

    CLOUD_MARKERS = ("onedrive", "dropbox", "icloud", "nutstore", "坚果云", "googledrive",
                     "baidunetdisk", "百度网盘", "weiyun", "阿里云盘", "aliyundrive")

    def cloud_risk(self, path):
        """数据目录是不是落在同步盘里。返回命中的关键词，没命中返回 None。"""
        low = str(path).lower()
        for m in self.CLOUD_MARKERS:
            if m in low:
                return m
        return None

    def play_cue(self, kind):
        """播放提示音：kind = 'start' 或 'stop'。失败就静默。"""
        try:
            import winsound
            key = "sound_start" if kind == "start" else "sound_stop"
            name = self.cfg.get(key) or ""
            if not name:
                return
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds", name + ".wav")
            if os.path.exists(path):
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            print("[warn] 提示音失败:", e, flush=True)

    def set_autostart(self, on):
        """开机自启动 = 启/停计划任务 tactile-app（已存在的那个）。"""
        import subprocess
        flag = "/enable" if on else "/disable"
        try:
            subprocess.run(["schtasks", "/change", "/tn", "tactile-app", flag],
                           capture_output=True, creationflags=0x08000000)
            print(f"[ok] 开机自启动 -> {'开' if on else '关'}", flush=True)
            return True
        except Exception as e:
            print("[warn] 设置自启动失败:", e, flush=True)
            return False

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry("540x600")
        win.resizable(False, True)
        win.transient(self.root)

        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        # —— 数据目录 ——
        ttk.Label(frm, text="数据目录", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(4, 4))
        dir_var = tk.StringVar(value=self.data_dir)
        ttk.Label(row, textvariable=dir_var, foreground="#555").pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="选择…", width=7, command=self.choose_data_dir).pack(side="right")

        cloud_lbl = ttk.Label(frm, text="", foreground="#c00", wraplength=470, justify="left")
        cloud_lbl.pack(anchor="w", pady=(0, 10))

        def refresh_cloud_warning():
            hit = self.cloud_risk(self.data_dir)
            if hit:
                cloud_lbl.config(text=(
                    f"⚠ 这个目录在同步盘里（命中「{hit}」）——数据会被上传到云端，"
                    "不再是「只存本机」。建议换到本机的普通目录，例如 E:\\ai\\tools\\tactile\\data。"))
            else:
                cloud_lbl.config(text="")
        refresh_cloud_warning()
        win.after(600, refresh_cloud_warning)

        # —— 提示音 ——
        ttk.Label(frm, text="提示音", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        names = [
            ("1_soft_up", "1 柔和上滑"),
            ("2_single", "2 短促一声"),
            ("3_wood", "3 木质轻敲"),
            ("4_two_note", "4 两音递进"),
        ]
        srow = ttk.Frame(frm)
        srow.pack(fill="x", pady=(4, 8))

        start_var = tk.StringVar(value=self.cfg.get("sound_start", "1_soft_up"))
        stop_var = tk.StringVar(value=self.cfg.get("sound_stop", "stop_1_soft_up"))

        ttk.Label(srow, text="开始").pack(side="left")
        ttk.Combobox(srow, textvariable=start_var, width=12, state="readonly",
                     values=[n for n, _ in names]).pack(side="left", padx=4)
        ttk.Label(srow, text="停止").pack(side="left", padx=(10, 0))
        ttk.Combobox(srow, textvariable=stop_var, width=14, state="readonly",
                     values=["stop_" + n for n, _ in names]).pack(side="left", padx=4)

        def preview(kind):
            old = self.cfg.get("sound_start") if kind == "start" else self.cfg.get("sound_stop")
            self.cfg["sound_start" if kind == "start" else "sound_stop"] = (
                start_var.get() if kind == "start" else stop_var.get())
            self.play_cue(kind)
            self.cfg["sound_start" if kind == "start" else "sound_stop"] = old

        prow = ttk.Frame(frm)
        prow.pack(fill="x", pady=(0, 12))
        ttk.Button(prow, text="试听开始音", command=lambda: preview("start")).pack(side="left")
        ttk.Button(prow, text="试听停止音", command=lambda: preview("stop")).pack(side="left", padx=6)

        # —— 敏感窗口守卫 ——
        ttk.Label(frm, text="敏感窗口自动暂停", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        guard_var = tk.BooleanVar(value=self.cfg.get("sensitive_guard", "1") == "1")
        ttk.Checkbutton(frm, text="前台是密码管理器 / 网银时，自动暂停采集（连计数都不做）",
                        variable=guard_var).pack(anchor="w", pady=(4, 2))
        guard_entry = ttk.Entry(frm)
        guard_entry.insert(0, self.cfg.get("sensitive_procs", ""))
        guard_entry.pack(fill="x", pady=(2, 2))
        ttk.Label(frm, text="上面是进程名，逗号分隔（不区分大小写）",
                  foreground="#777").pack(anchor="w", pady=(0, 12))

        # —— 开机自启 / 记住设置 ——
        auto_var = tk.BooleanVar(value=self.cfg.get("autostart") == "1")
        rem_var = tk.BooleanVar(value=self.cfg.get("remember") == "1")
        ttk.Checkbutton(frm, text="开机自启动（登录后自动运行 tactile）",
                        variable=auto_var).pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(frm, text="启动后保留上次设置（档位 / 间隔；档 5 永远要重新过门禁）",
                        variable=rem_var).pack(anchor="w", pady=(0, 12))

        def save():
            want_remember = bool(rem_var.get())
            if want_remember:
                # 记住设置意味着档 5 下次也不再重过门禁 —— 所以在这里过一遍，并把风险讲清
                passed = self.tier5_gate(extended=True)
                if not passed:
                    rem_var.set(False)
                    self.status.config(text="没有通过门禁，已取消「保留上次设置」", foreground="#a00")
                    return
            self.cfg["sound_start"] = start_var.get()
            self.cfg["sound_stop"] = stop_var.get()
            self.cfg["autostart"] = "1" if auto_var.get() else "0"
            self.cfg["remember"] = "1" if want_remember else "0"
            self.cfg["sensitive_guard"] = "1" if guard_var.get() else "0"
            self.cfg["sensitive_procs"] = guard_entry.get().strip()
            self._save_config()
            self.collector.set_guard(guard_var.get(),
                                     [p for p in guard_entry.get().split(",") if p.strip()])
            self.set_autostart(auto_var.get())
            self.status.config(text="设置已保存", foreground="#0a0")
            print("[ok] 设置已保存", flush=True)
            win.destroy()

        ttk.Button(frm, text="保存", command=save).pack(fill="x", pady=(6, 0))

    def choose_data_dir(self):
        from tkinter import filedialog
        cur = self.data_dir
        picked = filedialog.askdirectory(title="选择数据保存目录", initialdir=cur)
        if not picked:
            return
        picked = os.path.normpath(picked)
        self.data_dir = picked
        self.collector.data_dir = picked
        self.cfg["data_dir"] = picked
        self._save_config()
        self.dir_var.set(self._short(picked))
        self.status.config(text=f"数据目录已改为：{picked}", foreground="#0a0")
        print(f"[ok] 数据目录 -> {picked}", flush=True)

    @staticmethod
    def _short(p, limit=34):
        p = str(p)
        return p if len(p) <= limit else "…" + p[-(limit - 1):]

    def register_hotkey(self):
        """tk 的主循环会吞掉线程级 WM_HOTKEY，所以另起一个线程 + 隐藏窗口来收。"""
        candidates = [
            ("Ctrl+Alt+T", MOD_CONTROL | MOD_ALT, VK_T),
            ("Ctrl+Alt+F9", MOD_CONTROL | MOD_ALT, 0x78),
            ("Ctrl+Shift+T", MOD_CONTROL | 0x0004, VK_T),
        ]
        self.hotkey_name = None
        for name, mods, vk in candidates:
            if self._probe(mods, vk):
                self.hotkey_name = name
                self._start_hotkey_thread(mods, vk)
                print(f"[ok] 热键已注册: {name}", flush=True)
                return
        print("[warn] 所有候选热键都被占用，只能用鼠标切换", flush=True)

    def unregister_hotkey(self):
        """热键注册在子线程的隐藏窗口上；进程退出即释放，这里只做兜底。"""
        try:
            user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass

    def _probe(self, mods, vk):
        """先试一下能不能注册，成功就立刻注销（真正注册在子线程里做）"""
        ok = user32.RegisterHotKey(None, 99, mods, vk)
        if ok:
            user32.UnregisterHotKey(None, 99)
        return bool(ok)

    def _start_hotkey_thread(self, mods, vk):
        import threading
        def runner():
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint,
                                         WPARAM_T, LPARAM_T)
            def wndproc(hwnd, msg, wp, lp):
                if msg == 0x0312:      # WM_HOTKEY
                    self.root.after(0, self.toggle)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wp, lp)
            self._wndproc_ref = WNDPROC(wndproc)

            hinst = kernel32.GetModuleHandleW(None)
            cls = WNDCLASSW()
            cls.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
            cls.hInstance = hinst
            cls.lpszClassName = "TactileHotkeyWnd"
            cls.style = 0
            user32.RegisterClassW(ctypes.byref(cls))
            # HWND_MESSAGE = -3 → 只收消息的隐藏窗口
            hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "tactile-hk", 0,
                                          0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
            if not hwnd:
                print("[warn] 隐藏窗口创建失败，热键不可用", flush=True)
                return
            if not user32.RegisterHotKey(hwnd, HOTKEY_ID, mods, vk):
                print("[warn] 子线程注册热键失败", flush=True)
                return
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), hwnd, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        threading.Thread(target=runner, daemon=True).start()

    def pump(self):
        """每 40ms 看一次有没有热键消息（不阻塞 tk 主循环）"""
        msg = wt.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:   # WM_HOTKEY
                self.toggle()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.root.after(40, self.pump)

    def open_panel(self):
        """用默认浏览器打开本地面板"""
        port = getattr(self, "port", None)
        if not port:
            self.status.config(text="HTTP 没起来，打不开面板", foreground="#a00")
            return
        import threading
        url = f"http://127.0.0.1:{port}/"
        print(f"[ok] 打开面板 {url}", flush=True)
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    def clear_dialog(self):
        """二选一：彻底删除 / 删除但留一行日志"""
        win = tk.Toplevel(self.root)
        win.title("清空数据")
        win.geometry("360x190")
        win.resizable(False, False)
        win.transient(self.root)
        ttk.Label(win, text="要删掉 data 目录里所有 jsonl。选一种：",
                  padding=12).pack(anchor="w")

        def do(keep):
            r = self.collector.clear_data(keep_log=keep)
            self.counter = 0
            self.count_lbl.config(text=f"已清空 {r['records']} 条（{'留了一行日志' if keep else '不留痕'}）")
            print(f"[ok] 清空 {r}", flush=True)
            win.destroy()

        ttk.Button(win, text="彻底删除（不留任何痕迹）",
                   command=lambda: do(False)).pack(fill="x", padx=12, pady=4)
        ttk.Button(win, text="删除，但留一行「何时清了多少」",
                   command=lambda: do(True)).pack(fill="x", padx=12, pady=4)
        ttk.Button(win, text="取消", command=win.destroy).pack(fill="x", padx=12, pady=8)

    def stats(self, minutes=60):
        """读今天的数据文件，返回最近 N 分钟的记录"""
        import glob, json as _json
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.data_dir, today + ".jsonl")
        rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(_json.loads(ln))
                    except Exception:
                        pass
        return {"minutes": minutes, "count": len(rows), "rows": rows[-3600:]}

    def on_quit(self):
        """HTTP /quit 调它：先停采集再关窗"""
        self.root.after(0, self.quit)

    def _on_write(self, rec):
        self.counter += 1
        self.root.after(0, lambda: self.count_lbl.config(
            text=f"已写 {self.counter} 条 ｜ 最近 keys={rec['keys']} px={rec['mousePx']} active={rec['activeSec']}s"))

    def toggle_collect(self):
        if self.collect_var.get():
            try:
                iv = float(self.interval_var.get())
            except ValueError:
                iv = 60.0
            self.collector.set_interval(iv)
            self.collector.start()
            self.play_cue("start")
            print(f"[ok] 开始采集，间隔 {self.collector.interval}s", flush=True)
        else:
            self.collector.stop()
            self.play_cue("stop")
            print("[ok] 停止采集", flush=True)
        self.refresh()

    def apply_interval(self):
        try:
            iv = float(self.interval_var.get())
        except ValueError:
            self.status.config(text="间隔必须是数字", foreground="#a00")
            return
        self.collector.set_interval(iv)
        self.cfg["interval"] = str(iv)
        self._save_config()
        self.status.config(text=f"间隔已设为 {self.collector.interval}s", foreground="#555")

    def apply_tier(self):
        try:
            tier = int(self.tier_var.get())
        except ValueError:
            tier = 1
        if tier >= 5:
            # 档 5 走两道门；没通过就退回原来的档
            if getattr(self.collector, "tier5_unlocked", False) or self.tier5_gate():
                self.collector.unlock_tier5()
                self.collector.set_tier(5)
                self.collector._event("tier5_on")
                print("[ok] 档位设为 5（已过门禁）", flush=True)
                self.refresh()
            else:
                self.tier_var.set(str(self.collector.tier))
                self.status.config(text="档 5 未开启（门禁未通过）", foreground="#a00")
            return
        self.collector.set_tier(tier)
        self.cfg["tier"] = str(tier)
        self._save_config()
        if getattr(self.collector, "tier5_unlocked", False) and tier < 5:
            self.collector._event("tier5_off")
            self.collector.tier5_unlocked = False
        print(f"[ok] 档位设为 {tier}", flush=True)
        self.refresh()

    def show_tier_help(self):
        """各个档位的说明（点击文字弹出）"""
        win = tk.Toplevel(self.root)
        win.title("各个档位的说明")
        win.geometry("560x470")
        win.resizable(False, True)
        win.transient(self.root)

        txt = tk.Text(win, wrap="word", padx=16, pady=14, relief="flat",
                      font=("Microsoft YaHei", 10))
        txt.pack(fill="both", expand=True)

        lines = [
            ("档 0 · 不采集", "完全关闭。程序还在，但不读任何输入。默认就是这个。"),
            ("档 1 · 基础", "只记数量，不记内容：\n"
                          "· 这段时间按了多少次键（不知道是哪个键）\n"
                          "· 鼠标移动了多少像素（不记坐标）\n"
                          "· 产生了多少次移动\n"
                          "· 有输入活动的时长、最长一次停顿\n"
                          "适合：看作息、看忙闲。"),
            ("档 2 · 节奏", "在档 1 之上，多记\"手的节奏\"：\n"
                          "· 连击段数——连续按 3 下以上算一段（能看出你在快速操作还是单点）\n"
                          "· 抖动次数——鼠标在很短时间内来回摆（能看出犹豫、瞄准、找东西）\n"
                          "适合：区分\"在想\"和\"在干\"。"),
            ("档 3 · 窗口", "在档 2 之上，多记：\n"
                          "· 前台窗口的进程名（例如 chrome.exe、Code.exe）\n"
                          "· 不记窗口标题，也不记窗口里有什么\n"
                          "适合：知道你在哪个程序里待了多久、切了几次。"),
            ("档 4 · 路径", "在档 3 之上，多记：\n"
                          "· 鼠标路径——每 0.5 秒取一个点（不是全程录屏，只是轨迹采样）\n"
                          "· 按键分类计数：字母 / 数字 / 功能键 / 修饰键 / 编辑键 / 方向键 / 鼠标键\n"
                          "  ——只知道\"字母键按了 17 次\"，不知道是哪几个字母\n"
                          "适合：看操作习惯。"),
            ("档 5 · 完全明细", "在档 4 之上，记住每一个键：\n"
                             "· 具体是哪个键、按了几次（例如 W 4 次、D 3 次、左键 2 次）\n"
                             "· 这等于键盘流水：能还原出你打的字\n\n"
                             "⚠️ 这一档会被杀毒软件当成键盘记录器。\n"
                             "⚠️ 开启需要两道门：读满 10 秒 + 手动输入「我明白」。"),
            ("", ""),
            ("采集的数据存在哪", "只存在本机 E:\\ai\\tools\\tactile\\data\\ 下，一天一个文件。\n"
                             "不联网、不上传、没有遥测。\n"
                             "保留多久由你定，随时可以在「清空数据」里删掉。"),
            ("随时能停", "· 取消勾选「开始采集」立刻停\n"
                       "· 左上角热键 Ctrl+Alt+T 切换\n"
                       "· 托盘菜单里也有开始/停止\n"
                       "· 面板 http://127.0.0.1:18090/ 上也有按钮"),
        ]
        txt.tag_configure("h", font=("Microsoft YaHei", 11, "bold"), spacing1=10, spacing3=4)
        txt.tag_configure("b", spacing3=10, lmargin1=8, lmargin2=8)
        for head, body in lines:
            if not head:
                txt.insert("end", "\n")
                continue
            txt.insert("end", head + "\n", "h")
            txt.insert("end", body + "\n", "b")
        txt.config(state="disabled")

    def tier5_gate(self, extended=False):
        """两道门：① 读满 10 秒才能点「继续」 ② 手输「我明白」。返回 True 表示通过。
        extended=True 时多讲一段：AI 权限过高的风险，并说明不建议开。"""
        win = tk.Toplevel(self.root)
        win.title("档 5 · 完整按键记录")
        win.geometry("520x420" if extended else "460x300")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        ok = {"value": False}

        ttk.Label(win, text="档 5 会记录你按下的每一个键。", font=("Segoe UI", 12, "bold"),
                  padding=(16, 14, 16, 4)).pack(anchor="w")
        body = ("包括密码、私信、任何你输入的内容。\n"
                "这些记录只存在本机，但一旦被看到，等于看到你的键盘流水。\n")
        if extended:
            body += ("\n如果读取它的 AI 权限很高，风险会更大：\n"
                     "· 它可能在你不知情的时候把这些记录读走、写进别的地方；\n"
                     "· 它可能把这些记录和你的其它数据（对话、文件、位置）关联起来；\n"
                     "· 一旦它出错，出错的方式你未必察觉得到。\n"
                     "\n所以：**不建议开。** 除非你确实需要「记住每一个键」这个精度，\n"
                     "并且清楚上面这些代价。\n")
        body += "\n门 1：下面这段字读完、按钮才会亮（10 秒）。"
        ttk.Label(win, text=body, justify="left", padding=(16, 0, 16, 10)).pack(anchor="w")

        count_lbl = ttk.Label(win, text="还需等待 10 秒…", padding=(16, 0, 16, 8))
        count_lbl.pack(anchor="w")

        btn_continue = ttk.Button(win, text="继续（暂不可点）", state="disabled")
        btn_continue.pack(fill="x", padx=16, pady=(0, 10))

        step2 = ttk.Frame(win, padding=(16, 0, 16, 12))
        ttk.Label(step2, text="门 2：手动输入「我明白」四个字（不能复制粘贴）").pack(anchor="w")
        entry = ttk.Entry(step2)
        entry.pack(fill="x", pady=6)
        btn_confirm = ttk.Button(step2, text="开启档 5", state="disabled")
        btn_confirm.pack(fill="x")

        remaining = {"v": 10}

        def tick():
            remaining["v"] -= 1
            if remaining["v"] > 0:
                count_lbl.config(text=f"还需等待 {remaining['v']} 秒…")
                win.after(1000, tick)
            else:
                count_lbl.config(text="门 1 通过。")
                btn_continue.config(text="继续", state="normal")

        def on_continue():
            btn_continue.config(state="disabled")
            step2.pack(fill="x")
            entry.focus_set()

        def on_entry(*_):
            btn_confirm.config(state="normal" if entry.get().strip() == "我明白" else "disabled")

        def on_confirm():
            ok["value"] = True
            win.grab_release()
            win.destroy()

        btn_continue.config(command=on_continue)
        entry.bind("<KeyRelease>", on_entry)
        btn_confirm.config(command=on_confirm)

        win.after(1000, tick)
        win.wait_window()
        return ok["value"]

    def refresh(self):
        tier = getattr(self.collector, "tier", 1)
        if getattr(self.collector, "running", False):
            self.status.config(text=f"状态：采集中（档 {tier}，{self.collector.interval}s 聚合）", foreground="#a00")
        elif self.running:
            self.status.config(text=f"状态：运行中（档 {tier}，未采集）", foreground="#0a0")
        else:
            self.status.config(text=f"状态：待机（档 {tier}，未采集）", foreground="#555")

    def toggle(self):
        self.running = not self.running
        self.refresh()

    # ---------- 托盘 ----------
    def _tray_icon_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([6, 6, 58, 58], radius=14, fill=(16, 29, 48, 255),
                            outline=(120, 180, 255, 255), width=3)
        d.ellipse([24, 24, 40, 40], fill=(78, 161, 255, 255))
        return img

    def setup_tray(self):
        if not HAS_TRAY:
            print("[warn] 没装 pystray，托盘图标不可用", flush=True)
            return
        def menu():
            collecting = getattr(self.collector, "running", False)
            return pystray.Menu(
                pystray.MenuItem("显示窗口", lambda: self.root.after(0, self.show_window), default=True),
                pystray.MenuItem("打开面板", lambda: self.root.after(0, self.open_panel)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "停止采集" if collecting else "开始采集",
                    lambda: self.root.after(0, self._tray_toggle_collect),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda: self.root.after(0, self.quit)),
            )
        self.tray = pystray.Icon("tactile", self._tray_icon_image(), "tactile · 触觉", menu())
        self.tray.run_detached()
        print("[ok] 托盘图标已就绪", flush=True)

    def _tray_toggle_collect(self):
        self.collect_var.set(not getattr(self.collector, "running", False))
        self.toggle_collect()

    def hide_to_tray(self):
        self.root.withdraw()
        print("[ok] 收进托盘", flush=True)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))

    def quit(self):
        try:
            self.unregister_hotkey()
        except Exception:
            pass
        self.running = False
        try:
            self.collector.stop()
        except Exception:
            pass
        try:
            if getattr(self, "tray", None):
                self.tray.stop()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        finally:
            print("window destroyed", flush=True)

    def run(self):
        print(f"{APP} {VERSION} started pid={os.getpid()}")
        self.root.mainloop()
        print("exited cleanly")

def acquire_single_instance():
    """单实例锁：占用一个固定本地端口。已被占用就返回 False。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 18091))
        s.listen(1)
        globals()["_LOCK_SOCK"] = s          # 保持住，进程活着就一直占着
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False


if __name__ == "__main__":
    if not acquire_single_instance():
        print("[warn] 已经有一个 tactile 在运行，本次启动退出", flush=True)
        sys.exit(0)
    App().run()
