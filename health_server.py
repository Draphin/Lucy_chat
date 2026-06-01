import os
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self):
        """Responds to Render's automated pings to keep the free service alive."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Lucy Live Service Status: Operational")

    def log_message(self, format, *args):
        """Suppresses messy HTTP connection log spam in your console."""
        return

def start_health_server():
    """Listens on the required port so Render doesn't force a shutdown."""
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckServer)
    print(f"[Render Engine]: Web port binding established on port {port}", flush=True)
    server.serve_forever()
