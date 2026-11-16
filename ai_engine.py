import os
import sys
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any
import requests

from media_engine import get_ffmpeg_exe, CREATE_NO_WINDOW


def _register_nvidia_dll_paths():
    """
    Zajistí, aby CTranslate2 / Faster-Whisper na Windows automaticky našel
    CUDA 12 knihovny (cublas64_12.dll, cudnn64_9.dll) nainstalované přes pip ve venvu.
    """
    if os.name != "nt":
        return
    try:
        import site
        packages_dirs = site.getsitepackages()
        for pkg_dir in packages_dirs:
            nvidia_dir = os.path.join(pkg_dir, "nvidia")
            if os.path.isdir(nvidia_dir):
                for root, dirs, _ in os.walk(nvidia_dir):
                    if "bin" in dirs:
                        bin_path = os.path.join(root, "bin")
                        os.environ["PATH"] = bin_path + os.pathsep + os.environ.get("PATH", "")
                        if hasattr(os, "add_dll_directory"):
                            try:
                                os.add_dll_directory(bin_path)
                            except Exception:
                                pass
    except Exception:
        pass


_register_nvidia_dll_paths()


def format_timestamp_srt(seconds: float) -> str:
    """Převede sekundy na SRT časový formát HH:MM:SS,mmm."""
    ms = int(round((seconds - int(seconds)) * 1000))
    total_sec = int(seconds)
    s = total_sec % 60
    m = (total_sec // 60) % 60
    h = total_sec // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_timestamp_vtt(seconds: float) -> str:
    """Převede sekundy na WebVTT časový formát HH:MM:SS.mmm."""
    ms = int(round((seconds - int(seconds)) * 1000))
    total_sec = int(seconds)
    s = total_sec % 60
    m = (total_sec // 60) % 60
    h = total_sec // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def format_timestamp_short(seconds: float) -> str:
    """Převede sekundy na čitelný čas [MM:SS] nebo [HH:MM:SS]."""
    total_sec = int(seconds)
    s = total_sec % 60
    m = (total_sec // 60) % 60
    h = total_sec // 3600
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


class TranscriptionResult:
    def __init__(self, segments: List[Dict[str, Any]], detected_language: str, duration: float):
        self.segments = segments  # [{"start": float, "end": float, "text": str}]
        self.detected_language = detected_language
        self.duration = duration

    def to_plain_text(self, include_timestamps: bool = False) -> str:
        lines = []
        for seg in self.segments:
            txt = seg["text"].strip()
            if not txt:
                continue
            if include_timestamps:
                ts = format_timestamp_short(seg["start"])
                lines.append(f"{ts} {txt}")
            else:
                lines.append(txt)
        return "\n".join(lines) if include_timestamps else " ".join(lines)

    def to_srt(self) -> str:
        blocks = []
        for idx, seg in enumerate(self.segments, start=1):
            start_ts = format_timestamp_srt(seg["start"])
            end_ts = format_timestamp_srt(seg["end"])
            txt = seg["text"].strip()
            blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{txt}")
        return "\n\n".join(blocks)

    def to_vtt(self) -> str:
        blocks = ["WEBVTT\n"]
        for seg in self.segments:
            start_ts = format_timestamp_vtt(seg["start"])
            end_ts = format_timestamp_vtt(seg["end"])
            txt = seg["text"].strip()
            blocks.append(f"{start_ts} --> {end_ts}\n{txt}")
        return "\n\n".join(blocks)


class WhisperTranscriber:
    """Správa lokálního Faster-Whisper modelu s podporou RTX 5060 Ti (CUDA float16) a fallbackem na CPU."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._loaded_model = None
        self._loaded_model_key = None

    def _extract_temp_wav(self, media_path: str) -> str:
        """Extrahuje 16kHz mono WAV pro maximální rychlost a spolehlivost přepisu z jakéhokoli videa/audia."""
        ffmpeg = get_ffmpeg_exe()
        tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="whisper_audio_")
        os.close(tmp_fd)
        cmd = [
            ffmpeg, "-y", "-hide_banner",
            "-i", media_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            tmp_wav
        ]
        subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            check=True,
        )
        return tmp_wav

    def transcribe(
        self,
        media_path: str,
        model_size: str = "large-v3-turbo",
        language: Optional[str] = None,
        device_preference: str = "cuda",
        progress_callback: Optional[Callable[[float, str], None]] = None,
        segment_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> TranscriptionResult:
        from faster_whisper import WhisperModel

        if progress_callback:
            progress_callback(0.02, "Připravuji zvukovou stopu (16 kHz WAV)...")

        tmp_wav = None
        try:
            tmp_wav = self._extract_temp_wav(media_path)

            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Přepis byl zrušen.")

            # Mapování čitelných názvů modelů na HuggingFace / CTranslate2 identifikátory
            model_map = {
                "large-v3-turbo (Doporučeno pro RTX 5060 Ti - extrémně rychlý a přesný)": "deepdml/faster-whisper-large-v3-turbo-ct2",
                "large-v3 (Maximální přesnost)": "large-v3",
                "medium (Střední velikost)": "medium",
                "small (Rychlý, menší nároky)": "small",
                "base (Základní rychlý)": "base",
            }
            actual_model_id = model_map.get(model_size, model_size)

            # Zvolíme device & compute_type
            use_cuda = "cuda" in device_preference.lower() or "gpu" in device_preference.lower()
            device = "cuda" if use_cuda else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"

            model_key = f"{actual_model_id}:{device}:{compute_type}"
            if self._loaded_model is None or self._loaded_model_key != model_key:
                if progress_callback:
                    progress_callback(
                        0.05,
                        f"Načítám AI model '{model_size.split(' ')[0]}' na {device.upper()} ({compute_type})... (Při 1. spuštění se model stáhne)"
                    )
                try:
                    self._loaded_model = WhisperModel(
                        actual_model_id,
                        device=device,
                        compute_type=compute_type,
                        download_root=self.cache_dir,
                    )
                    self._loaded_model_key = model_key
                except Exception as cuda_err:
                    if device == "cuda":
                        if progress_callback:
                            progress_callback(0.06, f"CUDA inicializace selhala ({cuda_err}), přepínám na CPU (int8) na Ryzenu 9600...")
                        device = "cpu"
                        compute_type = "int8"
                        model_key = f"{actual_model_id}:cpu:int8"
                        self._loaded_model = WhisperModel(
                            actual_model_id,
                            device="cpu",
                            compute_type="int8",
                            download_root=self.cache_dir,
                            cpu_threads=8,
                        )
                        self._loaded_model_key = model_key
                    else:
                        raise

            if progress_callback:
                progress_callback(0.10, "Spouštím AI přepis audia...")

            lang_code = None
            if language and not language.startswith("Auto"):
                lang_code = language.split(" ")[0].strip().lower()

            segments_gen, info = self._loaded_model.transcribe(
                tmp_wav,
                language=lang_code,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )

            total_dur = info.duration if info.duration and info.duration > 0 else 1.0
            collected_segments = []

            for seg in segments_gen:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Přepis byl zrušen uživatelem.")

                seg_dict = {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text.strip(),
                }
                collected_segments.append(seg_dict)

                if segment_callback:
                    segment_callback(seg_dict)

                if progress_callback:
                    frac = 0.10 + 0.88 * min(1.0, seg.end / total_dur)
                    progress_callback(
                        frac,
                        f"Přepisuji ({info.language.upper()}): {format_timestamp_short(seg.end)} / {format_timestamp_short(total_dur)}"
                    )

            if progress_callback:
                progress_callback(1.0, f"Přepis dokončen! Detekovaný jazyk: {info.language.upper()}")

            return TranscriptionResult(
                segments=collected_segments,
                detected_language=info.language,
                duration=total_dur,
            )

        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except Exception:
                    pass


class BionicStudioClient:
    """
    Klient pro propojení s LM Studio / Bionic Studio lokálním serverem (OpenAI-kompatibilní API).
    Výchozí adresa: http://localhost:1234/v1
    """

    def __init__(self, base_url: str = "http://localhost:1234/v1", api_key: str = "lm-studio"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def check_connection(self) -> tuple[bool, str, List[str]]:
        """
        Otestuje spojení s Bionic / LM Studio serverem a vrátí:
        (is_connected, status_message, list_of_model_ids)
        """
        url = f"{self.base_url}/models"
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=4,
            )
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("id", "unknown") for m in data.get("data", [])]
                if models:
                    return True, f"Připojeno ({len(models)} modelů aktivních)", models
                return True, "Server běží, ale není načten žádný model v paměti", []
            return False, f"Server vrátil HTTP {resp.status_code}", []
        except requests.exceptions.ConnectionError:
            return False, "Server neběží (spusťte Bionic / LM Studio Server na portu 1234)", []
        except Exception as e:
            return False, f"Chyba připojení: {str(e)}", []

    def process_transcript(
        self,
        transcript_text: str,
        task_prompt: str,
        model_id: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Odešle přepsaný text do Bionic / LM Studia ke zpracování (shrnutí, korektura, akční body, překlad...).
        """
        if not transcript_text.strip():
            return False, "Přepis je prázdný – nejprve prosím spusťte AI přepis audia."

        ok, msg, models = self.check_connection()
        if not ok:
            return False, f"Nelze se spojit s Bionic / LM Studiem na {self.base_url}.\nDetail: {msg}"

        chosen_model = model_id if (model_id and model_id in models) else (models[0] if models else "local-model")

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": chosen_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Jsi profesionální AI asistent a editor napojený na Bionic Studio. "
                        "Zpracováváš audio přepisy přesně podle zadání uživatele. Odpovídej přehledně, "
                        "strukturovaně (pomocí odrážek a nadpisů) a v jazyce, který požaduje uživatel (výchozí čeština)."
                    ),
                },
                {
                    "role": "user",
                    "content": f"{task_prompt}\n\n---\nZDE JE PŘEPIS AUDIA:\n{transcript_text}",
                },
            ],
            "temperature": 0.3,
        }

        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=180,
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    return True, content.strip()
                return False, "Bionic Studio vrátilo prázdnou odpověď."
            else:
                return False, f"Chyba API ({resp.status_code}): {resp.text}"
        except Exception as e:
            return False, f"Chyba při komunikaci s Bionic Studiem: {str(e)}"
