#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G09 智慧吸塵車：樹莓派控制伺服器（port 5000）

功能
  1. 提供控制網頁：瀏覽器打開 http://樹莓派IP:5000/ 就會看到 index.html
  2. 接收網頁指令：POST /api/command  {"cmd": "F", "speed": 180}
     轉成一行文字送給 Arduino："F180\n"（S、V、v、1、2、3 不帶數字）
  3. 回報狀態：GET /api/status  → 最後指令、Arduino 是否連線（超音波只在 Arduino 內部判斷，不回傳）
  4. 斷線保護：超過 0.5 秒沒收到移動指令 → 自動送出 S（停車）

執行：python3 app.py
沒接 Arduino 也能執行（模擬模式），終端機會印出本來要送的指令。
"""
import glob
import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial          # sudo apt install -y python3-serial
except ImportError:
    serial = None

PORT = 5000
BAUD_RATE = 115200
WATCHDOG_SEC = 0.5          # 這麼久沒收到移動指令就停車
MOTION_CMDS = set('FBLR')
ALLOWED_CMDS = set('FBLRSVv123')
HERE = os.path.dirname(os.path.abspath(__file__))

state = {
    "last_cmd": "S",
    "speed": 180,
    "vacuum": False,
    "arduino": False,        # 是否連上 Arduino
    "port": None,
}
last_motion_time = 0.0
ser = None
ser_lock = threading.Lock()
last_printed = None          # 同一個指令重複送時只印一次，畫面才不會洗版


def find_port():
    """自動尋找 Arduino：/dev/ttyACM*（原廠 Uno）或 /dev/ttyUSB*（相容板）"""
    ports = sorted(glob.glob('/dev/ttyACM*')) + sorted(glob.glob('/dev/ttyUSB*'))
    return ports[0] if ports else None


def connect():
    """連接 Arduino；失敗就維持模擬模式，之後每 2 秒重試"""
    global ser
    if serial is None:
        return
    port = find_port()
    if not port:
        return
    try:
        s = serial.Serial(port, BAUD_RATE, timeout=0.2)
        time.sleep(2)               # Arduino 連線時會自動重開機，等它準備好
        s.reset_input_buffer()
        ser = s
        state["arduino"], state["port"] = True, port
        print(f"[OK] 已連上 Arduino：{port}")
    except Exception as e:
        print(f"[WARN] 無法開啟 {port}：{e}")


def send_line(line):
    """送一行指令給 Arduino；沒連上時只印出來（模擬模式）"""
    global ser, last_printed
    show = line != last_printed
    last_printed = line
    with ser_lock:
        if ser is not None:
            try:
                ser.write((line + '\n').encode())
                if show:
                    print(f"→ Arduino  {line}")
                return
            except Exception as e:
                print(f"[WARN] 傳送失敗，Arduino 可能被拔掉了：{e}")
                ser = None
                state["arduino"], state["port"] = False, None
        if show:
            print(f"→ (模擬)   {line}")


def serial_reader():
    """背景執行：維持和 Arduino 的連線，被拔掉時自動重連（Arduino 不回傳距離，讀到的訊息直接略過）"""
    global ser
    while True:
        if ser is None:
            connect()
            if ser is None:
                time.sleep(2)
                continue
        try:
            ser.readline()          # 只用來偵測連線是否還在
        except Exception:
            with ser_lock:
                ser = None
            state["arduino"], state["port"] = False, None
            print("[WARN] Arduino 連線中斷，重新尋找中…")
            time.sleep(1)


def watchdog():
    """背景執行：網頁每 0.15 秒會重送一次移動指令；超過 0.5 秒沒收到 → 停車"""
    while True:
        time.sleep(0.1)
        if state["last_cmd"] in MOTION_CMDS and time.time() - last_motion_time > WATCHDOG_SEC:
            print("[SAFE] 0.5 秒沒收到指令（網頁關掉或網路斷了）→ 自動停車")
            state["last_cmd"] = 'S'
            send_line('S')


def handle_command(cmd, speed):
    global last_motion_time
    if cmd in MOTION_CMDS:
        speed = max(0, min(255, int(speed)))
        state["speed"] = speed
        last_motion_time = time.time()
        line = f"{cmd}{speed}"
    else:
        line = cmd
    if cmd == 'V':
        state["vacuum"] = True
    elif cmd == 'v':
        state["vacuum"] = False
    if cmd in 'FBLRS':
        state["last_cmd"] = cmd
    send_line(line)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def log_message(self, fmt, *args):      # 不要每個請求都印一行，畫面才看得清楚
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith('/api/status'):
            return self.send_json(state)
        if self.path in ('/', ''):
            self.path = '/index.html'
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith('/api/command'):
            return self.send_json({"status": "error", "msg": "unknown path"}, 404)
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length).decode() or '{}')
            cmd = str(body.get('cmd', 'S'))[:1]
            if cmd not in ALLOWED_CMDS:
                return self.send_json({"status": "error", "msg": f"unknown cmd {cmd}"}, 400)
            handle_command(cmd, body.get('speed', state["speed"]))
            self.send_json({"status": "ok", "sent": cmd, "telemetry": state})
        except Exception as e:
            self.send_json({"status": "error", "msg": str(e)}, 400)


if __name__ == '__main__':
    if serial is None:
        print("[WARN] 沒有安裝 pyserial，只能模擬。安裝：sudo apt install -y python3-serial")
    threading.Thread(target=serial_reader, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f"控制伺服器啟動：瀏覽器打開 http://<樹莓派IP>:{PORT}/   （按 Ctrl+C 結束）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        send_line('S')
        send_line('v')
        print("\n已停車並關閉吸塵，伺服器結束")
