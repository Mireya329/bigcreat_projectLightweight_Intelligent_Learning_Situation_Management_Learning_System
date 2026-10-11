"""Loopback gateway: POST /ai; GET /health. One inference at a time."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from captain_ai import Client, PROTOCOL_VERSION, SCHEMA_VERSION, strict_loads, envelope, protocol_info, SCHEMAS

def make_server(port=8765, client=None):
    client = client or Client()
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)
        def send_json(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def failure(self, status, reason, code=2):
            self.send_json(status,envelope(code,raw={'text':'','reason':reason,'retried':0,'http_status':status},model=client.model))
        def do_GET(self):
            if self.path == '/schema':
                return self.send_json(200,{'protocol':protocol_info(),'schemas':SCHEMAS})
            if self.path != '/health':
                return self.failure(404, 'unknown endpoint')
            self.send_json(200, {'status': 'gateway_running', 'model': client.model,
                'protocol_version': PROTOCOL_VERSION, 'schema_version': SCHEMA_VERSION,'protocol':protocol_info(),
                'note': 'Gateway health does not verify model readiness.'})
        def do_POST(self):
            if self.path != '/ai':
                return self.failure(404, 'unknown endpoint')
            if self.headers.get('Origin'):
                return self.failure(403, 'browser origins unsupported; call from backend')
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                return self.failure(415, 'application/json required')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    return self.failure(413, 'body must be 1..65536 bytes')
                request = strict_loads(self.rfile.read(size).decode('utf-8'))
            except (ValueError, UnicodeError, TimeoutError):
                return self.failure(400, 'invalid JSON body')
            if not lock.acquire(blocking=False):
                return self.failure(503, 'model_busy: retry later', code=1)
            try:
                self.send_json(200, client.call(request))
            finally:
                lock.release()
        def log_message(self, fmt, *args):
            pass  # No question/essay content in HTTP access logs.
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args()
    server = make_server(args.port)
    print('Captain AI: http://127.0.0.1:%s/ai (Ctrl+C to stop)' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
