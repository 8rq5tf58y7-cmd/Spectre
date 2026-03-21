#!/usr/bin/env python3
"""Simple HTTP server for RTX Converter web app. Run from rtx_web folder."""
import http.server
import socketserver
import webbrowser

PORT = 8080

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    url = f"http://localhost:{PORT}"
    print(f"Serving at {url}")
    print("On iPad (same WiFi): http://<this-computer-ip>:{PORT}")
    print("Press Ctrl+C to stop")
    webbrowser.open(url)
    httpd.serve_forever()
