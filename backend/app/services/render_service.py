import hashlib
import json
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..schemas import Cue, EditPlan, Visual
from .ffmpeg_service import detect_hw_encoder, final_codec_args, run_ffmpeg, segment_codec_args

RES = {("16:9", "preview"): (1280, 720), ("16:9", "final"): (1920, 1080),
       ("9:16", "preview"): (720, 1280), ("9:16", "final"): (1080, 1920)}
BITRATE = {"preview": 3000, "final": 8000}      # kbps, cùng bảng cho 16:9 và 9:16

PRESETS = {
    "cinematic_teal_orange": "colorbalance=rs=-0.12:gs=-0.02:bs=0.15:rh=0.15:gh=0.02:bh=-0.12",
    "warm_film": "colorbalance=rs=0.08:gs=0.03:bs=-0.08,eq=saturation=1.05:contrast=1.04",
    "cold_thriller": "colorbalance=rs=-0.10:bs=0.12:rh=-0.04:bh=0.08,eq=saturation=0.85:contrast=1.1",
    "vibrant_vlog": "eq=saturation=1.35:contrast=1.08",
    "noir": "hue=s=0,eq=contrast=1.2",
    "vintage_sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,eq=contrast=1.05:saturation=0.9",
}
_LOGO_XY = {"top-left": ("{m}", "{m}"), "top-right": ("W-w-{m}", "{m}"),
            "bottom-left": ("{m}", "H-h-{m}"), "bottom-right": ("W-w-{m}", "H-h-{m}")}


class RenderOptions(BaseModel):
    aspect_ratio: Literal["16:9", "9:16"] = "16:9"
    quality: Literal["preview", "final"] = "preview"
    fps: Literal[24, 30, 60] = 30
    burn_subtitles: bool = True
    subtitle_font: str = "Arial"
    subtitle_size: int = Field(54, ge=10, le=200)       # px trên khung 1080 cạnh ngắn
    subtitle_color: str = "#FFFFFF"
    subtitle_outline_color: str = "#000000"
    subtitle_margin_v: int | None = None
    logo_position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    logo_width_pct: float = Field(0.12, ge=0.03, le=0.4)
    logo_opacity: float = Field(0.9, ge=0.1, le=1.0)
    logo_margin: int = Field(40, ge=0, le=300)
    music_volume: float = Field(0.18, ge=0.0, le=1.0)
    music_ducking: bool = True
    vignette: bool = False
    film_grain: bool = False
    hw_accel: Literal["auto", "off"] = "auto"     # GPU: tự dò, tự quay về CPU nếu lỗi
    parallel_segments: int = Field(0, ge=0, le=8)   # 0 = tự chọn theo số lõi CPU


log = logging.getLogger("mre.render")
CACHE_VERSION = "v2"     # tăng khi đổi cách dựng đoạn (PRESETS, segment_filter...) để vô hiệu cache cũ


def _file_sig(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{st.st_size}-{int(st.st_mtime)}"
    except OSError:
        return "missing"


def _segment_key(src: str, v: Visual, W: int, H: int, fps: int, frames: int, preset: str, crf: str,
                 lut_sig: str | None) -> str:
    raw = json.dumps([CACHE_VERSION, src, _file_sig(src), v.model_dump(), W, H, fps, frames, preset, crf, lut_sig],
                     sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def prune_cache(cache_dir: Path, max_bytes: int = 8 * 1024 ** 3, pattern: str = "*.mp4"):
    """Xoá đoạn cache cũ nhất (LRU theo mtime) khi vượt dung lượng."""
    files = sorted(Path(cache_dir).glob(pattern), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= max_bytes:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)


@dataclass
class Segment:
    visual: Visual
    frames: int


def plan_segments(plan: EditPlan, voice_duration: float, fps: int) -> tuple[list[Segment], int]:
    """Chia timeline theo FRAME để tổng không bao giờ trôi lệch so với voice.
    Mỗi section kéo dài tới voice_start của section kế (khoảng lặng giữa cue được hấp thụ);
    section đầu bắt đầu từ 0, section cuối kéo tới hết voice."""
    secs = plan.sections
    segs: list[Segment] = []
    end_frame = 0
    for k, sec in enumerate(secs):
        t_start = 0.0 if k == 0 else sec.voice_start
        t_end = secs[k + 1].voice_start if k + 1 < len(secs) else max(sec.voice_end, voice_duration)
        weights = [v.timeline_duration for v in sec.visuals]
        total_w = sum(weights)
        cum = 0.0
        for v, w in zip(sec.visuals, weights):
            f0 = round((t_start + (t_end - t_start) * cum / total_w) * fps)
            cum += w
            f1 = round((t_start + (t_end - t_start) * cum / total_w) * fps)
            if f1 > f0:
                segs.append(Segment(v, f1 - f0))
            end_frame = f1
    return segs, end_frame


def _transition(kind: str, side: str, dur: float) -> list[str]:
    if kind == "flash_cut":
        return [f"fade=t=in:st=0:d=0.12:color=white"] if side == "in" else \
               [f"fade=t=out:st={max(dur - 0.12, 0):.3f}:d=0.12:color=white"]
    if kind in ("fade", "cross_dissolve"):
        return [f"fade=t=in:st=0:d=0.3"] if side == "in" else [f"fade=t=out:st={max(dur - 0.3, 0):.3f}:d=0.3"]
    if kind == "whip_pan":
        win = "between(t,0,0.15)" if side == "in" else f"between(t,{max(dur - 0.15, 0):.3f},{dur:.3f})"
        return [f"gblur=sigma=14:steps=1:enable='{win}'"]
    return []   # none / shake: chưa hỗ trợ shake


def segment_filter(v: Visual, W: int, H: int, fps: int, frames: int, lut_file: str | None = None) -> str:
    dur = frames / fps
    f: list[str] = []
    if abs(v.speed - 1) > 1e-3:
        f.append(f"setpts=PTS/{v.speed:.4f}")
    f.append(f"fps={fps}")
    zoom = abs(v.zoom_end - v.zoom_start) > 1e-3 or v.zoom_start > 1.001
    k = 2 if zoom else 1                      # zoom: dựng ở 2x rồi zoompan để tránh rung do làm tròn pixel
    f.append(f"scale={k * W}:{k * H}:force_original_aspect_ratio=increase,crop={k * W}:{k * H}")
    f.append(f"tpad=stop_mode=clone:stop_duration={dur:.3f}")   # đóng băng frame cuối nếu clip ngắn hơn voice
    if zoom:
        z = f"{v.zoom_start:.4f}+({v.zoom_end:.4f}-{v.zoom_start:.4f})*min(on/{max(frames - 1, 1)},1)"
        f.append(f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}")
    if (v.brightness, v.contrast, v.saturation) != (1.0, 1.0, 1.0):
        f.append(f"eq=brightness={(v.brightness - 1) * 0.4:.3f}:contrast={v.contrast:.3f}:saturation={v.saturation:.3f}")
    if v.filter:
        if lut_file:
            f.append(f"lut3d={lut_file}")
        elif v.filter in PRESETS:
            f.append(PRESETS[v.filter])
    f += _transition(v.transition_in, "in", dur) + _transition(v.transition_out, "out", dur)
    f.append("format=yuv420p,setsar=1")
    return ",".join(f)


def _ass_color(hex_color: str) -> str:
    h = hex_color.lstrip("#").ljust(6, "0")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_text(text: str) -> str:
    t = (text or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return "\\N".join(x.strip() for x in t.replace("\r", "").split("\n") if x.strip())


def subtitle_ass(o: RenderOptions, W: int, H: int, cues) -> str:
    """File .ass với khung ảo = đúng kích thước video (PlayRes W×H) -> cỡ chữ tính bằng pixel thật, khớp bản xem nhanh.
    (Dùng thẳng SRT thì libass lấy khung 384×288 mặc định -> chữ bị phóng to ~3,75 lần, nằm giữa màn hình.)"""
    s = min(W, H) / 1080
    margin = o.subtitle_margin_v if o.subtitle_margin_v is not None else round((260 if H > W else 70) * s)
    font = re.sub(r"[^\w \-]", "", o.subtitle_font) or "Arial"
    side = round(60 * s)
    head = ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            f"PlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Default,{font},{round(o.subtitle_size * s)},{_ass_color(o.subtitle_color)},&H000000FF,"
            f"{_ass_color(o.subtitle_outline_color)},&H64000000,-1,0,0,0,100,100,0,0,1,{max(1, round(3 * s))},0,2,"
            f"{side},{side},{margin},1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = "".join(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Default,,0,0,0,,{_ass_text(c.text)}\n"
                 for c in cues if c.end > c.start and (c.text or "").strip())
    return head + ev


def subtitle_filter(o: RenderOptions, W: int, H: int) -> str:
    return "subtitles=subs.ass"


def render_video(plan: EditPlan, cues: list[Cue], footage_paths: dict[str, str], voice_path: str,
                 voice_duration: float, out_path: Path, opts: RenderOptions, workdir: Path,
                 music_path: str | None = None, logo_path: str | None = None,
                 luts: dict[str, str] | None = None, progress_cb=None,
                 cache_dir: Path | None = None, cancel=None):
    cb = progress_cb or (lambda p: None)
    W, H = RES[(opts.aspect_ratio, opts.quality)]
    fps = opts.fps
    preview = opts.quality == "preview"
    workdir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lut_files = {}
    for i, (name, p) in enumerate((luts or {}).items()):
        shutil.copy(p, workdir / f"lut_{i}.cube")
        lut_files[name] = f"lut_{i}.cube"

    segs, total_frames = plan_segments(plan, voice_duration, fps)
    total = total_frames / fps

    # 1) Dựng từng đoạn (song song + cache theo nội dung: sửa 1 cue chỉ dựng lại đúng đoạn đó)
    seg_dir = Path(cache_dir) if cache_dir else workdir
    seg_dir.mkdir(parents=True, exist_ok=True)
    preset, crf = "veryfast", ("23" if preview else "16")   # đoạn trung gian: nhẹ, bản cuối mã hoá lại ở bước 2
    state = {"enc": detect_hw_encoder() if opts.hw_accel == "auto" else None}
    lut_sig = {n: _file_sig(p) for n, p in (luts or {}).items()}

    def encode(i: int, seg: Segment) -> Path:
        v = seg.visual
        src = footage_paths.get(v.footage)
        if not src:
            raise RuntimeError(f"Không tìm thấy file footage '{v.footage}'")
        final = seg_dir / f"{_segment_key(src, v, W, H, fps, seg.frames, preset, crf, lut_sig.get(v.filter or ''))}.mp4"
        if final.exists():
            os.utime(final, None)           # cache hit: đánh dấu mới dùng (LRU)
            return final
        tmp = workdir / f"seg_{i:05d}.mp4"
        vf = segment_filter(v, W, H, fps, seg.frames, lut_files.get(v.filter or ""))

        def run_seg(enc):
            run_ffmpeg([*(["-hwaccel", "auto"] if enc else []), "-ss", f"{v.start:.3f}", "-t", f"{v.source_duration:.3f}",
                        "-i", src, "-an", "-vf", vf, "-frames:v", seg.frames, *segment_codec_args(enc, int(crf)),
                        "-pix_fmt", "yuv420p", tmp.name], cwd=workdir, cancel=cancel)

        enc = state["enc"]
        try:
            run_seg(enc)
        except RuntimeError as ex:      # RenderCancelled không phải RuntimeError nên không bị nuốt ở đây
            if not enc:
                raise
            log.warning("Tăng tốc phần cứng lỗi, chuyển sang CPU: %s", str(ex)[:200])
            state["enc"] = None
            run_seg(None)
        try:
            os.replace(tmp, final)          # nguyên tử: không bao giờ để lại file cache dở dang
        except OSError:
            shutil.move(str(tmp), str(final))
        return final

    from . import sysinfo
    # Số đoạn dựng song song theo cấu hình máy + chế độ hiệu năng (GPU mã hoá thì chạy được nhiều đoạn hơn)
    workers = opts.parallel_segments or sysinfo.parallel_segments(state["enc"])
    paths: list[Path | None] = [None] * len(segs)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(encode, i, sg): i for i, sg in enumerate(segs)}
        done = 0
        try:
            for fut in as_completed(futs):      # progress_cb chỉ gọi ở luồng chính (DB session không thread-safe)
                paths[futs[fut]] = fut.result()
                done += 1
                cb(int(70 * done / len(segs)))
        except BaseException:
            for f in futs:
                f.cancel()
            raise

    (workdir / "concat.txt").write_text(
        "".join("file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n" for p in paths), "utf-8")

    # 2) Ghép + voice + nhạc (ducking) + phụ đề + logo
    inputs = ["-f", "concat", "-safe", "0", "-i", "concat.txt", "-i", voice_path]
    nxt = 2
    music_i = logo_i = None
    if music_path:
        inputs += ["-stream_loop", "-1", "-i", music_path]
        music_i, nxt = nxt, nxt + 1
    if logo_path:
        inputs += ["-i", logo_path]
        logo_i = nxt

    vf = []
    if opts.vignette:
        vf.append("vignette=PI/5")
    if opts.film_grain:
        vf.append("noise=alls=6:allf=t")
    if opts.burn_subtitles and cues:
        (workdir / "subs.ass").write_text(subtitle_ass(opts, W, H, cues), "utf-8")
        vf.append(subtitle_filter(opts, W, H))      # burn trước, logo overlay sau => logo không bị phụ đề che
    fc = [f"[0:v]{','.join(vf) or 'null'}[v0]"]
    if logo_i is not None:
        x, y = (e.format(m=opts.logo_margin) for e in _LOGO_XY[opts.logo_position])
        fc.append(f"[{logo_i}:v]scale={int(W * opts.logo_width_pct)}:-1,format=rgba,"
                  f"colorchannelmixer=aa={opts.logo_opacity}[lg]")
        fc.append(f"[v0][lg]overlay={x}:{y}[vout]")
    else:
        fc.append("[v0]null[vout]")

    A = "aformat=sample_rates=48000:channel_layouts=stereo"
    if music_i is None:
        fc.append(f"[1:a]{A},apad[aout]")
    else:
        mus = f"[{music_i}:a]{A},volume={opts.music_volume}[mus]"
        if opts.music_ducking:
            fc += [f"[1:a]{A},apad,asplit=2[vo][sc]", mus,
                   "[mus][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400:makeup=1[duck]",
                   "[vo][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]"]
        else:
            fc += [f"[1:a]{A},apad[vo]", mus, "[vo][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]"]
        fc.append(f"[mix]afade=t=out:st={max(total - 3, 0):.2f}:d=3[aout]")

    br = BITRATE[opts.quality]

    def run_final(enc):
        run_ffmpeg([*inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
                    *final_codec_args(enc, preview, br), "-pix_fmt", "yuv420p",
                    "-r", fps, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    "-t", f"{total:.3f}", str(out_path)],
                   cwd=workdir, duration=total, progress_cb=lambda fr: cb(70 + int(30 * fr)), cancel=cancel)

    try:
        run_final(state["enc"])
    except RuntimeError as ex:
        if not state["enc"]:
            raise
        log.warning("Mã hoá phần cứng bước cuối lỗi, chuyển sang CPU: %s", str(ex)[:200])
        run_final(None)
    cb(100)
