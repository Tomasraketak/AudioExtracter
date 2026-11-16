import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Dict, Any
import imageio_ffmpeg

# Підтримка приховування консолі на Windows
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def get_ffmpeg_exe() -> str:
    """Vrátí cestu k FFmpeg binárce (systémové nebo přibalené v imageio_ffmpeg)."""
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def probe_media_info(file_path: str) -> Dict[str, Any]:
    """
    Zjistí základní informace o souboru pomocí `ffmpeg -i`:
    - duration_sec: délka v sekundách (float)
    - duration_str: formátovaná délka HH:MM:SS
    - has_video: bool
    - has_audio: bool
    - video_info: popis video streamu
    - audio_info: popis audio streamu
    - size_mb: velikost v MB
    """
    ffmpeg = get_ffmpeg_exe()
    info = {
        "duration_sec": 0.0,
        "duration_str": "00:00:00",
        "has_video": False,
        "has_audio": False,
        "video_info": "Žádné video",
        "audio_info": "Žádné audio",
        "size_mb": 0.0,
        "width": 1080,
        "height": 1920,
    }
    try:
        if os.path.exists(file_path):
            info["size_mb"] = round(os.path.getsize(file_path) / (1024 * 1024), 2)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        )
        stderr = proc.stderr

        # Duration: 00:03:45.12
        dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
        if dur_match:
            h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
            total_sec = h * 3600 + m * 60 + s
            info["duration_sec"] = total_sec
            info["duration_str"] = f"{h:02d}:{m:02d}:{int(s):02d}"

        # Video stream
        vid_match = re.search(r"Stream #\d+:\d+.*?: Video:\s*([^\n\r]+)", stderr)
        if vid_match:
            # Ignorovat cover art (mjpeg / png attached pic) pokud jde o audio soubor
            v_desc = vid_match.group(1).strip()
            ext = Path(file_path).suffix.lower()
            if ext not in {".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".wma"}:
                info["has_video"] = True
                # Zkrátit výpis
                parts = [p.strip() for p in v_desc.split(",")]
                info["video_info"] = ", ".join(parts[:3])
                res_m = re.search(r"\b(\d{2,5})x(\d{2,5})\b", v_desc)
                if res_m:
                    info["width"] = int(res_m.group(1))
                    info["height"] = int(res_m.group(2))

        # Audio stream
        aud_match = re.search(r"Stream #\d+:\d+.*?: Audio:\s*([^\n\r]+)", stderr)
        if aud_match:
            info["has_audio"] = True
            a_desc = aud_match.group(1).strip()
            parts = [p.strip() for p in a_desc.split(",")]
            info["audio_info"] = ", ".join(parts[:4])

    except Exception as e:
        info["error"] = str(e)

    return info


def check_nvenc_available() -> bool:
    """Ověří, zda FFmpeg a GPU podporují h264_nvenc."""
    ffmpeg = get_ffmpeg_exe()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=5,
        )
        return "h264_nvenc" in proc.stdout
    except Exception:
        return False


def build_audio_command(
    input_path: str,
    output_path: str,
    target_format: str,
    bitrate: str = "192k",
    sample_rate: str = "Původní",
    channels: str = "Původní",
    normalize: bool = False,
) -> list[str]:
    """Sestaví FFmpeg příkaz pro extrakci audia nebo konverzi mezi audio formáty."""
    ffmpeg = get_ffmpeg_exe()
    cmd = [ffmpeg, "-y", "-hide_banner", "-i", input_path, "-vn"]

    fmt = target_format.lower().strip()
    # Mapování kodeků
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
    elif fmt == "aac":
        cmd += ["-c:a", "aac", "-b:a", bitrate]
    elif fmt == "m4a":
        cmd += ["-c:a", "aac", "-b:a", bitrate]
    elif fmt == "wav":
        cmd += ["-c:a", "pcm_s16le"]
    elif fmt == "flac":
        cmd += ["-c:a", "flac"]
    elif fmt == "ogg":
        cmd += ["-c:a", "libvorbis", "-b:a", bitrate]
    elif fmt == "opus":
        cmd += ["-c:a", "libopus", "-b:a", bitrate]
    else:
        cmd += ["-b:a", bitrate]

    # Vzorkovací frekvence
    if sample_rate and sample_rate != "Původní":
        sr_clean = sample_rate.replace(" Hz", "").strip()
        if sr_clean.isdigit():
            cmd += ["-ar", sr_clean]

    # Kanály
    if channels == "Stereo (2.0)":
        cmd += ["-ac", "2"]
    elif channels == "Mono (1.0)":
        cmd += ["-ac", "1"]

    # Normalizace hlasitosti
    if normalize:
        cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]

    cmd.append(output_path)
    return cmd


def build_video_command(
    input_path: str,
    output_path: str,
    target_format: str,
    mode: str = "Stream Copy (Bleskový převod bez rekomprese)",
    resolution: str = "Původní",
    quality_preset: str = "Vysoká kvalita (CRF 18 / CQ 19)",
    audio_bitrate: str = "192k",
) -> list[str]:
    """
    Sestaví FFmpeg příkaz pro konverzi videa (např. MP4 <-> MOV <-> MKV <-> WEBM <-> AVI).
    Podporuje:
    - Stream Copy (-c copy) pro okamžitou změnu kontejneru (pokud je kompatibilní a nemění se rozlišení)
    - NVIDIA NVENC (h264_nvenc / hevc_nvenc) pro RTX 5060 Ti
    - CPU (libx264)
    """
    ffmpeg = get_ffmpeg_exe()
    cmd = [ffmpeg, "-y", "-hide_banner", "-i", input_path]

    fmt = target_format.lower().strip()

    # Určení cílového rozlišení
    scale_filter = None
    if resolution == "1080p (Full HD)":
        scale_filter = "scale=-2:1080"
    elif resolution == "720p (HD)":
        scale_filter = "scale=-2:720"
    elif resolution == "480p (SD)":
        scale_filter = "scale=-2:480"

    # Určení CQ / CRF hodnoty
    cq_val = "21"
    if "Bezztrátová / Maximální" in quality_preset:
        cq_val = "15"
    elif "Vysoká kvalita" in quality_preset:
        cq_val = "19"
    elif "Vyvážená" in quality_preset:
        cq_val = "23"
    elif "Menší soubor" in quality_preset:
        cq_val = "28"

    # Stream copy je možné jen pokud uživatel nechce měnit rozlišení a nejde o WEBM (který vyžaduje VP9/AV1)
    if "Stream Copy" in mode and scale_filter is None and fmt not in {"webm"}:
        cmd += ["-c", "copy"]
        if fmt in {"mp4", "mov"}:
            cmd += ["-movflags", "+faststart"]
        cmd.append(output_path)
        return cmd

    # Re-encoding
    if fmt == "webm":
        cmd += ["-c:v", "libvpx-vp9", "-crf", cq_val, "-b:v", "0"]
        if scale_filter:
            cmd += ["-vf", scale_filter]
        cmd += ["-c:a", "libopus", "-b:a", audio_bitrate]
    elif "NVENC (HEVC" in mode:
        cmd += ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", cq_val, "-b:v", "0"]
        if scale_filter:
            cmd += ["-vf", scale_filter]
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
    elif "NVENC" in mode:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", cq_val, "-b:v", "0"]
        if scale_filter:
            cmd += ["-vf", scale_filter]
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
    else:
        # CPU libx264
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", cq_val]
        if scale_filter:
            cmd += ["-vf", scale_filter]
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]

    if fmt in {"mp4", "mov"}:
        cmd += ["-movflags", "+faststart"]

    cmd.append(output_path)
    return cmd


def run_ffmpeg_with_progress(
    cmd: list[str],
    total_duration_sec: float,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> tuple[bool, str]:
    """
    Spustí FFmpeg proces, čte stderr v reálném čase a volá `progress_callback(fraction, status_text)`.
    Vrací (success, error_or_output_message).
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )

        stderr_lines = []
        time_pattern = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
        speed_pattern = re.compile(r"speed=\s*([^\s]+)")

        while True:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                return False, "Operace byla zrušena uživatelem."

            line = proc.stderr.readline()
            if not line and proc.poll() is not None:
                break

            if line:
                stderr_lines.append(line)
                t_match = time_pattern.search(line)
                if t_match and total_duration_sec > 0 and progress_callback:
                    h, m, s = int(t_match.group(1)), int(t_match.group(2)), float(t_match.group(3))
                    current_sec = h * 3600 + m * 60 + s
                    frac = min(0.99, max(0.0, current_sec / total_duration_sec))
                    sp_match = speed_pattern.search(line)
                    speed_str = f" ({sp_match.group(1)})" if sp_match else ""
                    progress_callback(frac, f"Zpracováno {int(frac * 100)} %{speed_str}")

        ret_code = proc.wait()
        if ret_code == 0:
            if progress_callback:
                progress_callback(1.0, "Hotovo (100 %)")
            return True, "Úspěšně dokončeno."
        else:
            err_tail = "".join(stderr_lines[-15:])
            return False, f"FFmpeg skončil s chybou (kód {ret_code}):\n{err_tail}"

    except Exception as e:
        return False, f"Chyba při spouštění FFmpeg: {str(e)}"
