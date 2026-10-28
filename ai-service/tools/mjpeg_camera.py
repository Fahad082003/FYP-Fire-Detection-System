"""Test camera: serves a webcam or a video file (looped) as an MJPEG stream, like an IP camera or a phone "IP Webcam" app.

Usage (in ai-service):  .\\.venv\\Scripts\\python tools\\mjpeg_camera.py 0             laptop webcam
                        .\\.venv\\Scripts\\python tools\\mjpeg_camera.py demo.mp4      video file, plays in a loop
Then on the Devices page add a camera with Stream link http://127.0.0.1:8090/video and format Mjpeg:
the dashboard shows the video and the AI service analyses the same live frames.
"""
import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2


class Camera(threading.Thread):
    """Reads frames at the source's own speed and keeps only the latest JPEG, so every viewer sees the same live frame."""

    def __init__(self, source: str):
        super().__init__(daemon=True)
        self.source = int(source) if source.isdigit() else source
        self.jpeg: bytes | None = None
        self.cond = threading.Condition()
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            cap = cv2.VideoCapture(self.source)
            if not cap.isOpened():
                print(f"cannot open {self.source!r}, retrying")
                time.sleep(2)
                continue
            fps = cap.get(cv2.CAP_PROP_FPS)
            delay = 1 / fps if 1 <= fps <= 60 else 1 / 15
            while not self.stopped.is_set():
                ok, frame = cap.read()
                if not ok:
                    break  # end of the file: reopen, so it loops
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                with self.cond:
                    self.jpeg = buf.tobytes()
                    self.cond.notify_all()
                if not isinstance(self.source, int):
                    time.sleep(delay)  # play a file in real time; a webcam already delivers at its own pace
            cap.release()


def make_handler(cam: Camera):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/video":
                self.send_error(404, "Use /video")
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = None
            try:
                while not cam.stopped.is_set():
                    with cam.cond:
                        cam.cond.wait_for(lambda: cam.jpeg is not None and cam.jpeg is not last, timeout=5)
                        jpg = last = cam.jpeg
                    if jpg is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # viewer closed the tab

        def log_message(self, *args):
            pass

    return Handler


def serve(source: str, host: str = "127.0.0.1", port: int = 8090) -> ThreadingHTTPServer:
    cam = Camera(source)
    cam.start()
    server = ThreadingHTTPServer((host, port), make_handler(cam))
    server.daemon_threads = True
    server.camera = cam  # camera.stopped.set() ends the video for every viewer
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="webcam number (0 = laptop camera) or a video file")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    server = serve(args.source, port=args.port)
    print(f"Test camera on http://127.0.0.1:{server.server_address[1]}/video  (Ctrl+C to stop)")
    server.serve_forever()
