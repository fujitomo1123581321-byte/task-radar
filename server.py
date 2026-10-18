"""
ローカル課題管理サーバー (Fast & Lightweight)
assignments.json の読み書きAPI付き
"""
import http.server
import socketserver
import json
import os
import sys
import webbrowser
import socket

# Ensure utf-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

PORT = 8085
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
JS_PATH = os.path.join(BASE_DIR, "assignments.js")

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

class AssignmentHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        # Block unauthorized access to sensitive files
        forbidden_patterns = ["config.json", ".env", ".git", ".vbs", ".bat"]
        normalized_path = self.path.lower().split("?")[0]
        if any(p in normalized_path for p in forbidden_patterns) or (normalized_path.endswith(".py") and not normalized_path.startswith("/api/")):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"403 Forbidden: Access to sensitive file is prohibited.")
            return

        if self.path == "/api/info":
            local_ip = get_local_ip()
            info = {
                "local_ip": local_ip,
                "port": PORT,
                "mobile_url": f"http://{local_ip}:{PORT}/index.html",
                "local_url": f"http://localhost:{PORT}/index.html"
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(info, ensure_ascii=False).encode("utf-8"))
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/assignments":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                content_length = 0
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
                # Atomic Save JSON
                temp_json = JSON_PATH + ".tmp"
                with open(temp_json, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp_json, JSON_PATH)

                # Also save JS for file:// fallback
                temp_js = JS_PATH + ".tmp"
                with open(temp_js, "w", encoding="utf-8") as f:
                    f.write("window.INITIAL_ASSIGNMENTS = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n")
                os.replace(temp_js, JS_PATH)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
            return
        if self.path == "/api/config":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                content_length = 0
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
                cfg_path = os.path.join(BASE_DIR, "config.json")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
            return

        if self.path == "/api/sync":
            try:
                import sync_letus
                sync_letus.fetch_and_sync()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
            return

        if self.path == "/api/ignore":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                content_length = 0
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
                title = data.get("title", "").strip() if isinstance(data, dict) else ""
                if title:
                    cfg_path = os.path.join(BASE_DIR, "config.json")
                    cfg = {}
                    if os.path.exists(cfg_path):
                        with open(cfg_path, "r", encoding="utf-8") as f:
                            cfg = json.load(f)
                    ignored = cfg.get("ignored_titles", [])
                    if title not in ignored:
                        ignored.append(title)
                        cfg["ignored_titles"] = ignored
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)

                    # Also persist in ignored_titles.json
                    ign_path = os.path.join(BASE_DIR, "ignored_titles.json")
                    ign_list = []
                    if os.path.exists(ign_path):
                        try:
                            with open(ign_path, "r", encoding="utf-8") as f:
                                ign_list = json.load(f)
                        except Exception:
                            ign_list = []
                    if title not in ign_list:
                        ign_list.append(title)
                        with open(ign_path, "w", encoding="utf-8") as f:
                            json.dump(ign_list, f, ensure_ascii=False, indent=2)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
            return

        if self.path == "/api/scan-submissions":
            try:
                import scan_letus_status
                import threading
                # Run scan in thread so HTTP response is returned immediately
                threading.Thread(target=scan_letus_status.scan_letus_submissions, daemon=True).start()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "started"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
            return

        # Handle unknown POST cleanly with 404 (SimpleHTTPRequestHandler has no do_POST)
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "error", "message": "Not found"}).encode("utf-8"))
        return

if __name__ == "__main__":
    local_ip = get_local_ip()
    print("==================================================")
    print("  [SERVER] 課題・タスクレーダー サーバー起動中")
    print(f"  [PC]     http://localhost:{PORT}")
    print(f"  [MOBILE] http://{local_ip}:{PORT}/index.html")
    print("  (スマホと同じWi-Fiに接続して上記URLを開いてください)")
    print("==================================================")
    webbrowser.open(f"http://localhost:{PORT}/index.html")
    with socketserver.TCPServer(("", PORT), AssignmentHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nサーバーを停止しました。")
