import os
import sys
import json
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any
import requests

from media_engine import get_ffmpeg_exe, CREATE_NO_WINDOW
from gguf_whisper_loader import find_local_bionic_whisper_gguf, convert_bionic_gguf_to_ct2


def _register_nvidia_dll_paths():
    """
    Zajistí, aby CTranslate2 / Faster-Whisper na Windows automaticky našel
    CUDA 12 knihovny (cublas64_12.dll, cublasLt64_12.dll, cudart64_12.dll)
    přímo z Bionic Studia / LM Studia nebo z Python venvu.
    """
    if os.name != "nt":
        return

    extra_dll_dirs = []

    # 1. Bionic Studio / LM Studio CUDA 12 vendor knihovny
    try:
        home = Path.home()
        lms_backends = home / ".lmstudio" / "extensions" / "backends"
        if lms_backends.is_dir():
            for root, _, files in os.walk(lms_backends):
                for f in files:
                    if f.lower() in {"cublas64_12.dll", "cudart64_12.dll"}:
                        extra_dll_dirs.append(root)
                        break
    except Exception:
        pass

    # 2. Případné nvidia balíčky ve venvu
    try:
        import site
        for pkg_dir in site.getsitepackages():
            nvidia_dir = os.path.join(pkg_dir, "nvidia")
            if os.path.isdir(nvidia_dir):
                for root, dirs, _ in os.walk(nvidia_dir):
                    if "bin" in dirs:
                        extra_dll_dirs.append(os.path.join(root, "bin"))
    except Exception:
        pass

    for bin_path in extra_dll_dirs:
        if os.path.isdir(bin_path):
            os.environ["PATH"] = bin_path + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(bin_path)
                except Exception:
                    pass
            # Explicitně přednačíst CUDA 12 knihovny do paměti procesu přes ctypes
            try:
                import ctypes
                for dll_name in ("cudart64_12.dll", "cublasLt64_12.dll", "cublas64_12.dll"):
                    dll_full = os.path.join(bin_path, dll_name)
                    if os.path.isfile(dll_full):
                        ctypes.WinDLL(dll_full)
            except Exception:
                pass


_register_nvidia_dll_paths()


def detect_bionic_server_url() -> str:
    """
    Automaticky zjistí port lokálního serveru Bionic Studia / LM Studia
    ze souboru ~/.lmstudio/apps/bionic/.internal/http-server-config.json (např. 1235 nebo 1234).
    """
    try:
        cfg_path = Path.home() / ".lmstudio" / "apps" / "bionic" / ".internal" / "http-server-config.json"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                port = data.get("port")
                if port:
                    return f"http://localhost:{port}/v1"
    except Exception:
        pass

    # Otestovat běžné porty 1235 a 1234
    for port in (1235, 1234):
        try:
            r = requests.get(f"http://localhost:{port}/v1/models", timeout=0.8)
            if r.status_code == 200:
                return f"http://localhost:{port}/v1"
        except Exception:
            pass

    return "http://localhost:1235/v1"


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
    """
    Správa lokálního Whisper modelu s přímým využitím již staženého modelu v Bionic Studiu
    (whisper-large-v3-turbo-Q8_0.gguf) na RTX 5060 Ti (CUDA float16) bez stahování z internetu.
    """

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._loaded_model = None
        self._loaded_model_key = None

    def get_local_bionic_model_path(self) -> Optional[str]:
        return find_local_bionic_whisper_gguf()

    def _extract_temp_wav(
        self,
        media_path: str,
        trim_start_sec: Optional[float] = None,
        trim_end_sec: Optional[float] = None,
    ) -> str:
        """Extrahuje 16kHz mono WAV pro maximální rychlost a spolehlivost přepisu z jakéhokoli videa/audia."""
        ffmpeg = get_ffmpeg_exe()
        tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="whisper_audio_")
        os.close(tmp_fd)
        cmd = [ffmpeg, "-y", "-hide_banner"]
        if trim_start_sec is not None and trim_start_sec > 0:
            cmd += ["-ss", f"{trim_start_sec:.2f}"]
        if trim_end_sec is not None and trim_end_sec > 0:
            cmd += ["-to", f"{trim_end_sec:.2f}"]
        cmd += [
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

    def _resolve_model_dir(
        self,
        model_size: str,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> tuple[str, bool]:
        """
        Pokud je zvolen model z Bionic Studia nebo 'large-v3-turbo' a na disku existuje
        Bionic Studio GGUF model, převede/načte ho 100% lokálně bez internetu.
        Vrací (model_path_or_id, is_local_dir).
        """
        local_gguf = find_local_bionic_whisper_gguf()
        wants_turbo_or_bionic = (
            "bionic" in model_size.lower()
            or "turbo" in model_size.lower()
        )
        if local_gguf and wants_turbo_or_bionic:
            ct2_dir = os.path.join(self.cache_dir, "bionic-whisper-large-v3-turbo-ct2")
            convert_bionic_gguf_to_ct2(
                gguf_path=local_gguf,
                output_dir=ct2_dir,
                quantization="float16",
                progress_callback=progress_callback,
            )
            return ct2_dir, True

        model_map = {
            "large-v3 (Maximální přesnost)": "large-v3",
            "medium (Střední velikost)": "medium",
            "small (Rychlý, menší nároky)": "small",
            "base (Základní rychlý)": "base",
        }
        return model_map.get(model_size, model_size), False

    def transcribe(
        self,
        media_path: str,
        model_size: str = "Bionic Studio: whisper-large-v3-turbo (Lokální model - bez stahování)",
        language: Optional[str] = None,
        device_preference: str = "cuda",
        word_timestamps: bool = False,
        trim_start_sec: Optional[float] = None,
        trim_end_sec: Optional[float] = None,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        segment_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> TranscriptionResult:
        from faster_whisper import WhisperModel

        if progress_callback:
            progress_callback(0.02, "Připravuji zvukovou stopu (16 kHz WAV)...")

        start_offset = float(trim_start_sec) if (trim_start_sec is not None and trim_start_sec > 0) else 0.0
        tmp_wav = None
        try:
            tmp_wav = self._extract_temp_wav(media_path, trim_start_sec=trim_start_sec, trim_end_sec=trim_end_sec)

            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Přepis byl zrušen.")

            actual_model_id, is_local = self._resolve_model_dir(model_size, progress_callback)

            use_cuda = "cuda" in device_preference.lower() or "gpu" in device_preference.lower()
            device = "cuda" if use_cuda else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"

            model_key = f"{actual_model_id}:{device}:{compute_type}"
            if self._loaded_model is None or self._loaded_model_key != model_key:
                if progress_callback:
                    progress_callback(
                        0.08,
                        f"Inicializuji lokální model na {device.upper()} ({compute_type})..."
                    )
                try:
                    self._loaded_model = WhisperModel(
                        actual_model_id,
                        device=device,
                        compute_type=compute_type,
                        download_root=None if is_local else self.cache_dir,
                        local_files_only=is_local,
                    )
                    self._loaded_model_key = model_key
                except Exception as cuda_err:
                    if device == "cuda":
                        if progress_callback:
                            progress_callback(
                                0.09,
                                f"CUDA inicializace selhala ({cuda_err}), přepínám na CPU (int8) na Ryzenu 9600..."
                            )
                        device = "cpu"
                        compute_type = "int8"
                        model_key = f"{actual_model_id}:cpu:int8"
                        self._loaded_model = WhisperModel(
                            actual_model_id,
                            device="cpu",
                            compute_type="int8",
                            download_root=None if is_local else self.cache_dir,
                            local_files_only=is_local,
                            cpu_threads=8,
                        )
                        self._loaded_model_key = model_key
                    else:
                        raise

            if progress_callback:
                progress_callback(0.12, "Spouštím AI přepis audia...")

            lang_code = None
            if language and not language.startswith("Auto"):
                lang_code = language.split(" ")[0].strip().lower()

            import itertools
            try:
                segments_gen, info = self._loaded_model.transcribe(
                    tmp_wav,
                    language=lang_code,
                    beam_size=5,
                    word_timestamps=word_timestamps,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
                first_item = next(segments_gen, None)
                if first_item is not None:
                    segments_gen = itertools.chain([first_item], segments_gen)
                else:
                    segments_gen = iter([])
            except Exception as runtime_cuda_err:
                if device == "cuda" and ("cublas" in str(runtime_cuda_err).lower() or "cuda" in str(runtime_cuda_err).lower() or "cudnn" in str(runtime_cuda_err).lower()):
                    if progress_callback:
                        progress_callback(0.14, f"CUDA knihovna nebyla nalezena ({runtime_cuda_err}), přepínám na CPU (Ryzen 9600)...")
                    device = "cpu"
                    compute_type = "int8"
                    model_key = f"{actual_model_id}:cpu:int8"
                    self._loaded_model = WhisperModel(
                        actual_model_id,
                        device="cpu",
                        compute_type="int8",
                        download_root=None if is_local else self.cache_dir,
                        local_files_only=is_local,
                        cpu_threads=8,
                    )
                    self._loaded_model_key = model_key
                    segments_gen, info = self._loaded_model.transcribe(
                        tmp_wav,
                        language=lang_code,
                        beam_size=5,
                        word_timestamps=word_timestamps,
                        vad_filter=True,
                        vad_parameters=dict(min_silence_duration_ms=500),
                    )
                else:
                    raise

            total_dur = info.duration if info.duration and info.duration > 0 else 1.0
            collected_segments = []

            for seg in segments_gen:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Přepis byl zrušen uživatelem.")

                words_list = []
                if word_timestamps and getattr(seg, "words", None):
                    for w in seg.words:
                        words_list.append({
                            "start": float(w.start) + start_offset,
                            "end": float(w.end) + start_offset,
                            "word": w.word.strip(),
                        })

                seg_dict = {
                    "start": float(seg.start) + start_offset,
                    "end": float(seg.end) + start_offset,
                    "text": seg.text.strip(),
                    "words": words_list,
                }
                collected_segments.append(seg_dict)

                if segment_callback:
                    segment_callback(seg_dict)

                if progress_callback:
                    frac = 0.12 + 0.86 * min(1.0, seg.end / total_dur)
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
    Klient pro propojení s Bionic Studio / LM Studio lokálním serverem (OpenAI-kompatibilní API).
    Automaticky detekuje port (1235 nebo 1234) a odfiltruje non-LLM modely (whisper, surya-ocr, embedding).
    """

    NON_LLM_KEYWORDS = ("whisper", "embed", "surya", "ocr", "tts", "nomic")

    def __init__(self, base_url: Optional[str] = None, api_key: str = "lm-studio"):
        self.base_url = (base_url or detect_bionic_server_url()).rstrip("/")
        self.api_key = api_key

    def _filter_llm_models(self, models: List[str]) -> List[str]:
        llm_only = [
            m for m in models
            if not any(kw in m.lower() for kw in self.NON_LLM_KEYWORDS)
        ]
        return llm_only if llm_only else models

    def check_connection(self) -> tuple[bool, str, List[str]]:
        """
        Otestuje spojení s Bionic / LM Studio serverem. Pokud na zadaném portu neodpovídá,
        automaticky zkusí detekovaný port z Bionic Studia (např. 1235 <-> 1234).
        """
        urls_to_try = [self.base_url]
        auto_url = detect_bionic_server_url().rstrip("/")
        if auto_url not in urls_to_try:
            urls_to_try.append(auto_url)
        for fallback in ("http://localhost:1235/v1", "http://localhost:1234/v1"):
            if fallback not in urls_to_try:
                urls_to_try.append(fallback)

        for candidate_url in urls_to_try:
            try:
                resp = requests.get(
                    f"{candidate_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=3,
                )
                if resp.status_code == 200:
                    self.base_url = candidate_url
                    data = resp.json()
                    all_models = [m.get("id", "unknown") for m in data.get("data", [])]
                    llm_models = self._filter_llm_models(all_models)
                    has_whisper = any("whisper" in m.lower() for m in all_models)
                    whisper_tag = " + Whisper Turbo nalezen" if has_whisper else ""
                    if llm_models:
                        return True, f"Připojeno ({llm_models[0]}{whisper_tag})", llm_models
                    return True, f"Server běží ({len(all_models)} modelů)", all_models
            except Exception:
                continue

        return False, "Server neběží (zapněte Bionic Studio na portu 1235/1234)", []

    def process_transcript(
        self,
        transcript_text: str,
        task_prompt: str,
        model_id: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Odešle přepsaný text do Bionic Studia ke zpracování pomocí načteného LLM (např. Gemma-4-12B nebo Qwen3.8).
        """
        if not transcript_text.strip():
            return False, "Přepis je prázdný – nejprve prosím spusťte AI přepis audia."

        ok, msg, llm_models = self.check_connection()
        if not ok:
            return False, f"Nelze se spojit s Bionic Studiem na {self.base_url}.\nDetail: {msg}"

        chosen_model = model_id if (model_id and model_id in llm_models) else (llm_models[0] if llm_models else "local-model")

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
            "temperature": 0.5,
            "repeat_penalty": 1.15,
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
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
