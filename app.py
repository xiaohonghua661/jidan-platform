# -*- coding: utf-8 -*-
"""记单平台看板：只读本地网页。双击「启动记单平台.bat」即可。写入走 ledger.py。"""
import json, subprocess, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import ledger

HOST, PORT0 = "127.0.0.1", 8765


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会让第二个实例也绑上同一端口，「已在运行」就判断不出来
    allow_reuse_address = sys.platform != "win32"


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/orders":
            try:
                cats, chans = ledger.load_cfg()  # 每次重读配置，改了配置不用重启看板
                # 「8086」这种纯数字键在浏览器里会被排到最前，顺序单独给一份
                self._json(200, {"类别": cats, "类别顺序": list(cats), "渠道": chans, "订单": ledger.all_rows(),
                                 "手动改动": ledger.pending_manual()})
            except Exception as e:  # 配置或数据文件读坏了，也给页面一个说法
                self._json(500, {"错误": f"{type(e).__name__}: {e}"})
        elif path in ("/", "/index.html"):
            self._send(200, (ledger.HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if self.headers.get("X-Jidan") != "1":   # 要带自定义头，别的网页跨站发不了
            self._json(403, {"错误": "缺少请求头 X-Jidan: 1（只接受看板页面发来的修改）"})
            return
        if path == "/api/set":
            # 看板上用户手动改环节（√ / X / 空）：走 ledger 同一把锁写入，并记一笔等 AI 跟用户确认
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 10000)).decode("utf-8") or "{}")
                cat, no = body.get("类别"), body.get("单号")
                if body.get("字段"):                     # 备注 / 期限 / 价格 / 定金 / 尾款
                    e = ledger.manual_set(cat, no, field=body["字段"], text=str(body.get("文本") or ""))
                elif "文本" in body:                     # 环节里写一段字（日期、快递号…）
                    e = ledger.manual_set(cat, no, stage=body.get("环节"), text=str(body.get("文本") or ""))
                elif body.get("值") in ledger.VALUE_NAME:  # 环节 √ / X / 空
                    e = ledger.manual_set(cat, no, stage=body.get("环节"), value=ledger.VALUE_NAME[body["值"]])
                else:
                    raise ValueError("值只能是 完成 / 不做 / 未做")
                self._json(200, {"ok": True, "改动": e})
            except (ValueError, RuntimeError) as e:
                self._json(400, {"错误": str(e)})
            return
        if path == "/api/remind":
            try:
                pyw = Path(sys.executable).with_name("pythonw.exe")
                subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(ledger.HERE / "remind.py")],
                                 cwd=str(ledger.HERE), close_fds=True, creationflags=0x00000008 | 0x00000200)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"错误": f"{type(e).__name__}: {e}"})
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    no_browser = "--no-browser" in sys.argv
    ports = range(PORT0, PORT0 + 10)
    if "--port" in sys.argv:  # 自测用：只占指定端口，不碰用户开着的那个
        ports = [int(sys.argv[sys.argv.index("--port") + 1])]
    for port in ports:
        try:
            srv = Server((HOST, port), H)
            break
        except OSError:
            # 已有一个平台在跑：直接打开它
            try:
                urlopen(f"http://{HOST}:{port}/api/orders", timeout=1).read()
                if not no_browser:
                    webbrowser.open(f"http://{HOST}:{port}/")
                print(f"记单平台已在运行：http://{HOST}:{port}/")
                return
            except Exception:
                continue
    else:
        sys.exit(f"端口 {ports[0]}-{ports[-1]} 都被占用")
    url = f"http://{HOST}:{port}/"
    print(f"记单平台已启动：{url}\n关闭本窗口即停止。")
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
