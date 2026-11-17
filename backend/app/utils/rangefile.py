import os

from fastapi import Request, Response
from fastapi.responses import StreamingResponse


def ranged_file(request: Request, path: str, media_type: str):
    """Trả file có hỗ trợ HTTP Range (để <video>/<audio> tua được mà không tải hết file)."""
    size = os.path.getsize(path)
    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes="):
        s, _, e = rng[6:].split(",")[0].strip().partition("-")
        try:
            if s == "":
                start = max(size - int(e), 0)
            else:
                start = int(s)
                end = min(int(e), size - 1) if e else size - 1
            if start > end or start >= size:
                raise ValueError
            status = 206
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1

    def chunks():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                data = f.read(min(1024 * 1024, left))
                if not data:
                    break
                left -= len(data)
                yield data

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(chunks(), status_code=status, media_type=media_type, headers=headers)
