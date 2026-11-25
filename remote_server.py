# -*- coding: utf-8 -*-
"""
HJ 프레젠테이션 리모컨 - 노트북(윈도우)용 프로그램
- 폰 브라우저에서 PowerPoint 슬라이드 넘기기, 음량, 미디어 플레이어 재생/정지, 레이저 포인터 조작
- 연결 방식 2가지
  1) 직접 연결: 노트북과 폰이 같은 와이파이(LTE 동글, 노트북 핫스팟, 강의장 와이파이)
  2) 인터넷 중계: 노트북과 폰이 서로 다른 네트워크여도 둘 다 인터넷만 되면 연결 (MQTT 중계 서버 경유)
"""
import asyncio
import json
import os
import queue
import secrets
import socket
import string
import sys
import threading
import time
import webbrowser

from aiohttp import web
import segno

try:
    import paho.mqtt.client as mqtt
except Exception:  # 중계 기능만 꺼짐
    mqtt = None

IS_WIN = sys.platform == "win32"
PORT = 8765
VERSION = "1.1"


# ------------------------------------------------------------------ 경로/설정
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def res_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


CONFIG_PATH = os.path.join(app_dir(), "hj_remote_config.json")
DEFAULT_CONFIG = {
    "port": PORT,
    # 인터넷 중계용 폰 페이지 주소 (remote.html 을 GitHub Pages 등에 올린 주소)
    "relay_page_url": "https://coachhj.github.io/remote/remote.html",
    "mqtt_path": "/mqtt",
    "relay_enabled": True,
    "volume_step": 2,          # 버튼 한 번에 올리거나 내릴 단계(윈도우 기준 1단계 = 2%)
}


def rand_id(n):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("설정 파일을 읽지 못해 기본값을 씁니다:", e)
    changed = False
    if not cfg.get("key"):
        cfg["key"] = rand_id(10)
        changed = True
    if not cfg.get("room"):
        cfg["room"] = rand_id(14)
        changed = True
    if changed or not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("설정 파일을 저장하지 못했습니다:", e)
    return cfg


CFG = load_config()


# ------------------------------------------------------------------ 윈도우 조작
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))

    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    MOUSEEVENTF_MOVE = 0x0001
    EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0xAD, 0xAE, 0xAF, 0xB3}

    def _key(vk, up=False):
        flags = KEYEVENTF_KEYUP if up else 0
        if vk in EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        return i

    def press(*vks):
        """vks 를 순서대로 누르고 역순으로 뗀다 (예: Ctrl+L)"""
        seq = [_key(v) for v in vks] + [_key(v, True) for v in reversed(vks)]
        arr = (INPUT * len(seq))(*seq)
        user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))

    def mouse_move(dx, dy):
        i = INPUT(type=0)
        i.mi = MOUSEINPUT(dx=int(dx), dy=int(dy), mouseData=0, dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=0)
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))

    def center_cursor_on_slideshow():
        hwnd = user32.FindWindowW("screenClass", None)  # PowerPoint 슬라이드쇼 창
        if not hwnd:
            return False
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        user32.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        return True
else:  # 개발/테스트용 (윈도우가 아닐 때는 동작만 기록)
    def press(*vks):
        print("[테스트] 키 입력", [hex(v) for v in vks])

    def mouse_move(dx, dy):
        pass

    def center_cursor_on_slideshow():
        print("[테스트] 커서 가운데로")
        return True

VK_LEFT, VK_RIGHT = 0x25, 0x27
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_CONTROL, VK_L = 0x11, 0x4C


class PowerPoint:
    """COM 으로 PowerPoint 슬라이드쇼를 직접 제어 (창 포커스와 무관하게 동작)"""

    def __init__(self):
        self.ok = False
        if IS_WIN:
            try:
                import pythoncom  # noqa
                import win32com.client  # noqa
                self.ok = True
            except Exception:
                print("pywin32 가 없어 PowerPoint 직접 제어 대신 키 입력을 씁니다.")

    def _view(self):
        if not self.ok:
            return None, None
        import win32com.client
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
        except Exception:
            return None, None
        try:
            if app.SlideShowWindows.Count > 0:
                w = app.SlideShowWindows(1)
                return w.View, w.Presentation
        except Exception:
            pass
        return None, None

    def go(self, direction):
        view, _ = self._view()
        if view is not None:
            try:
                view.Next() if direction > 0 else view.Previous()
                return True
            except Exception:
                pass
        press(VK_RIGHT if direction > 0 else VK_LEFT)
        return False

    def laser(self):
        view, _ = self._view()
        if view is not None:
            try:
                view.LaserPointerEnabled = not view.LaserPointerEnabled
                return bool(view.LaserPointerEnabled)
            except Exception:
                pass
        press(VK_CONTROL, VK_L)
        return None

    def state(self):
        view, pres = self._view()
        if view is None:
            return {"show": False}
        try:
            pos = int(view.CurrentShowPosition)
            total = int(pres.Slides.Count)
            laser = None
            try:
                laser = bool(view.LaserPointerEnabled)
            except Exception:
                pass
            return {"show": True, "slide": pos, "total": total, "laser": laser}
        except Exception:
            return {"show": True}


def media_play_pause():
    """윈도우 미디어 플레이어만 골라서 재생/정지. 반환값: 사용한 방법 설명"""
    if not IS_WIN:
        print("[테스트] 미디어 재생/정지")
        return "테스트"
    # 1) 새 '미디어 플레이어'(윈도우 11) - 시스템 미디어 세션에서 해당 앱만 골라 제어
    try:
        from winrt.windows.media.control import \
            GlobalSystemMediaTransportControlsSessionManager as SessionManager

        async def _toggle():
            mgr = await SessionManager.request_async()
            for s in mgr.get_sessions():
                app_id = (s.source_app_user_model_id or "").lower()
                if "zunemusic" in app_id or "wmplayer" in app_id or "mediaplayer" in app_id:
                    await s.try_toggle_play_pause_async()
                    return True
            return False

        if asyncio.run(_toggle()):
            return "미디어 플레이어"
    except Exception:
        pass
    # 2) 예전 'Windows Media Player' - 창에 직접 재생/정지 명령
    hwnd = user32.FindWindowW("WMPlayerApp", None)
    if hwnd:
        user32.SendMessageW(hwnd, 0x0111, 0x4978, 0)  # WM_COMMAND, 재생/정지
        return "Windows Media Player"
    # 3) 마지막 수단 - 키보드의 재생/정지 키
    press(VK_MEDIA_PLAY_PAUSE)
    return "재생/정지 키"


# ------------------------------------------------------------------ 명령 처리(단일 작업 스레드)
class Worker:
    def __init__(self):
        self.q = queue.Queue()
        self.ppt = None
        self.listeners = []  # 상태 변경을 받을 콜백(dict) 목록
        self.lock = threading.Lock()
        self.last_state = {}
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, cmd):
        self.q.put(cmd)

    def broadcast(self, msg):
        with self.lock:
            cbs = list(self.listeners)
        for cb in cbs:
            try:
                cb(msg)
            except Exception:
                pass

    def _run(self):
        if IS_WIN:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
        self.ppt = PowerPoint()
        last_poll = 0
        while True:
            try:
                cmd = self.q.get(timeout=1.0)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                try:
                    self._handle(cmd)
                except Exception as e:
                    print("명령 처리 오류:", cmd, e)
            now = time.time()
            if now - last_poll > 2.0:
                last_poll = now
                self._push_state()

    def _push_state(self, force=False):
        try:
            st = self.ppt.state()
        except Exception:
            st = {"show": False}
        if force or st != self.last_state:
            self.last_state = st
            self.broadcast({"t": "state", **st})

    def _handle(self, cmd):
        c = cmd.get("c")
        if c == "move":
            # 쌓인 이동 명령을 한 번에 합쳐 처리
            dx, dy = float(cmd.get("dx", 0)), float(cmd.get("dy", 0))
            while True:
                try:
                    nxt = self.q.get_nowait()
                except queue.Empty:
                    break
                if nxt.get("c") == "move":
                    dx += float(nxt.get("dx", 0))
                    dy += float(nxt.get("dy", 0))
                else:
                    if dx or dy:
                        mouse_move(dx, dy)
                    dx = dy = 0
                    self._handle(nxt)
            if dx or dy:
                mouse_move(dx, dy)
            return
        if c == "next":
            self.ppt.go(+1)
            self._push_state(force=True)
        elif c == "prev":
            self.ppt.go(-1)
            self._push_state(force=True)
        elif c == "volup":
            for _ in range(int(CFG.get("volume_step", 2))):
                press(VK_VOLUME_UP)
            self.broadcast({"t": "toast", "msg": "음량 +"})
        elif c == "voldown":
            for _ in range(int(CFG.get("volume_step", 2))):
                press(VK_VOLUME_DOWN)
            self.broadcast({"t": "toast", "msg": "음량 −"})
        elif c == "mute":
            press(VK_VOLUME_MUTE)
            self.broadcast({"t": "toast", "msg": "음소거 전환"})
        elif c == "media":
            how = media_play_pause()
            self.broadcast({"t": "toast", "msg": f"재생/정지 ({how})"})
        elif c == "laser":
            on = self.ppt.laser()
            msg = "레이저 켜짐" if on else ("레이저 꺼짐" if on is False else "레이저 전환(Ctrl+L)")
            self.broadcast({"t": "toast", "msg": msg})
            self._push_state(force=True)
        elif c == "center":
            ok = center_cursor_on_slideshow()
            self.broadcast({"t": "toast", "msg": "포인터를 화면 가운데로" if ok else "슬라이드쇼 화면을 못 찾았어요"})
        elif c == "state":
            self._push_state(force=True)


WORKER = Worker()
ALLOWED = {"next", "prev", "volup", "voldown", "mute", "media", "laser", "center", "move", "state"}


def accept(cmd):
    if isinstance(cmd, dict) and cmd.get("c") in ALLOWED:
        WORKER.submit(cmd)


# ------------------------------------------------------------------ 인터넷 중계(MQTT)
DEFAULT_BROKERS = ["broker.emqx.io:8084", "broker.hivemq.com:8884", "test.mosquitto.org:8081"]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


class BrokerConn:
    """중계 서버 하나와의 연결"""

    def __init__(self, entry, relay):
        self.relay = relay
        self.host, _, port = entry.partition(":")
        self.port = int(port or 8084)
        self.name = self.host.split(".")[-2] if self.host.count(".") >= 1 else self.host
        self.status = "연결 중"
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="hjpc-" + rand_id(8), transport="websockets")
        cl.ws_set_options(path=CFG.get("mqtt_path", "/mqtt"))
        if CFG.get("mqtt_tls", True):
            cl.tls_set()
        cl.reconnect_delay_set(2, 30)
        cl.on_connect = self._on_connect
        cl.on_disconnect = self._on_disconnect
        cl.on_message = relay.on_message
        cl.on_connect_fail = self._on_fail
        self.client = cl
        cl.connect_async(self.host, self.port, keepalive=30)
        cl.loop_start()

    def _set(self, st):
        if st != self.status:
            self.status = st
            log(f"[인터넷 중계] {self.host}:{self.port} → {st}")

    def _on_connect(self, cl, userdata, flags, rc, props=None):
        if not rc.is_failure:
            cl.subscribe(self.relay.t_cmd, qos=0)
            self._set("연결됨")
        else:
            self._set(f"거부됨({rc})")

    def _on_fail(self, *a, **k):
        self._set("접속 실패(이 네트워크에서 막혔을 수 있음)")

    def _on_disconnect(self, cl, userdata, flags, rc, props=None):
        self._set("끊김(재시도 중)" if self.status == "연결됨" else "접속 실패(이 네트워크에서 막혔을 수 있음)")

    def publish(self, payload):
        if self.status == "연결됨":
            self.client.publish(self.relay.t_state, payload, qos=0)


class Relay:
    def __init__(self):
        self.conns = []
        self.enabled = bool(CFG.get("relay_enabled")) and mqtt is not None
        if not self.enabled:
            return
        base = f"hjremote/{CFG['room']}"
        self.t_cmd, self.t_state = base + "/c", base + "/s"
        self.last_phone = 0
        for entry in self.brokers():
            try:
                self.conns.append(BrokerConn(entry, self))
            except Exception as e:
                log(f"[인터넷 중계] {entry} 준비 실패: {e}")
        with WORKER.lock:
            WORKER.listeners.append(self.publish)

    @staticmethod
    def brokers():
        b = CFG.get("brokers")
        if isinstance(b, list) and b:
            return b
        return DEFAULT_BROKERS

    @property
    def status(self):
        if not self.enabled:
            return "꺼짐"
        ok = [c.name for c in self.conns if c.status == "연결됨"]
        if ok:
            return "연결됨(" + ", ".join(ok) + ")"
        return "중계 서버에 접속 못 함 — 노트북 네트워크 확인"

    def detail(self):
        return [{"host": c.host, "port": c.port, "status": c.status} for c in self.conns]

    def on_message(self, cl, userdata, m):
        try:
            data = json.loads(m.payload.decode("utf-8"))
        except Exception:
            return
        if data.get("k") != CFG["key"]:
            return
        if data.get("c") == "hello":
            if time.time() - self.last_phone > 60:
                log("[인터넷 중계] 폰이 연결되었습니다")
            self.last_phone = time.time()
            cl.publish(self.t_state, json.dumps({"t": "hello", "v": VERSION}), qos=0)
            WORKER.submit({"c": "state"})
            return
        self.last_phone = time.time()
        accept(data)

    def publish(self, msg):
        payload = json.dumps(msg, ensure_ascii=False)
        for c in self.conns:
            c.publish(payload)


# ------------------------------------------------------------------ 웹 서버
def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    good = [ip for ip in ips if not ip.startswith("127.") and not ip.startswith("169.254.")]

    def rank(ip):  # 보통 와이파이/핫스팟 대역을 앞으로
        return (0 if ip.startswith("192.168.") else 1 if ip.startswith("172.") else 2, ip)
    return sorted(good, key=rank)


def local_url(ip):
    return f"http://{ip}:{CFG['port']}/?k={CFG['key']}"


def relay_url():
    return f"{CFG['relay_page_url']}?r={CFG['room']}&k={CFG['key']}&b={','.join(Relay.brokers())}"


def qr_svg(text):
    return segno.make(text, error="m").svg_inline(scale=6, dark="#111", light="#fff", border=2)


ws_clients = set()


async def handle_index(request):
    with open(res_path("remote.html"), "rb") as f:
        return web.Response(body=f.read(), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})


async def handle_ws(request):
    if request.query.get("k") != CFG["key"]:
        return web.Response(status=403, text="열쇠가 맞지 않습니다")
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()

    def send(msg):
        if not ws.closed:
            asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    with WORKER.lock:
        WORKER.listeners.append(send)
    ws_clients.add(ws)
    log(f"[같은 와이파이] 폰 연결됨 ({request.remote})")
    await ws.send_json({"t": "hello", "v": VERSION})
    WORKER.submit({"c": "state"})
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    accept(json.loads(msg.data))
                except Exception:
                    pass
    finally:
        ws_clients.discard(ws)
        log(f"[같은 와이파이] 폰 연결 끊김 ({request.remote})")
        with WORKER.lock:
            if send in WORKER.listeners:
                WORKER.listeners.remove(send)
    return ws


def is_local(request):
    return request.remote in ("127.0.0.1", "::1")


async def handle_admin(request):
    if not is_local(request):
        return web.Response(status=403, text="노트북에서만 볼 수 있습니다")
    return web.Response(text=admin_html(), content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


async def handle_status(request):
    if not is_local(request):
        return web.Response(status=403)
    return web.json_response({"phones": len(ws_clients), "relay": RELAY.status, "detail": RELAY.detail(), "ips": local_ips()})


def admin_html():
    ips = local_ips()
    cards = []
    for ip in ips:
        cards.append(f"""<div class="card"><h3>같은 와이파이로 연결 <small>{ip}</small></h3>{qr_svg(local_url(ip))}
<p class="url">{local_url(ip)}</p><p class="hint">폰이 이 와이파이에 연결되어 있어야 합니다. 안 열리면 방화벽_허용.bat 을 한 번 실행하세요.</p></div>""")
    if not ips:
        cards.append('<div class="card"><h3>같은 와이파이로 연결</h3><p>네트워크에 연결되어 있지 않습니다.</p></div>')
    if RELAY.enabled:
        cards.append(f"""<div class="card relay"><h3>인터넷 중계로 연결 <small id="rs">{RELAY.status}</small></h3>{qr_svg(relay_url())}
<p class="url">{relay_url()}</p><p class="hint">폰과 노트북이 서로 다른 네트워크여도 둘 다 인터넷이 되면 됩니다.</p><div id="rd" class="hint"></div></div>""")
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>HJ 리모컨</title>
<style>
body{{font-family:'Malgun Gothic',sans-serif;background:#0f1115;color:#e8e8e8;margin:0;padding:24px}}
h1{{margin:0 0 4px;font-size:22px}} .sub{{color:#9aa;margin:0 0 20px}}
.wrap{{display:flex;flex-wrap:wrap;gap:20px}}
.card{{background:#1b1f27;border-radius:14px;padding:18px;width:320px}}
.card h3{{margin:0 0 12px;font-size:16px}} .card h3 small{{color:#7fd1a8;font-weight:normal;margin-left:6px}}
.card svg{{width:100%!important;height:auto!important;border-radius:8px}}
.url{{font-size:11px;color:#8a93a3;word-break:break-all}} .hint{{font-size:13px;color:#b9c0cc}}
#st{{margin-top:18px;font-size:15px}} b{{color:#7fd1a8}}
</style></head><body>
<h1>HJ 프레젠테이션 리모컨</h1>
<p class="sub">폰 카메라로 QR을 찍어 접속하세요. 이 창은 닫아도 되고, 프로그램을 끝낼 때는 검은 콘솔 창을 닫으세요.</p>
<div class="wrap">{''.join(cards)}</div>
<div id="st">연결된 폰(같은 와이파이): <b id="n">0</b>대</div>
<script>
setInterval(async()=>{{try{{const r=await fetch('/status');const j=await r.json();
document.getElementById('n').textContent=j.phones;const rs=document.getElementById('rs');if(rs)rs.textContent=j.relay;
const rd=document.getElementById('rd');if(rd&&j.detail)rd.innerHTML=j.detail.map(d=>`${{d.status==='연결됨'?'🟢':'🔴'}} ${{d.host}}:${{d.port}} — ${{d.status}}`).join('<br>');}}catch(e){{}}}},2000);
</script></body></html>"""


RELAY = None


def main():
    global RELAY
    os.system("title HJ 프레젠테이션 리모컨" if IS_WIN else "")
    RELAY = Relay()
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/remote.html", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/admin", handle_admin)
    app.router.add_get("/status", handle_status)

    port = int(CFG["port"])
    print("=" * 56)
    print(" HJ 프레젠테이션 리모컨", VERSION)
    print("=" * 56)
    for ip in local_ips():
        print(" 같은 와이파이 :", local_url(ip))
    if RELAY.enabled:
        print(" 인터넷 중계   :", relay_url())
    print(" 노트북 화면에 QR 코드 창이 열립니다. (http://localhost:%d/admin)" % port)
    print(" 끝내려면 이 창을 닫으세요.")
    print("=" * 56)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}/admin")).start()
    web.run_app(app, host="0.0.0.0", port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
