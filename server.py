# -*- coding: utf-8 -*-
"""
tactile · 本地 HTTP（只绑 127.0.0.1）

  GET /            → 极小面板（HTML，5 秒自动刷新）
  GET /state       → 当前状态
  GET /stats?m=60  → 最近 m 分钟的聚合
  GET /off         → 停止采集（进程留着）
  GET /quit        → 退出整个程序

端口写进 port.txt，别的程序靠它找过来。
"""
import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime

PANEL = """<!doctype html><meta charset="utf-8">
<title>tactile</title>
<style>
body{font:14px/1.6 system-ui;margin:16px;background:#111;color:#ddd}
h2{margin:0 0 8px;font-size:16px}
.k{color:#888}.v{color:#7ee2b8}
.bar{height:8px;background:#222;margin:2px 0;display:flex}
.bar i{display:block;background:#3a7}
a{color:#7ac}
</style>
<h2>tactile <span style="color:#888;font-size:12px">刷新 <span id=rl></span></span></h2>
<div style="margin-bottom:8px">刷新速度：
<a href="?r=0.5">0.5s</a> ·
<a href="?r=1">1s</a> ·
<a href="?r=2">2s</a> ·
<a href="?r=5">5s</a> ·
<a href="?r=15">15s</a> ·
<a href="?r=60">60s</a></div>
<div id=st>loading…</div>
<hr style="border-color:#333">
<div id=cnt></div>
<script>
const R = __R__;
document.getElementById('rl').textContent = R + 's';
async function tick(){
  try{
    const d = await (await fetch('/state')).json();
    document.getElementById('st').innerHTML =
      '<span class=k>状态</span> <span class=v>'+(d.collecting?'采集中':'待机')+'</span><br>'+
      '<span class=k>档位</span> '+d.tier+'　<span class=k>间隔</span> '+d.interval+'s<br>'+
      '<span class=k>空闲</span> '+d.idle_seconds+'s<br>'+
      '<span class=k>最近一条</span> <code>'+JSON.stringify(d.last)+'</code>';
    const s = await (await fetch('/stats?m=60')).json();
    let h='<span class=k>最近 '+s.minutes+' 分钟：'+s.count+' 条</span><br>';
    for(const r of s.rows.slice(-40)){
      const w=Math.min(100, Math.round((r.keys||0)*2));
      const p=Math.min(100, Math.round((r.mousePx||0)/50));
      h+='<div class=bar><i style="width:'+w+'%;background:#5a8"></i><i style="width:'+p+'%;background:#48f"></i></div>';
    }
    document.getElementById('cnt').innerHTML=h+'<span class=k>绿=按键密度　蓝=鼠标移动</span>';
  }catch(e){ document.getElementById('st').textContent='连接失败: '+e; }
}
tick(); setInterval(tick, R*1000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    app = None          # 由 start() 注入

    def log_message(self, *a):
        pass            # 静音访问日志

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        a = self.app
        if u.path == "/":
            try:
                r = float((parse_qs(u.query).get("r") or ["5"])[0])
            except ValueError:
                r = 5.0
            r = max(0.5, min(60.0, r))
            body = PANEL.replace("__R__", str(r)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/state":
            c = a.collector
            self._json({
                "collecting": bool(getattr(c, "running", False)),
                "tier": 1,
                "interval": c.interval,
                "idle_seconds": round(c._idle_seconds(), 1),
                "last": c.last_line,
                "pid": os.getpid(),
            })
        elif u.path == "/stats":
            m = int((parse_qs(u.query).get("m") or ["60"])[0])
            self._json(a.stats(minutes=m))
        elif u.path == "/clear":
            keep = (parse_qs(u.query).get("log") or ["1"])[0] == "1"
            self._json(dict(a.collector.clear_data(keep_log=keep), ok=True))
        elif u.path == "/rules":
            self._json(dict(a.get_ask_rules(), ok=True))
        elif u.path == "/on":
            q = parse_qs(u.query)
            try:
                iv = float((q.get("interval") or ["60"])[0])
            except ValueError:
                iv = 60.0
            a.collector.set_interval(iv)
            a.collector.start()
            self._json({"ok": True, "collecting": True, "interval": a.collector.interval})
        elif u.path == "/off":
            a.collector.stop()
            self._json({"ok": True, "collecting": False})
        elif u.path == "/quit":
            self._json({"ok": True, "bye": True})
            threading.Thread(target=a.on_quit, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)


def start(app, port=18090, port_file=None):
    """启动 HTTP（后台线程）。返回实际端口。"""
    Handler.app = app
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if port_file:
        try:
            with open(port_file, "w", encoding="utf-8") as f:
                f.write(str(actual))
        except Exception:
            pass
    print(f"[ok] HTTP 在 127.0.0.1:{actual}（port.txt 已写）", flush=True)
    return srv, actual
