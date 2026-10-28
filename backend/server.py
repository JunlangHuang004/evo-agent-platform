"""Serve the existing UI and a small same-origin JSON API on one port."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
import argparse
import json
import mimetypes

from .config import Config
from .engine import Platform, ActionError

PUBLIC_FILES = {'index.html', 'styles.css', 'panels.css', 'js/main.js', 'js/view.js',
                'js/store.js', 'js/workbench.js', 'js/panels.js', 'js/data.js'}


def make_handler(platform):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'EvoMAC/0.2'

        def log_message(self, fmt, *args):
            # Request paths and bodies are deliberately not logged.
            pass

        def _headers(self, status, content_type, length):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()

        def _json(self, status, body):
            content = json.dumps(platform.config.scrub(body), ensure_ascii=False).encode('utf-8')
            self._headers(status, 'application/json; charset=utf-8', len(content))
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _valid_host(self):
            # Prevent DNS rebinding to the local development service.
            try:
                parsed = urlsplit('http://' + self.headers.get('Host', ''))
                return parsed.hostname in ('localhost', '127.0.0.1', '::1') and parsed.port == self.server.server_port
            except ValueError:
                return False

        def do_GET(self):
            if not self._valid_host():
                return self._json(403, {'error': '只接受本机访问。'})
            path = unquote(urlsplit(self.path).path)
            if path == '/api/health':
                return self._json(200, {'ok': True, **platform.snapshot()['connection']})
            if path == '/api/state':
                return self._json(200, platform.snapshot())
            if path.startswith('/api/runs/'):
                run_id = path[len('/api/runs/'):]
                run = platform.repo.get_run(run_id)
                return self._json(200, platform._public(run)) if run else self._json(404, {'error': '任务不存在。'})
            name = 'index.html' if path == '/' else path.lstrip('/')
            # Never serve .env, Python source, SQLite, agent files, or arbitrary paths.
            if name not in PUBLIC_FILES:
                return self._json(404, {'error': 'Not found'})
            file = platform.config.root / name
            if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(platform.config.root):
                return self._json(404, {'error': 'Not found'})
            data = file.read_bytes()
            kind = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            self._headers(200, kind + '; charset=utf-8', len(data))
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if not self._valid_host():
                return self._json(403, {'error': '只接受本机访问。'})
            origin = self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
                return self._json(403, {'error': '拒绝跨站请求。'})
            if self.headers.get('X-EvoMAC-Client') != '1':
                return self._json(403, {'error': '缺少客户端请求标记。'})
            if urlsplit(self.path).path != '/api/action':
                return self._json(404, {'error': 'Not found'})
            if self.headers.get_content_type() != 'application/json':
                return self._json(415, {'error': '请使用 application/json。'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > 200000:
                    return self._json(413, {'error': '请求体大小无效。'})
                payload = json.loads(self.rfile.read(size))
                return self._json(200, platform.action(payload))
            except (json.JSONDecodeError, UnicodeError, ValueError) as error:
                return self._json(getattr(error, 'status', 400), {'error': str(error)[:500]})
            except Exception:
                return self._json(500, {'error': '后端处理失败，请检查服务终端。'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Run the EvoMAC local backend + UI')
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    config = Config(root)
    platform = Platform(config)
    port = args.port or config.port
    # Local development service. Exposing this on a public interface requires authentication.
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(platform))
    print(f'EvoMAC: http://127.0.0.1:{port}/#workspace', flush=True)
    print(f'Provider: {config.provider} | Model: {config.model} | API configured: {bool(config.api_key)}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        platform.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
