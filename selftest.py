# -*- coding: utf-8 -*-
"""自证：列出所有 tactile 进程。退出码 0 = 没有残留。"""
import subprocess, sys, re

# 只找 python.exe —— 否则 powershell 自己的命令行里也含 tactile.py，会算成"有残留"
PS = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
      "Where-Object { $_.CommandLine -like '*tactile.py*' } | "
      "Select-Object ProcessId,Name | ConvertTo-Json -Compress")

def main():
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                       capture_output=True, text=True, timeout=30)
    out = (r.stdout or "").strip()
    if not out:
        print("[OK] 没有残留进程")
        return 0
    import json
    try:
        data = json.loads(out)
    except Exception:
        print(out); print("[WARN] 解析失败，请人工确认"); return 2
    if isinstance(data, dict): data = [data]
    print(f"[FAIL] 还有 {len(data)} 个进程：")
    for d in data:
        print(f"   pid={d.get('ProcessId')}  {d.get('Name')}")
    return 1

if __name__ == "__main__":
    sys.exit(main())
