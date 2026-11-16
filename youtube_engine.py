import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List

from media_engine import get_ffmpeg_exe, CREATE_NO_WINDOW


def _sanitize_filename(name: str) -> str:
    """Odstraní nepovolené znaky z názvu souboru pro Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned[:150] or "youtube_video"


def fetch_youtube_info(url: str) -> Dict[str, Any]:
    """
    Načte metadata o YouTube videu (nebo playlistu) bez stahování:
    - title: název videa
    - uploader: kanál / autor
    - duration_sec: délka v sekundách
    - duration_str: HH:MM:SS
    - view_count: počet zhlédnutí
    - resolution_list: dostupná rozlišení
    - webpage_url: kanonická URL
    """
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        dur = int(info.get("duration") or 0)
        h, m, s = dur // 3600, (dur % 3600) // 60, dur % 60
        dur_str = f"{h:02d}:{m:02d}:{s:02d}" if dur > 0 else "Neznámá délka"

        heights = sorted(
            {
                int(f.get("height"))
                for f in info.get("formats", [])
                if f.get("height") and f.get("vcodec") != "none"
            },
            reverse=True,
        )

        return {
            "title": info.get("title", "Neznámé video"),
            "uploader": info.get("uploader", "Neznámý kanál"),
            "duration_sec": dur,
            "duration_str": dur_str,
            "view_count": info.get("view_count") or 0,
            "heights": heights,
            "webpage_url": info.get("webpage_url", url),
        }


def download_youtube_media(
    url: str,
    output_dir: str,
    target_format: str = "mp4",
    max_resolution: str = "Nejvyšší dostupná (4K / 1440p / 1080p)",
    progress_callback: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """
    Stáhne YouTube video (nebo jen audio) do zvoleného formátu:
    - Video formáty: 'mp4', 'mov', 'mkv', 'webm'
    - Audio formáty: 'mp3', 'aac', 'm4a', 'wav', 'flac'
    Využívá přibalený FFmpeg z imageio-ffmpeg.
    Vrací absolutní cestu ke staženému souboru.
    """
    import yt_dlp

    os.makedirs(output_dir, exist_ok=True)
    ffmpeg_exe = get_ffmpeg_exe()

    fmt = target_format.split(" ")[0].lower().strip()
    is_audio_only = fmt in {"mp3", "aac", "m4a", "wav", "flac"}

    # Určení výškového limitu pro video
    height_limit = None
    if "2160p" in max_resolution or "4K" in max_resolution and "Nejvyšší" not in max_resolution:
        height_limit = 2160
    elif "1440p" in max_resolution:
        height_limit = 1440
    elif "1080p" in max_resolution:
        height_limit = 1080
    elif "720p" in max_resolution:
        height_limit = 720
    elif "480p" in max_resolution:
        height_limit = 480

    # Výběr streamu
    if is_audio_only:
        format_selector = "bestaudio/best"
    else:
        # Pro MP4 a MOV preferujeme H.264 (avc1) + AAC (m4a), aby soubor šel otevřít v každém editoru
        # (DaVinci Resolve, Premiere Pro, Windows Media Player) a remux do MOV/MP4 trval 0.5 sekundy.
        if fmt in {"mp4", "mov"}:
            if height_limit:
                format_selector = (
                    f"bestvideo[height<={height_limit}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                    f"bestvideo[height<={height_limit}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[height<={height_limit}]+bestaudio/best[height<={height_limit}]/best"
                )
            else:
                # Pokud uživatel chce absolutní maximum (4K/2K) nebo 1080p kompatibilní
                format_selector = (
                    "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                    "bestvideo+bestaudio/best"
                )
        else:
            if height_limit:
                format_selector = f"bestvideo[height<={height_limit}]+bestaudio/best[height<={height_limit}]/best"
            else:
                format_selector = "bestvideo+bestaudio/best"

    downloaded_files: List[str] = []

    def _hook(d: Dict[str, Any]):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Stahování bylo zrušeno uživatelem.")

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            speed_mb = f"{speed / (1024 * 1024):.1f} MB/s" if speed else ""
            if total > 0:
                frac = min(0.90, max(0.02, (downloaded / total) * 0.90))
                pct = int((downloaded / total) * 100)
                if progress_callback:
                    progress_callback(frac, f"Stahuji z YouTube: {pct} % ({speed_mb})")
            else:
                if progress_callback:
                    progress_callback(0.3, f"Stahuji z YouTube... ({speed_mb})")
        elif status == "finished":
            fn = d.get("filename")
            if fn:
                downloaded_files.append(fn)
            if progress_callback:
                progress_callback(0.92, "Stahování dokončeno, spojuji stopy a ukládám výstupní formát...")

    # Zjistit nejprve info a připravit čisté jméno souboru
    if progress_callback:
        progress_callback(0.02, "Načítám informace o YouTube videu...")

    info = fetch_youtube_info(url)
    clean_title = _sanitize_filename(info["title"])

    # Dočasný template v cílové složce
    outtmpl = os.path.join(output_dir, f"{clean_title}.%(ext)s")

    ydl_opts: Dict[str, Any] = {
        "format": format_selector,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "ffmpeg_location": ffmpeg_exe,
        "progress_hooks": [_hook],
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
    }

    if not is_audio_only:
        # Pro MOV nejprve spojíme do MP4 a poté uděláme čistý FFmpeg převod do MOV (nebo přímý merge do mp4/mkv/webm)
        merge_fmt = "mp4" if fmt in {"mp4", "mov"} else fmt
        ydl_opts["merge_output_format"] = merge_fmt

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Najít výsledný soubor po sloučení
    expected_merged = os.path.join(output_dir, f"{clean_title}.{('mp4' if fmt in {'mp4', 'mov'} else fmt)}")
    source_file = None
    if os.path.isfile(expected_merged):
        source_file = expected_merged
    else:
        # Hledat nejnovější soubor začínající clean_title
        for f in os.listdir(output_dir):
            if f.startswith(clean_title) and not f.endswith(".part") and not f.endswith(".ytdl"):
                candidate = os.path.join(output_dir, f)
                if os.path.isfile(candidate):
                    source_file = candidate
                    break

    if not source_file or not os.path.isfile(source_file):
        raise RuntimeError("Stažený soubor nebyl nalezen na disku.")

    final_path = os.path.join(output_dir, f"{clean_title}.{fmt}")

    # Pokud uživatel chtěl MOV nebo audio formát (MP3, AAC, WAV, FLAC, M4A), převedeme přes přibalený FFmpeg
    if is_audio_only:
        if progress_callback:
            progress_callback(0.95, f"Extrahuji audio do formátu .{fmt.upper()}...")
        from media_engine import build_audio_command
        temp_source = source_file
        if os.path.abspath(temp_source).lower() == os.path.abspath(final_path).lower():
            temp_source = os.path.join(output_dir, f"{clean_title}_raw_audio.tmp")
            shutil.move(source_file, temp_source)
        cmd = build_audio_command(temp_source, final_path, fmt, bitrate="192k")
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW, check=True)
        if os.path.isfile(temp_source) and os.path.abspath(temp_source).lower() != os.path.abspath(final_path).lower():
            try:
                os.remove(temp_source)
            except Exception:
                pass
    elif fmt == "mov" and os.path.abspath(source_file).lower() != os.path.abspath(final_path).lower():
        if progress_callback:
            progress_callback(0.95, "Ukládám do Apple QuickTime kontejneru (.MOV)...")
        # Zkusit nejprve rychlý Stream Copy do MOV (funguje pokud je video H.264/HEVC a audio AAC)
        cmd_copy = [
            ffmpeg_exe, "-y", "-hide_banner",
            "-i", source_file,
            "-c", "copy", "-movflags", "+faststart",
            final_path,
        ]
        res = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        if res.returncode != 0:
            # Pokud bylo 4K video ve VP9/AV1, překódujeme přes RTX 5060 Ti (h264_nvenc) do MOV
            if progress_callback:
                progress_callback(0.96, "Překódovávám VP9/AV1 stream do .MOV pomocí NVIDIA NVENC...")
            cmd_nvenc = [
                ffmpeg_exe, "-y", "-hide_banner",
                "-i", source_file,
                "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                final_path,
            ]
            subprocess.run(cmd_nvenc, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW, check=True)

        # Smazat mezisoubor .mp4
        if os.path.isfile(final_path) and os.path.isfile(source_file):
            try:
                os.remove(source_file)
            except Exception:
                pass
    else:
        final_path = source_file

    if progress_callback:
        progress_callback(1.0, f"✅ Staženo z YouTube: {os.path.basename(final_path)}")

    return final_path
