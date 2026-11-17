import os
import sys
import json
import threading
import subprocess
from pathlib import Path
from tkinter import filedialog, messagebox, colorchooser
import customtkinter as ctk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

from media_engine import (
    probe_media_info,
    check_nvenc_available,
    build_audio_command,
    build_video_command,
    run_ffmpeg_with_progress,
)
from ai_engine import (
    WhisperTranscriber,
    BionicStudioClient,
    TranscriptionResult,
    format_timestamp_short,
    detect_bionic_server_url,
)
from youtube_engine import fetch_youtube_info, download_youtube_media
from reels_engine import (
    ReelsSubtitleStyle,
    parse_time_input,
    group_words_into_reels_chunks,
    format_chunks_for_editor,
    parse_chunks_from_editor,
    correct_chunks_with_gemma,
    render_style_preview_image,
    render_reels_video_overlay,
)

# Nastavení vzhledu
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Oddělení procesu od výchozí ikony pythonw.exe na hlavním panelu Windows (Taskbar)
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BionicStudio.MediaAudioStudio.App")
    except Exception:
        pass

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts"}
SUPPORTED_AUDIO_EXTS = {".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".wma"}
ALL_SUPPORTED_EXTS = SUPPORTED_VIDEO_EXTS | SUPPORTED_AUDIO_EXTS


class BaseWindow(ctk.CTk, TkinterDnD.DnDWrapper if HAS_DND else object):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if HAS_DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:
                pass


class BionicMediaStudioApp(BaseWindow):
    def __init__(self):
        super().__init__()

        self.title("Bionic Media & Audio Studio — RTX 5060 Ti & Ryzen 9600 Edition")
        self.geometry("1280x860")
        self.minsize(1100, 750)
        self._apply_app_icon()
        # CustomTkinter přepisuje ikonu okna po 200 ms, proto ji znovu nastavíme po 220 ms a 550 ms
        self.after(220, self._apply_app_icon)
        self.after(550, self._apply_app_icon)

        # Stav aplikace
        self.files: list[str] = []
        self.selected_file: str | None = None
        self.selected_file_info: dict = {}
        self.output_dir: str = ""
        self.is_busy: bool = False
        self.cancel_event = threading.Event()
        self.nvenc_available = check_nvenc_available()

        self.transcriber = WhisperTranscriber()
        self.last_transcription: TranscriptionResult | None = None
        self.bionic_client = BionicStudioClient()

        # Stav pro Reels Titulky & Video Overlay
        self.reels_chunks: list[dict] = []
        self.reels_primary_hex: str = "#FFFFFF"
        self.reels_highlight_hex: str = "#FFE600"
        self.reels_outline_hex: str = "#000000"

        self._load_settings()
        self._build_ui()
        self._setup_drag_and_drop()

        # Automatický test Bionic Studio připojení na pozadí
        threading.Thread(target=self._refresh_bionic_status_bg, daemon=True).start()

    def _apply_app_icon(self):
        """Nastaví vlastní ikonu aplikace do titulkového pruhu i na hlavní panel Windows (Taskbar)."""
        ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
        if not os.path.exists(ico_path):
            try:
                from generate_icon import create_app_icon
                create_app_icon(ico_path)
            except Exception:
                return

        try:
            self.iconbitmap(default=ico_path)
            self.iconbitmap(ico_path)
        except Exception:
            pass

        try:
            from PIL import Image, ImageTk
            img = Image.open(ico_path)
            self._icon_photos = [
                ImageTk.PhotoImage(img.resize((256, 256), Image.Resampling.LANCZOS)),
                ImageTk.PhotoImage(img.resize((64, 64), Image.Resampling.LANCZOS)),
                ImageTk.PhotoImage(img.resize((48, 48), Image.Resampling.LANCZOS)),
                ImageTk.PhotoImage(img.resize((32, 32), Image.Resampling.LANCZOS)),
                ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS)),
            ]
            self.wm_iconphoto(True, *self._icon_photos)
        except Exception:
            pass

        if os.name == "nt":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                LR_LOADFROMFILE = 0x00000010
                IMAGE_ICON = 1
                WM_SETICON = 0x0080
                ICON_SMALL = 0
                ICON_BIG = 1
                GCLP_HICON = -14
                GCLP_HICONSM = -34
                GA_ROOT = 2

                hicon_big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
                hicon_small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)

                hwnd = self.winfo_id()
                parent_hwnd = user32.GetParent(hwnd)
                root_hwnd = user32.GetAncestor(hwnd, GA_ROOT)

                for target_hwnd in {hwnd, parent_hwnd, root_hwnd}:
                    if target_hwnd:
                        if hicon_big:
                            user32.SendMessageW(target_hwnd, WM_SETICON, ICON_BIG, hicon_big)
                            if hasattr(user32, "SetClassLongPtrW"):
                                user32.SetClassLongPtrW(target_hwnd, GCLP_HICON, hicon_big)
                        if hicon_small:
                            user32.SendMessageW(target_hwnd, WM_SETICON, ICON_SMALL, hicon_small)
                            if hasattr(user32, "SetClassLongPtrW"):
                                user32.SetClassLongPtrW(target_hwnd, GCLP_HICONSM, hicon_small)
            except Exception:
                pass

    def _load_settings(self):
        detected_url = detect_bionic_server_url()
        self.settings = {
            "bionic_url": detected_url,
            "bionic_api_key": "lm-studio",
            "whisper_model": "Bionic Studio: whisper-large-v3-turbo (Lokální model - bez stahování)",
            "whisper_device": "GPU - NVIDIA CUDA (RTX 5060 Ti 16GB FP16)",
            "whisper_lang": "Auto (Automatická detekce)",
            "output_dir": "",
        }
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    # Pokud měl uživatel starou výchozí adresu 1234 a Bionic běží na 1235, aktualizujeme ji
                    if saved.get("bionic_url") == "http://localhost:1234/v1" and detected_url != "http://localhost:1234/v1":
                        saved["bionic_url"] = detected_url
                    if "Doporučeno pro RTX 5060 Ti" in saved.get("whisper_model", ""):
                        saved["whisper_model"] = "Bionic Studio: whisper-large-v3-turbo (Lokální model - bez stahování)"
                    self.settings.update(saved)
            except Exception:
                pass
        self.output_dir = self.settings.get("output_dir", "")
        self.bionic_client = BionicStudioClient(
            base_url=self.settings["bionic_url"],
            api_key=self.settings["bionic_api_key"],
        )

    def _save_settings(self):
        try:
            self.settings["bionic_url"] = self.entry_bionic_url.get().strip()
            self.settings["whisper_model"] = self.combo_whisper_model.get()
            self.settings["whisper_device"] = self.combo_whisper_device.get()
            self.settings["whisper_lang"] = self.combo_whisper_lang.get()
            self.settings["output_dir"] = self.output_dir
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=340)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ==================== HORNÍ HLAVIČKA ====================
        header = ctk.CTkFrame(self, height=64, corner_radius=0, fg_color=("#e2e8f0", "#111827"))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        title_lbl = ctk.CTkLabel(
            header,
            text="🎬 Bionic Media & Audio Studio",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title_lbl.grid(row=0, column=0, padx=20, pady=(10, 2), sticky="w")

        sub_lbl = ctk.CTkLabel(
            header,
            text="Extrakce audia • Konverze Audio/Video (MP4, MOV, AAC, MP3, WAV, FLAC) • Lokální AI Přepis & Bionic Studio",
            font=ctk.CTkFont(size=12),
            text_color=("gray30", "gray70"),
        )
        sub_lbl.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="w")

        hw_badge_text = "⚡ GPU: NVIDIA RTX 5060 Ti (16GB VRAM) + Ryzen 9600"
        if self.nvenc_available:
            hw_badge_text += " | NVENC Aktivní"
        hw_badge = ctk.CTkLabel(
            header,
            text=hw_badge_text,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#dbeafe", "#1e3a8a"),
            text_color=("#1e40af", "#93c5fd"),
            corner_radius=8,
            padx=12,
            pady=6,
        )
        hw_badge.grid(row=0, column=2, rowspan=2, padx=20, pady=12, sticky="e")

        # ==================== LEVÝ PANEL (SOUBORY & DRAG AND DROP) ====================
        left_panel = ctk.CTkFrame(self, width=340, corner_radius=12)
        left_panel.grid(row=1, column=0, padx=(16, 8), pady=12, sticky="nsew")
        left_panel.grid_rowconfigure(2, weight=1)
        left_panel.grid_columnconfigure(0, weight=1)

        # Drop zóna
        self.drop_frame = ctk.CTkFrame(
            left_panel,
            height=115,
            corner_radius=10,
            border_width=2,
            border_color=("#3b82f6", "#2563eb"),
            fg_color=("#eff6ff", "#172554"),
        )
        self.drop_frame.grid(row=0, column=0, padx=12, pady=(12, 8), sticky="ew")
        self.drop_frame.grid_propagate(False)
        self.drop_frame.grid_columnconfigure(0, weight=1)

        drop_title = ctk.CTkLabel(
            self.drop_frame,
            text="📥 Přetáhněte videa nebo audia sem\n(MP4, MOV, MKV, AAC, MP3, WAV, FLAC...)",
            font=ctk.CTkFont(size=13, weight="bold"),
            justify="center",
        )
        drop_title.grid(row=0, column=0, padx=10, pady=(14, 6))

        btn_row = ctk.CTkFrame(self.drop_frame, fg_color="transparent")
        btn_row.grid(row=1, column=0, pady=(2, 10))

        self.btn_add_files = ctk.CTkButton(
            btn_row,
            text="➕ Vybrat soubory",
            width=135,
            height=30,
            command=self._on_add_files,
        )
        self.btn_add_files.pack(side="left", padx=4)

        self.btn_clear_files = ctk.CTkButton(
            btn_row,
            text="🗑️ Vyčistit seznam",
            width=125,
            height=30,
            fg_color=("gray70", "gray30"),
            hover_color=("gray60", "gray40"),
            command=self._on_clear_files,
        )
        self.btn_clear_files.pack(side="left", padx=4)

        # Nadpis seznamu
        self.lbl_queue_count = ctk.CTkLabel(
            left_panel,
            text="Fronta souborů (0):",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self.lbl_queue_count.grid(row=1, column=0, padx=14, pady=(4, 2), sticky="ew")

        # Scrollovací seznam souborů
        self.file_list_frame = ctk.CTkScrollableFrame(left_panel, corner_radius=8)
        self.file_list_frame.grid(row=2, column=0, padx=12, pady=4, sticky="nsew")
        self.file_list_frame.grid_columnconfigure(0, weight=1)

        # Detail vybraného souboru
        self.info_box = ctk.CTkFrame(left_panel, corner_radius=8, fg_color=("#f1f5f9", "#1e293b"))
        self.info_box.grid(row=3, column=0, padx=12, pady=(6, 8), sticky="ew")
        self.lbl_file_info = ctk.CTkLabel(
            self.info_box,
            text="Žádný soubor není vybrán.\nPřidejte video nebo audio výše.",
            font=ctk.CTkFont(size=11),
            justify="left",
            anchor="w",
            wraplength=295,
        )
        self.lbl_file_info.pack(padx=10, pady=8, fill="x")

        # Výstupní složka
        out_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        out_frame.grid(row=4, column=0, padx=12, pady=(2, 12), sticky="ew")
        out_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            out_frame,
            text="📁 Výstupní složka (prázdné = vedle originálu):",
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self.entry_out_dir = ctk.CTkEntry(out_frame, placeholder_text="Stejná složka jako zdrojový soubor")
        self.entry_out_dir.grid(row=1, column=0, sticky="ew", padx=(0, 4))
        if self.output_dir:
            self.entry_out_dir.insert(0, self.output_dir)

        btn_pick_dir = ctk.CTkButton(out_frame, text="...", width=34, command=self._on_pick_output_dir)
        btn_pick_dir.grid(row=1, column=1, padx=(0, 4))

        btn_open_dir = ctk.CTkButton(
            out_frame,
            text="📂",
            width=34,
            fg_color=("gray70", "gray30"),
            command=self._on_open_output_dir,
        )
        btn_open_dir.grid(row=1, column=2)

        # ==================== PRAVÝ PANEL (ZÁLOŽKY) ====================
        self.tabview = ctk.CTkTabview(self, corner_radius=12)
        self.tabview.grid(row=1, column=1, padx=(8, 16), pady=12, sticky="nsew")

        self.tab_audio = self.tabview.add("🎵 Extrakce & Konverze Audia")
        self.tab_video = self.tabview.add("🎬 Konverze Videa (MP4 ⇄ MOV)")
        self.tab_yt = self.tabview.add("📥 YouTube Stahovač (MP4 / MOV)")
        self.tab_reels = self.tabview.add("🔥 Reels Titulky & Overlay (DaVinci)")
        self.tab_ai = self.tabview.add("🤖 AI Přepis & Bionic Studio")
        self.tab_help = self.tabview.add("📖 Návod & Nastavení")

        self._build_tab_audio()
        self._build_tab_video()
        self._build_tab_youtube()
        self._build_tab_reels()
        self._build_tab_ai()
        self._build_tab_help()

        # ==================== SPODNÍ STAVOVÝ ŘÁDEK & PROGRESS ====================
        bottom_bar = ctk.CTkFrame(self, height=58, corner_radius=0, fg_color=("#e2e8f0", "#0f172a"))
        bottom_bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        bottom_bar.grid_columnconfigure(0, weight=1)

        self.lbl_status = ctk.CTkLabel(
            bottom_bar,
            text="Připraveno. Přetáhněte soubory do levého panelu nebo klikněte na 'Vybrat soubory'.",
            font=ctk.CTkFont(size=12),
            anchor="w",
        )
        self.lbl_status.grid(row=0, column=0, padx=20, pady=(8, 2), sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(bottom_bar, height=12)
        self.progress_bar.grid(row=1, column=0, padx=20, pady=(2, 10), sticky="ew")
        self.progress_bar.set(0.0)

        self.btn_cancel = ctk.CTkButton(
            bottom_bar,
            text="⏹ Zrušit úlohu",
            width=120,
            fg_color="#dc2626",
            hover_color="#b91c1c",
            state="disabled",
            command=self._on_cancel_task,
        )
        self.btn_cancel.grid(row=0, column=1, rowspan=2, padx=20, pady=10)

    # ------------------------------------------------------------------
    # ZÁLOŽKA 1: EXTRAKCE & KONVERZE AUDIA
    # ------------------------------------------------------------------
    def _build_tab_audio(self):
        tab = self.tab_audio
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)

        intro = ctk.CTkFrame(tab, corner_radius=10, fg_color=("#eff6ff", "#1e293b"))
        intro.grid(row=0, column=0, columnspan=2, padx=14, pady=(10, 14), sticky="ew")
        ctk.CTkLabel(
            intro,
            text=(
                "💡 Zde můžete extrahovat zvuk z jakéhokoliv videa (MP4, MOV, MKV, AVI, WEBM) nebo "
                "vzájemně převádět audio formáty mezi sebou (např. AAC ⇄ MP3 ⇄ WAV ⇄ FLAC ⇄ M4A ⇄ OGG)."
            ),
            font=ctk.CTkFont(size=12),
            wraplength=720,
            justify="left",
        ).pack(padx=14, pady=10, anchor="w")

        # Levý sloupec nastavení
        col1 = ctk.CTkFrame(tab, corner_radius=10)
        col1.grid(row=1, column=0, padx=(14, 7), pady=6, sticky="nsew")
        col1.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(col1, text="1. Cílový audio formát:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(16, 6), anchor="w"
        )
        self.combo_audio_fmt = ctk.CTkOptionMenu(
            col1,
            values=[
                "mp3 (Univerzální MP3 - libmp3lame)",
                "aac (Pokročilý kodek AAC - .aac)",
                "m4a (Apple / AAC kontejner - .m4a)",
                "wav (Bezztrátový nekomprimovaný PCM 16-bit)",
                "flac (Bezztrátový komprimovaný FLAC)",
                "ogg (Ogg Vorbis)",
                "opus (Moderní vysokoúčinný Opus)",
            ],
            height=36,
            command=self._on_audio_fmt_change,
        )
        self.combo_audio_fmt.pack(padx=16, pady=(0, 14), fill="x")

        ctk.CTkLabel(col1, text="2. Kvalita / Bitrate:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(4, 6), anchor="w"
        )
        self.combo_audio_bitrate = ctk.CTkOptionMenu(
            col1,
            values=[
                "320k (Maximální studiová kvalita)",
                "256k (Vysoká kvalita)",
                "192k (Doporučený standard)",
                "128k (Dobrá kvalita / menší soubor)",
                "96k (Kompaktní - mluvené slovo)",
                "64k (Minimální velikost)",
            ],
            height=36,
        )
        self.combo_audio_bitrate.set("192k (Doporučený standard)")
        self.combo_audio_bitrate.pack(padx=16, pady=(0, 16), fill="x")

        # Pravý sloupec nastavení
        col2 = ctk.CTkFrame(tab, corner_radius=10)
        col2.grid(row=1, column=1, padx=(7, 14), pady=6, sticky="nsew")
        col2.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(col2, text="3. Vzorkovací frekvence (Sample Rate):", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(16, 6), anchor="w"
        )
        self.combo_audio_sr = ctk.CTkOptionMenu(
            col2,
            values=["Původní", "48000 Hz (DVD / Video standard)", "44100 Hz (CD standard)", "22050 Hz", "16000 Hz (Hlas / AI)"],
            height=36,
        )
        self.combo_audio_sr.pack(padx=16, pady=(0, 14), fill="x")

        ctk.CTkLabel(col2, text="4. Zvukové kanály:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(4, 6), anchor="w"
        )
        self.combo_audio_ch = ctk.CTkOptionMenu(
            col2,
            values=["Původní", "Stereo (2.0)", "Mono (1.0)"],
            height=36,
        )
        self.combo_audio_ch.pack(padx=16, pady=(0, 14), fill="x")

        self.chk_audio_norm = ctk.CTkCheckBox(
            col2,
            text="Vyrovnat hlasitost (EBU R128 Loudness Normalization)",
            font=ctk.CTkFont(size=12),
        )
        self.chk_audio_norm.pack(padx=16, pady=(6, 16), anchor="w")

        # Spouštěcí tlačítka
        act_frame = ctk.CTkFrame(tab, fg_color="transparent")
        act_frame.grid(row=2, column=0, columnspan=2, padx=14, pady=24, sticky="ew")
        act_frame.grid_columnconfigure(0, weight=1)

        self.btn_run_audio = ctk.CTkButton(
            act_frame,
            text="🎵 Extrahovat / Převést Audio (všechny soubory ve frontě)",
            height=50,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            command=self._start_audio_conversion,
        )
        self.btn_run_audio.grid(row=0, column=0, sticky="ew")

    def _on_audio_fmt_change(self, choice: str):
        fmt = choice.split(" ")[0].lower()
        if fmt in {"wav", "flac"}:
            self.combo_audio_bitrate.configure(state="disabled")
        else:
            self.combo_audio_bitrate.configure(state="normal")

    # ------------------------------------------------------------------
    # ZÁLOŽKA 2: KONVERZE VIDEA (MP4 <-> MOV <-> MKV...)
    # ------------------------------------------------------------------
    def _build_tab_video(self):
        tab = self.tab_video
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)

        intro = ctk.CTkFrame(tab, corner_radius=10, fg_color=("#eff6ff", "#1e293b"))
        intro.grid(row=0, column=0, columnspan=2, padx=14, pady=(10, 14), sticky="ew")
        ctk.CTkLabel(
            intro,
            text=(
                "🎬 Převádějte video soubory mezi formáty MP4, MOV, MKV, WEBM a AVI.\n"
                "• Režim 'Stream Copy' změní kontejner (např. MOV ⇄ MP4) během 1 sekundy bez jakékoliv ztráty kvality.\n"
                "• Režim 'NVIDIA NVENC' využije hardwarový enkodér vaší RTX 5060 Ti pro ultra-rychlou rekompresi."
            ),
            font=ctk.CTkFont(size=12),
            wraplength=720,
            justify="left",
        ).pack(padx=14, pady=10, anchor="w")

        col1 = ctk.CTkFrame(tab, corner_radius=10)
        col1.grid(row=1, column=0, padx=(14, 7), pady=6, sticky="nsew")
        col1.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(col1, text="1. Cílový video formát:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(16, 6), anchor="w"
        )
        self.combo_video_fmt = ctk.CTkOptionMenu(
            col1,
            values=[
                "mp4 (Univerzální MPEG-4 Part 14)",
                "mov (Apple QuickTime Movie)",
                "mkv (Matroska Multimedia Container)",
                "webm (WebM Video VP9)",
                "avi (Audio Video Interleave)",
            ],
            height=36,
        )
        self.combo_video_fmt.pack(padx=16, pady=(0, 14), fill="x")

        ctk.CTkLabel(col1, text="2. Režim enkódování / Akcelerace:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(4, 6), anchor="w"
        )
        enc_modes = [
            "Stream Copy (Bleskový převod bez rekomprese)",
            "GPU: NVIDIA NVENC H.264 (RTX 5060 Ti)",
            "GPU: NVIDIA NVENC HEVC / H.265 (RTX 5060 Ti)",
            "CPU: x264 (Ryzen 9600 Software Encode)",
        ]
        self.combo_video_mode = ctk.CTkOptionMenu(col1, values=enc_modes, height=36)
        self.combo_video_mode.pack(padx=16, pady=(0, 16), fill="x")

        col2 = ctk.CTkFrame(tab, corner_radius=10)
        col2.grid(row=1, column=1, padx=(7, 14), pady=6, sticky="nsew")
        col2.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(col2, text="3. Kvalita obrazu (při rekompresi):", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(16, 6), anchor="w"
        )
        self.combo_video_quality = ctk.CTkOptionMenu(
            col2,
            values=[
                "Bezztrátová / Maximální (CRF 15 / CQ 15)",
                "Vysoká kvalita (CRF 19 / CQ 19)",
                "Vyvážená kvalita a velikost (CRF 23 / CQ 23)",
                "Menší soubor (CRF 28 / CQ 28)",
            ],
            height=36,
        )
        self.combo_video_quality.set("Vysoká kvalita (CRF 19 / CQ 19)")
        self.combo_video_quality.pack(padx=16, pady=(0, 14), fill="x")

        ctk.CTkLabel(col2, text="4. Změna rozlišení:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(4, 6), anchor="w"
        )
        self.combo_video_res = ctk.CTkOptionMenu(
            col2,
            values=["Původní", "1080p (Full HD)", "720p (HD)", "480p (SD)"],
            height=36,
        )
        self.combo_video_res.pack(padx=16, pady=(0, 16), fill="x")

        act_frame = ctk.CTkFrame(tab, fg_color="transparent")
        act_frame.grid(row=2, column=0, columnspan=2, padx=14, pady=24, sticky="ew")
        act_frame.grid_columnconfigure(0, weight=1)

        self.btn_run_video = ctk.CTkButton(
            act_frame,
            text="🎬 Převést Video soubory (MP4 ⇄ MOV / MKV / WEBM / AVI)",
            height=50,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#059669",
            hover_color="#047857",
            command=self._start_video_conversion,
        )
        self.btn_run_video.grid(row=0, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # ZÁLOŽKA 2.5: YOUTUBE STAHOVAČ (MP4 / MOV / AUDIO)
    # ------------------------------------------------------------------
    def _build_tab_youtube(self):
        tab = self.tab_yt
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)

        intro = ctk.CTkFrame(tab, corner_radius=10, fg_color=("#eff6ff", "#1e293b"))
        intro.grid(row=0, column=0, columnspan=2, padx=14, pady=(10, 12), sticky="ew")
        ctk.CTkLabel(
            intro,
            text=(
                "📥 Stahujte videa nebo samotný zvuk přímo z YouTube podle odkazu do formátů MP4, MOV, MKV, MP3, AAC nebo WAV.\n"
                "• Po stažení se soubor automaticky přidá do levé fronty, takže z něj můžete hned udělat AI přepis nebo konverzi!"
            ),
            font=ctk.CTkFont(size=12),
            wraplength=720,
            justify="left",
        ).pack(padx=14, pady=10, anchor="w")

        # Pole pro vložení odkazu
        url_frame = ctk.CTkFrame(tab, corner_radius=10)
        url_frame.grid(row=1, column=0, columnspan=2, padx=14, pady=6, sticky="ew")
        url_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            url_frame,
            text="🔗 Odkaz na YouTube video (URL):",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=16, pady=(14, 6), sticky="w")

        self.entry_yt_url = ctk.CTkEntry(
            url_frame,
            placeholder_text="https://www.youtube.com/watch?v=... nebo https://youtu.be/...",
            height=38,
            font=ctk.CTkFont(size=13),
        )
        self.entry_yt_url.grid(row=1, column=0, padx=(16, 6), pady=(0, 12), sticky="ew")

        btn_paste_yt = ctk.CTkButton(
            url_frame,
            text="📋 Vložit ze schránky",
            width=140,
            height=38,
            fg_color=("gray70", "gray30"),
            hover_color=("gray60", "gray40"),
            command=self._on_paste_yt_url,
        )
        btn_paste_yt.grid(row=1, column=1, padx=4, pady=(0, 12))

        self.btn_probe_yt = ctk.CTkButton(
            url_frame,
            text="🔍 Načíst info o videu",
            width=155,
            height=38,
            command=self._on_probe_yt_url,
        )
        self.btn_probe_yt.grid(row=1, column=2, padx=(4, 16), pady=(0, 12))

        self.lbl_yt_info = ctk.CTkLabel(
            url_frame,
            text="Zadejte YouTube odkaz výše pro zobrazení názvu, délky a kanálu.",
            font=ctk.CTkFont(size=12),
            text_color=("gray30", "gray70"),
            anchor="w",
            justify="left",
        )
        self.lbl_yt_info.grid(row=2, column=0, columnspan=3, padx=16, pady=(0, 12), sticky="ew")

        # Nastavení formátu a kvality
        col1 = ctk.CTkFrame(tab, corner_radius=10)
        col1.grid(row=2, column=0, padx=(14, 7), pady=6, sticky="nsew")
        col1.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(col1, text="1. Cílový formát stažení:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(16, 6), anchor="w"
        )
        self.combo_yt_fmt = ctk.CTkOptionMenu(
            col1,
            values=[
                "mp4 (Video MP4 - H.264 / AAC - univerzální)",
                "mov (Video Apple QuickTime .MOV)",
                "mkv (Video Matroska .MKV)",
                "mp3 (Pouze zvuk .MP3 - 192 kbps)",
                "aac (Pouze zvuk .AAC - 192 kbps)",
                "wav (Pouze zvuk .WAV - bezztrátový)",
            ],
            height=36,
        )
        self.combo_yt_fmt.pack(padx=16, pady=(0, 16), fill="x")

        col2 = ctk.CTkFrame(tab, corner_radius=10)
        col2.grid(row=2, column=1, padx=(7, 14), pady=6, sticky="nsew")
        col2.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(col2, text="2. Maximální rozlišení videa:", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(16, 6), anchor="w"
        )
        self.combo_yt_res = ctk.CTkOptionMenu(
            col2,
            values=[
                "1080p (Full HD - ideální pro MP4/MOV)",
                "Nejvyšší dostupná (4K / 1440p / 1080p)",
                "1440p (2K QHD)",
                "720p (HD)",
                "480p (SD - menší soubor)",
            ],
            height=36,
        )
        self.combo_yt_res.pack(padx=16, pady=(0, 16), fill="x")

        # Možnost automaticky přidat do fronty
        opts_frame = ctk.CTkFrame(tab, fg_color="transparent")
        opts_frame.grid(row=3, column=0, columnspan=2, padx=16, pady=(8, 4), sticky="ew")

        self.chk_yt_auto_queue = ctk.CTkCheckBox(
            opts_frame,
            text="Po stažení automaticky přidat soubor do levé fronty a vybrat jej (pro okamžitý AI přepis nebo úpravy)",
            font=ctk.CTkFont(size=12),
        )
        self.chk_yt_auto_queue.select()
        self.chk_yt_auto_queue.pack(anchor="w")

        # Tlačítko stáhnout
        act_frame = ctk.CTkFrame(tab, fg_color="transparent")
        act_frame.grid(row=4, column=0, columnspan=2, padx=14, pady=16, sticky="ew")
        act_frame.grid_columnconfigure(0, weight=1)

        self.btn_run_yt = ctk.CTkButton(
            act_frame,
            text="📥 Stáhnout z YouTube (MP4 / MOV / Audio)",
            height=50,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#dc2626",
            hover_color="#b91c1c",
            command=self._start_youtube_download,
        )
        self.btn_run_yt.grid(row=0, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # ZÁLOŽKA 2.8: REELS TITULKY, GEMMA 4 OPRAVA & DAVINCI OVERLAY
    # ------------------------------------------------------------------
    def _build_tab_reels(self):
        tab = self.tab_reels
        tab.grid_columnconfigure(0, weight=5)
        tab.grid_columnconfigure(1, weight=6)
        tab.grid_rowconfigure(0, weight=1)

        # ==================== LEVÁ ČÁST: AI PIPELINE & EDITOR TITULKŮ ====================
        left_col = ctk.CTkFrame(tab, corner_radius=10)
        left_col.grid(row=0, column=0, padx=(8, 5), pady=6, sticky="nsew")
        left_col.grid_columnconfigure(0, weight=1)
        left_col.grid_rowconfigure(3, weight=1)

        # 1. Časové omezení (Trim)
        trim_frame = ctk.CTkFrame(left_col, corner_radius=8, fg_color=("#f1f5f9", "#1e293b"))
        trim_frame.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")
        trim_frame.grid_columnconfigure((1, 3, 5), weight=1)

        ctk.CTkLabel(
            trim_frame,
            text="⏱️ Časový výřez videa (nepovinné — pro otitulkování jen vybrané části):",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, columnspan=6, padx=10, pady=(8, 4), sticky="w")

        ctk.CTkLabel(trim_frame, text="Od:", font=ctk.CTkFont(size=11)).grid(row=1, column=0, padx=(10, 4), pady=(0, 8), sticky="w")
        self.entry_reels_start = ctk.CTkEntry(trim_frame, width=68, height=28, placeholder_text="00:00")
        self.entry_reels_start.insert(0, "00:00")
        self.entry_reels_start.grid(row=1, column=1, padx=2, pady=(0, 8), sticky="ew")

        ctk.CTkLabel(trim_frame, text="Do:", font=ctk.CTkFont(size=11)).grid(row=1, column=2, padx=(8, 4), pady=(0, 8), sticky="w")
        self.entry_reels_end = ctk.CTkEntry(trim_frame, width=68, height=28, placeholder_text="Konec")
        self.entry_reels_end.insert(0, "Konec")
        self.entry_reels_end.grid(row=1, column=3, padx=2, pady=(0, 8), sticky="ew")

        self.combo_reels_timeline_mode = ctk.CTkOptionMenu(
            trim_frame,
            values=[
                "Začít od 00:00 (Oříznutý klip)",
                "Původní čas videa (Celá stopa)",
            ],
            height=28,
            width=175,
            font=ctk.CTkFont(size=11),
        )
        self.combo_reels_timeline_mode.grid(row=1, column=4, columnspan=2, padx=(8, 10), pady=(0, 8), sticky="ew")

        # 2. Nastavení AI Přepisu (Whisper Turbo) + Kontextové opravy (Gemma 4 12B QAT)
        ai_cfg = ctk.CTkFrame(left_col, corner_radius=8, fg_color=("#f8fafc", "#1e293b"))
        ai_cfg.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        ai_cfg.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(ai_cfg, text="Slova na titulek:", font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=0, column=0, padx=(10, 6), pady=(8, 4), sticky="w"
        )
        self.combo_reels_words = ctk.CTkOptionMenu(
            ai_cfg,
            values=[
                "2–3 slova (Dynamické Reels / TikTok - Doporučeno)",
                "1 slovo (Hormozi Flash styl)",
                "4–5 slov (Krátké věty)",
                "6–8 slov (Delší řádky)",
            ],
            height=28,
            font=ctk.CTkFont(size=11),
        )
        self.combo_reels_words.grid(row=0, column=1, padx=(0, 10), pady=(8, 4), sticky="ew")

        ctk.CTkLabel(ai_cfg, text="Kontext / Téma:", font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=1, column=0, padx=(10, 6), pady=4, sticky="w"
        )
        self.entry_reels_hint = ctk.CTkEntry(
            ai_cfg,
            height=28,
            placeholder_text="Volitelné: O čem video je / klíčová slova pro přesnější opravu v Gemma 4...",
            font=ctk.CTkFont(size=11),
        )
        self.entry_reels_hint.grid(row=1, column=1, padx=(0, 10), pady=4, sticky="ew")

        self.chk_reels_use_gemma = ctk.CTkCheckBox(
            ai_cfg,
            text="🧠 Opravit nesmyslná slova přes lokální Gemma 4 12B QAT podle okolního kontextu",
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.chk_reels_use_gemma.select()
        self.chk_reels_use_gemma.grid(row=2, column=0, columnspan=2, padx=10, pady=(4, 8), sticky="w")

        # Tlačítka pro AI přepis a opravu
        ai_btns = ctk.CTkFrame(left_col, fg_color="transparent")
        ai_btns.grid(row=2, column=0, padx=10, pady=4, sticky="ew")
        ai_btns.grid_columnconfigure(0, weight=1)

        self.btn_reels_transcribe = ctk.CTkButton(
            ai_btns,
            text="🪄 1. Přepsat (Whisper Turbo) + Opravit (Gemma 4 12B)",
            height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#7c3aed",
            hover_color="#6d28d9",
            command=self._start_reels_transcribe_and_correct,
        )
        self.btn_reels_transcribe.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self.btn_reels_recorrect = ctk.CTkButton(
            ai_btns,
            text="🧠 Znovu opravit v Gemma 4",
            width=165,
            height=38,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self._start_reels_gemma_only,
        )
        self.btn_reels_recorrect.grid(row=0, column=1)

        # 3. Textový editor časovaných Reels bloků
        ed_frame = ctk.CTkFrame(left_col, corner_radius=8)
        ed_frame.grid(row=3, column=0, padx=10, pady=(4, 10), sticky="nsew")
        ed_frame.grid_columnconfigure(0, weight=1)
        ed_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            ed_frame,
            text="✏️ Časované Reels titulky (můžete libovolně ručně upravit před exportem):",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=0, padx=8, pady=(6, 2), sticky="w")

        self.txt_reels_editor = ctk.CTkTextbox(
            ed_frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.txt_reels_editor.grid(row=1, column=0, padx=8, pady=(2, 8), sticky="nsew")
        self.txt_reels_editor.insert(
            "1.0",
            "[00:00.20 -> 00:01.50] Ukázka dynamických titulků\n"
            "[00:01.50 -> 00:03.10] pro Reels a DaVinci Resolve!\n\n"
            "👉 Jak na to:\n"
            "1. Vyberte vlevo video nebo audio soubor (případně nastavte čas Od–Do).\n"
            "2. Klikněte na '🪄 1. Přepsat (Whisper Turbo) + Opravit (Gemma 4 12B)'.\n"
            "3. Zkontrolujte opravený přepis zde v editoru, vpravo si nastavte vzhled a klikněte na '🎬 2. Vygenerovat Reels Video Overlay'!",
        )

        # ==================== PRAVÁ ČÁST: STYL, NÁHLED & DAVINCI EXPORT ====================
        right_col = ctk.CTkFrame(tab, corner_radius=10)
        right_col.grid(row=0, column=1, padx=(5, 8), pady=6, sticky="nsew")
        right_col.grid_columnconfigure(0, weight=1)
        right_col.grid_rowconfigure(1, weight=1)

        # Živý náhled nahoře vpravo
        preview_box = ctk.CTkFrame(right_col, corner_radius=8, fg_color=("#0f172a", "#0f172a"))
        preview_box.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")
        preview_box.grid_columnconfigure(0, weight=1)

        self.lbl_reels_preview = ctk.CTkLabel(preview_box, text="")
        self.lbl_reels_preview.grid(row=0, column=0, padx=6, pady=6)

        # Scrollovací panel nastavení vzhledu a exportu
        style_scroll = ctk.CTkScrollableFrame(right_col, corner_radius=8)
        style_scroll.grid(row=1, column=0, padx=10, pady=4, sticky="nsew")
        style_scroll.grid_columnconfigure((0, 1), weight=1)

        # Řádek 1: Font & Velikost písma
        ctk.CTkLabel(style_scroll, text="Font (Písmo):", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, padx=8, pady=(6, 2), sticky="w"
        )
        ctk.CTkLabel(style_scroll, text="Velikost písma:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=1, padx=8, pady=(6, 2), sticky="w"
        )

        self.combo_reels_font = ctk.CTkOptionMenu(
            style_scroll,
            values=[
                "Arial Black",
                "Impact",
                "Segoe UI Bold",
                "Trebuchet MS",
                "Verdana",
                "Tahoma",
                "Franklin Gothic Medium",
                "Calibri",
                "Consolas",
                "Comic Sans MS",
            ],
            height=30,
            command=lambda _: self._update_reels_preview(),
        )
        self.combo_reels_font.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")

        self.combo_reels_fontsize = ctk.CTkOptionMenu(
            style_scroll,
            values=[
                "52 px (Menší)",
                "64 px (Střední)",
                "76 px (Doporučeno pro Reels)",
                "88 px (Velké)",
                "100 px (Extra výrazné)",
                "115 px (Obří Hormozi styl)",
            ],
            height=30,
            command=lambda _: self._update_reels_preview(),
        )
        self.combo_reels_fontsize.set("76 px (Doporučeno pro Reels)")
        self.combo_reels_fontsize.grid(row=1, column=1, padx=8, pady=(0, 8), sticky="ew")

        # Řádek 2: Hlavní barva textu & Barva zvýrazněného slova (Karaoke)
        ctk.CTkLabel(style_scroll, text="Hlavní barva textu:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=2, column=0, padx=8, pady=(4, 2), sticky="w"
        )
        ctk.CTkLabel(style_scroll, text="Barva aktivního slova (Karaoke):", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=2, column=1, padx=8, pady=(4, 2), sticky="w"
        )

        p_col_frame = ctk.CTkFrame(style_scroll, fg_color="transparent")
        p_col_frame.grid(row=3, column=0, padx=8, pady=(0, 8), sticky="ew")
        p_col_frame.grid_columnconfigure(0, weight=1)

        self.combo_reels_pcolor = ctk.CTkOptionMenu(
            p_col_frame,
            values=[
                "Bílá (#FFFFFF)",
                "Žlutá (#FFD700)",
                "Azurová (#00FFFF)",
                "Světle zelená (#4ADE80)",
                "Oranžová (#FF8800)",
                "Růžová (#F472B6)",
            ],
            height=30,
            command=self._on_preset_pcolor,
        )
        self.combo_reels_pcolor.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.btn_pick_pcolor = ctk.CTkButton(
            p_col_frame, text="🎨", width=34, height=30, command=lambda: self._pick_reels_color("primary")
        )
        self.btn_pick_pcolor.grid(row=0, column=1)

        h_col_frame = ctk.CTkFrame(style_scroll, fg_color="transparent")
        h_col_frame.grid(row=3, column=1, padx=8, pady=(0, 8), sticky="ew")
        h_col_frame.grid_columnconfigure(0, weight=1)

        self.combo_reels_hcolor = ctk.CTkOptionMenu(
            h_col_frame,
            values=[
                "Žlutá (#FFE600)",
                "Zářivě zelená (#00FF66)",
                "Azurová (#00E5FF)",
                "Červeno-růžová (#FF3366)",
                "Oranžová (#FF6600)",
                "Bílá (#FFFFFF)",
            ],
            height=30,
            command=self._on_preset_hcolor,
        )
        self.combo_reels_hcolor.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.btn_pick_hcolor = ctk.CTkButton(
            h_col_frame, text="🎨", width=34, height=30, command=lambda: self._pick_reels_color("highlight")
        )
        self.btn_pick_hcolor.grid(row=0, column=1)

        # Řádek 3: Obrys (Tloušťka + Barva) & Pozice na obrazovce
        ctk.CTkLabel(style_scroll, text="Tloušťka a barva obrysu:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=4, column=0, padx=8, pady=(4, 2), sticky="w"
        )
        ctk.CTkLabel(style_scroll, text="Pozice a výška titulků:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=4, column=1, padx=8, pady=(4, 2), sticky="w"
        )

        o_col_frame = ctk.CTkFrame(style_scroll, fg_color="transparent")
        o_col_frame.grid(row=5, column=0, padx=8, pady=(0, 8), sticky="ew")
        o_col_frame.grid_columnconfigure(0, weight=1)

        self.combo_reels_outline = ctk.CTkOptionMenu(
            o_col_frame,
            values=[
                "6 px (Výrazný Reels obrys)",
                "8 px (Extra silný obrys)",
                "4 px (Střední obrys)",
                "2 px (Jemný obrys)",
                "0 px (Bez obrysu)",
            ],
            height=30,
            command=lambda _: self._update_reels_preview(),
        )
        self.combo_reels_outline.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.btn_pick_ocolor = ctk.CTkButton(
            o_col_frame, text="🎨", width=34, height=30, command=lambda: self._pick_reels_color("outline")
        )
        self.btn_pick_ocolor.grid(row=0, column=1)

        pos_frame = ctk.CTkFrame(style_scroll, fg_color="transparent")
        pos_frame.grid(row=5, column=1, padx=8, pady=(0, 8), sticky="ew")
        pos_frame.grid_columnconfigure((0, 1), weight=1)

        self.combo_reels_pos = ctk.CTkOptionMenu(
            pos_frame,
            values=[
                "Dole (Reels standard)",
                "Uprostřed obrazovky (Center)",
                "Horní třetina (Top)",
            ],
            height=30,
            command=lambda _: self._update_reels_preview(),
        )
        self.combo_reels_pos.grid(row=0, column=0, padx=(0, 3), sticky="ew")

        self.combo_reels_marginv = ctk.CTkOptionMenu(
            pos_frame,
            values=[
                "260 px (Safe Zone)",
                "380 px (Výše)",
                "160 px (Níže)",
                "80 px (U kraje)",
            ],
            height=30,
            command=lambda _: self._update_reels_preview(),
        )
        self.combo_reels_marginv.grid(row=0, column=1, padx=(3, 0), sticky="ew")

        # Řádek 4: Efekty (Checkboxes)
        fx_frame = ctk.CTkFrame(style_scroll, corner_radius=8, fg_color=("#f1f5f9", "#1e293b"))
        fx_frame.grid(row=6, column=0, columnspan=2, padx=8, pady=4, sticky="ew")
        fx_frame.grid_columnconfigure((0, 1), weight=1)

        self.chk_reels_karaoke = ctk.CTkCheckBox(
            fx_frame,
            text="Karaoke zvýrazňování aktivního slova",
            font=ctk.CTkFont(size=11),
            command=self._update_reels_preview,
        )
        self.chk_reels_karaoke.select()
        self.chk_reels_karaoke.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="w")

        self.chk_reels_caps = ctk.CTkCheckBox(
            fx_frame,
            text="VELKÁ PÍSMENA (ALL CAPS)",
            font=ctk.CTkFont(size=11),
            command=self._update_reels_preview,
        )
        self.chk_reels_caps.select()
        self.chk_reels_caps.grid(row=0, column=1, padx=10, pady=(8, 4), sticky="w")

        self.chk_reels_pop = ctk.CTkCheckBox(
            fx_frame,
            text="Pop-in animace při nástupu titulku",
            font=ctk.CTkFont(size=11),
        )
        self.chk_reels_pop.select()
        self.chk_reels_pop.grid(row=1, column=0, padx=10, pady=(2, 8), sticky="w")

        self.chk_reels_box = ctk.CTkCheckBox(
            fx_frame,
            text="Tmavý obdélníkový box pod textem",
            font=ctk.CTkFont(size=11),
            command=self._update_reels_preview,
        )
        self.chk_reels_box.grid(row=1, column=1, padx=10, pady=(2, 8), sticky="w")

        # Řádek 5: Rozlišení videa a FPS
        ctk.CTkLabel(style_scroll, text="Cílové rozlišení a poměr stran:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=7, column=0, padx=8, pady=(8, 2), sticky="w"
        )
        ctk.CTkLabel(style_scroll, text="Snímková frekvence (FPS):", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=7, column=1, padx=8, pady=(8, 2), sticky="w"
        )

        self.combo_reels_res = ctk.CTkOptionMenu(
            style_scroll,
            values=[
                "9:16 Vertikální - 1080x1920 (Full HD Reels / TikTok / Shorts)",
                "9:16 Vertikální - 2160x3840 (4K Reels / Shorts)",
                "16:9 Horizontální - 1920x1080 (Full HD YouTube / Klasické)",
                "16:9 Horizontální - 3840x2160 (4K YouTube)",
                "1:1 Čtverec - 1080x1080 (Instagram Feed)",
                "4:5 Portrét - 1080x1350 (Instagram Portrait)",
                "Podle zdrojového videa (Zachovat původní rozlišení)",
            ],
            height=30,
            command=lambda _: self._update_reels_preview(),
        )
        self.combo_reels_res.grid(row=8, column=0, padx=8, pady=(0, 8), sticky="ew")

        self.combo_reels_fps = ctk.CTkOptionMenu(
            style_scroll,
            values=["30 fps (Standard)", "60 fps (Plynulý pohyb)", "25 fps (PAL)", "24 fps (Film)"],
            height=30,
        )
        self.combo_reels_fps.grid(row=8, column=1, padx=8, pady=(0, 8), sticky="ew")

        # Řádek 6: Režim exportu pro DaVinci Resolve / Přímé vypálení
        ctk.CTkLabel(
            style_scroll,
            text="🎬 Režim exportu (DaVinci Resolve Overlay nebo Přímé vypálení do videa):",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=9, column=0, columnspan=2, padx=8, pady=(4, 2), sticky="w")

        self.combo_reels_export_mode = ctk.CTkOptionMenu(
            style_scroll,
            values=[
                "DaVinci Resolve Overlay: Průhledné .MOV (Alpha kanál - vložte rovnou do stopy V2 nad video)",
                "DaVinci Resolve Overlay: Černé pozadí .MP4 (v DaVinci: Composite Mode -> Screen / Add)",
                "DaVinci Resolve Overlay: Zelené plátno .MP4 (Green Screen #00FF00 pro 3D Keyer)",
                "Přímé vypálení do videa: Hotové .MP4 s titulky přímo v obraze (Burn-in)",
                "Přímé vypálení do videa: Hotové .MOV s titulky přímo v obraze (Burn-in)",
                "Pouze titulkové soubory .ASS + .SRT (pro přímý import do Subtitle Tracku)",
            ],
            height=32,
        )
        self.combo_reels_export_mode.grid(row=10, column=0, columnspan=2, padx=8, pady=(0, 10), sticky="ew")

        # Hlavní tlačítko pro vygenerování Overlaye
        self.btn_reels_export = ctk.CTkButton(
            right_col,
            text="🎬 2. Vygenerovat Reels Video Overlay / Titulky",
            height=46,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#e11d48",
            hover_color="#be123c",
            command=self._start_reels_export,
        )
        self.btn_reels_export.grid(row=2, column=0, padx=10, pady=(4, 10), sticky="ew")

        # Vykreslit výchozí náhled
        self._update_reels_preview()

    # ------------------------------------------------------------------
    # ZÁLOŽKA 3: AI PŘEPIS (WHISPER) & BIONIC STUDIO
    # ------------------------------------------------------------------
    def _build_tab_ai(self):
        tab = self.tab_ai
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # Horní panel nastavení Whisperu
        whisper_bar = ctk.CTkFrame(tab, corner_radius=10)
        whisper_bar.grid(row=0, column=0, padx=10, pady=(6, 6), sticky="ew")
        whisper_bar.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkLabel(whisper_bar, text="AI Model (Bionic Studio / Lokální):", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, padx=10, pady=(8, 2), sticky="w"
        )
        self.combo_whisper_model = ctk.CTkOptionMenu(
            whisper_bar,
            values=[
                "Bionic Studio: whisper-large-v3-turbo (Lokální model - bez stahování)",
                "large-v3 (Maximální přesnost)",
                "medium (Střední velikost)",
                "small (Rychlý, menší nároky)",
                "base (Základní rychlý)",
            ],
        )
        self.combo_whisper_model.set(self.settings["whisper_model"])
        self.combo_whisper_model.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(whisper_bar, text="Jazyk audia:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=1, padx=10, pady=(8, 2), sticky="w"
        )
        self.combo_whisper_lang = ctk.CTkOptionMenu(
            whisper_bar,
            values=[
                "Auto (Automatická detekce)",
                "cs (Čeština)",
                "sk (Slovenština)",
                "en (Angličtina)",
                "de (Němčina)",
                "es (Španělština)",
                "fr (Francouzština)",
                "pl (Polština)",
                "uk (Ukrajinština)",
            ],
        )
        self.combo_whisper_lang.set(self.settings["whisper_lang"])
        self.combo_whisper_lang.grid(row=1, column=1, padx=10, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(whisper_bar, text="Výpočetní jednotka:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=2, padx=10, pady=(8, 2), sticky="w"
        )
        self.combo_whisper_device = ctk.CTkOptionMenu(
            whisper_bar,
            values=[
                "GPU - NVIDIA CUDA (RTX 5060 Ti 16GB FP16)",
                "CPU - AMD Ryzen 9600 (INT8)",
            ],
        )
        self.combo_whisper_device.set(self.settings["whisper_device"])
        self.combo_whisper_device.grid(row=1, column=2, padx=10, pady=(0, 10), sticky="ew")

        self.btn_transcribe = ctk.CTkButton(
            whisper_bar,
            text="🎙️ Spustit AI Přepis",
            height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#7c3aed",
            hover_color="#6d28d9",
            command=self._start_transcription,
        )
        self.btn_transcribe.grid(row=0, column=3, rowspan=2, padx=12, pady=10, sticky="ew")

        # Panel Bionic Studio / LM Studio integrace
        bionic_bar = ctk.CTkFrame(tab, corner_radius=10, fg_color=("#f8fafc", "#1e293b"))
        bionic_bar.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        bionic_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            bionic_bar,
            text="🧠 Bionic / LM Studio API:",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=(12, 6), pady=8, sticky="w")

        self.entry_bionic_url = ctk.CTkEntry(bionic_bar, width=210)
        self.entry_bionic_url.insert(0, self.settings["bionic_url"])
        self.entry_bionic_url.grid(row=0, column=1, padx=4, pady=8, sticky="w")

        self.lbl_bionic_status = ctk.CTkLabel(
            bionic_bar,
            text="🔄 Ověřuji Bionic Studio...",
            font=ctk.CTkFont(size=11),
            text_color=("gray30", "gray70"),
        )
        self.lbl_bionic_status.grid(row=0, column=2, padx=8, pady=8, sticky="w")

        btn_test_bionic = ctk.CTkButton(
            bionic_bar,
            text="🔄 Otestovat spojení",
            width=130,
            height=28,
            command=self._on_test_bionic,
        )
        btn_test_bionic.grid(row=0, column=3, padx=8, pady=8)

        # Řádek pro AI akce nad přepisem přes Bionic Studio
        ctk.CTkLabel(
            bionic_bar,
            text="AI Úkol:",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=1, column=0, padx=(12, 6), pady=(0, 8), sticky="w")

        self.combo_bionic_task = ctk.CTkOptionMenu(
            bionic_bar,
            values=[
                "Vytvoř strukturované shrnutí, hlavní myšlenky a klíčové body (v češtině)",
                "Uprav přepis do čtivého článku / poznámek a oprav gramatické chyby",
                "Vypiš ze záznamu úkoly (Action Items) a časovou osu témat",
                "Přelož celý přepis plynule do češtiny",
                "Přelož celý přepis plynule do angličtiny",
            ],
            width=390,
        )
        self.combo_bionic_task.grid(row=1, column=1, columnspan=2, padx=4, pady=(0, 8), sticky="ew")

        self.btn_send_bionic = ctk.CTkButton(
            bionic_bar,
            text="✨ Zpracovat v Bionic Studiu",
            width=180,
            height=30,
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self._start_bionic_processing,
        )
        self.btn_send_bionic.grid(row=1, column=3, padx=8, pady=(0, 8))

        # Hlavní textové pole s přepisem a výstupem
        text_container = ctk.CTkFrame(tab, corner_radius=10)
        text_container.grid(row=2, column=0, padx=10, pady=6, sticky="nsew")
        text_container.grid_columnconfigure(0, weight=1)
        text_container.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(text_container, fg_color="transparent")
        toolbar.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")

        self.chk_show_timestamps = ctk.CTkCheckBox(
            toolbar,
            text="Zobrazit časové značky [MM:SS]",
            font=ctk.CTkFont(size=12),
            command=self._refresh_transcript_view,
        )
        self.chk_show_timestamps.select()
        self.chk_show_timestamps.pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            toolbar,
            text="📋 Kopírovat text",
            width=120,
            height=28,
            command=self._copy_transcript,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            toolbar,
            text="💾 Uložit .TXT",
            width=105,
            height=28,
            fg_color="#059669",
            hover_color="#047857",
            command=lambda: self._export_transcript("txt"),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            toolbar,
            text="🎬 Uložit titulky .SRT",
            width=135,
            height=28,
            fg_color="#059669",
            hover_color="#047857",
            command=lambda: self._export_transcript("srt"),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            toolbar,
            text="🌐 Uložit .VTT",
            width=105,
            height=28,
            fg_color="#059669",
            hover_color="#047857",
            command=lambda: self._export_transcript("vtt"),
        ).pack(side="left", padx=4)

        self.txt_transcript = ctk.CTkTextbox(
            text_container,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="word",
        )
        self.txt_transcript.grid(row=1, column=0, padx=10, pady=(2, 10), sticky="nsew")
        self.txt_transcript.insert(
            "1.0",
            "Zde se v reálném čase zobrazí AI přepis audia nebo videa...\n\n"
            "Postup:\n"
            "1. Vyberte vlevo video (MP4, MOV...) nebo audio (MP3, AAC, WAV...).\n"
            "2. Klikněte na 'Spustit AI Přepis' – přepis proběhne lokálně na vaší RTX 5060 Ti.\n"
            "3. Výsledek můžete rovnou uložit jako .TXT, .SRT titulky, nebo ho jedním kliknutím poslat do Bionic Studia k analýze či shrnutí.",
        )

    # ------------------------------------------------------------------
    # ZÁLOŽKA 4: PODROBNÝ NÁVOD V APLIKACI
    # ------------------------------------------------------------------
    def _build_tab_help(self):
        tab = self.tab_help
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        help_box = ctk.CTkTextbox(tab, font=ctk.CTkFont(size=13), wrap="word")
        help_box.grid(row=0, column=0, padx=12, pady=12, sticky="nsew")

        guide_text = """====================================================================
📖 KOMPLETNÍ PRŮVODCE APLIKACÍ BIONIC MEDIA & AUDIO STUDIO
====================================================================

Tento program je navržen přímo pro vaši sestavu (AMD Ryzen 9600 + NVIDIA GeForce RTX 5060 Ti 16GB VRAM), aby veškeré operace probíhaly 100% lokálně, soukromě a maximální rychlostí.

--------------------------------------------------------------------
1. JAK NAHRÁT SOUBORY A NASTAVIT VÝSTUP
--------------------------------------------------------------------
• Přetažení myší (Drag & Drop): Stačí chytit libovolné soubory (MP4, MOV, MKV, AVI, WEBM, AAC, MP3, WAV, FLAC, M4A, OGG) v Průzkumníku Windows a přetáhnout je do modrého pole vlevo nahoře.
• Tlačítko „➕ Vybrat soubory“: Otevře klasické okno pro výběr jednoho či více souborů najednou.
• Kliknutím na soubor ve frontě vlevo zobrazíte jeho technické parametry (délku, velikost, kodeky obrazu a zvuku).
• Výstupní složka: Pokud necháte pole výstupní složky prázdné, vyexportované soubory se uloží přímo vedle původního videa/audia.

--------------------------------------------------------------------
2. EXTRAKCE AUDIA Z VIDEA A KONVERZE AUDIO FORMÁTŮ (AAC ⇄ MP3 ⇄ WAV...)
--------------------------------------------------------------------
• Přejděte na záložku „🎵 Extrakce & Konverze Audia“.
• Do fronty můžete vložit jak videa (MP4, MOV, MKV...), tak zvukové soubory (AAC, MP3, WAV, FLAC...).
• Zvolte cílový formát:
   - MP3: Nejrozšířenější formát, kompatibilní všude.
   - AAC / M4A: Vyšší kvalita než MP3 při stejném datovém toku.
   - WAV / FLAC: Bezztrátové studiové formáty bez komprese kvality.
• Nastavte Bitrate (např. 192k nebo 320k pro maximální kvalitu), vzorkovací frekvenci (48000 Hz / 44100 Hz) a volitelně zapněte vyrovnání hlasitosti (EBU R128).
• Klikněte na „🎵 Extrahovat / Převést Audio“.

--------------------------------------------------------------------
3. KONVERZE VIDEA (MP4 ⇄ MOV ⇄ MKV ⇄ WEBM ⇄ AVI)
--------------------------------------------------------------------
• Přejděte na záložku „🎬 Konverze Videa (MP4 ⇄ MOV)“.
• Zvolte cílový formát (např. MP4 nebo MOV).
• Režimy převodu:
   1) Stream Copy (Bleskový převod bez rekomprese):
      Pouze přebalí obraz a zvuk do nového kontejneru (např. z MOV do MP4 nebo z MP4 do MOV). Trvá jen pár sekund a zachová 100% původní kvalitu.
   2) GPU: NVIDIA NVENC H.264 / HEVC (RTX 5060 Ti):
      Využije dedikovaný čip na vaší grafické kartě RTX 5060 Ti. Ideální, pokud potřebujete změnit rozlišení (např. na 1080p/720p) nebo zmenšit velikost souboru.
   3) CPU: x264 (Ryzen 9600):
      Klasické softwarové kódování přes procesor.

--------------------------------------------------------------------
4. LOKÁLNÍ AI PŘEPIS AUDIA (PŘÍMO Z BIONIC STUDIA NA RTX 5060 Ti)
--------------------------------------------------------------------
• Přejděte na záložku „🤖 AI Přepis & Bionic Studio“.
• Vyberte vlevo soubor, který chcete přepsat (funguje přímo na videa MP4/MOV i na audio MP3/AAC/WAV – program si zvuk extrahuje automaticky na pozadí).
• Výchozí model: „Bionic Studio: whisper-large-v3-turbo (Lokální model - bez stahování)“ – aplikace automaticky načte váš již stažený model přímo z úložiště Bionic Studia (`~/.lmstudio/models/handy-computer/whisper-large-v3-turbo-gguf/whisper-large-v3-turbo-Q8_0.gguf`) a spustí ho v přesnosti FP16 na vaší RTX 5060 Ti bez jakéhokoliv stahování z internetu!
• Po dokončení můžete přepis uložit jako čistý text (.TXT) nebo jako časované titulky (.SRT / .VTT) přímo do videa.

--------------------------------------------------------------------
5. PROPOJENÍ S BIONIC STUDIEM / LM STUDIEM
--------------------------------------------------------------------
Jak propojit aplikaci s Bionic / LM Studiem:
1. Otevřete Bionic / LM Studio (aplikace automaticky detekuje běžící port, např. `http://localhost:1235/v1` nebo `1234`).
2. Načtěte svůj oblíbený lokální LLM model (např. Gemma-4-12B, Qwen-3.8).
3. V naší aplikaci klikněte na „🔄 Otestovat spojení“ – rozsvítí se zelený stav „Připojeno“.
4. Vyberte požadovaný úkol (Shrnutí, Článek, Akční body, Překlad) a klikněte na „✨ Zpracovat v Bionic Studiu“.
--------------------------------------------------------------------
6. 🔥 REELS TITULKY, OPRAVA PŘES GEMMA 4 12B A DAVINCI RESOLVE OVERLAY
--------------------------------------------------------------------
Záložka „🔥 Reels Titulky & Overlay (DaVinci)“ propojuje lokální model Whisper Turbo, jazykový model Gemma 4 12B QAT a generátor dynamických titulků ve stylu Reels / TikTok / Shorts:

KROK 1: Časový výřez a dvoustupňový AI přepis (Whisper ➔ Gemma 4 12B QAT)
• Chcete-li otitulkovat jen určitou část videa, zadejte vlevo nahoře čas „Od“ (např. `00:15`) a „Do“ (např. `00:55`).
  - Režim „Začít od 00:00 (Oříznutý klip)“ přečísluje vyexportovaný overlay tak, aby začínal od nuly.
  - Režim „Původní čas videa (Celá stopa)“ zachová původní časovou osu videa, takže overlay sedne přesně na danou minutu a sekundu v celém videu.
• Volitelně napište do pole „Kontext / Téma“ pár klíčových slov (např. názvy produktů, her nebo odborné pojmy).
• Klikněte na „🪄 1. Přepsat (Whisper Turbo) + Opravit (Gemma 4 12B)“:
  1) Whisper Turbo nejprve na RTX 5060 Ti vytvoří přepis s přesným časováním každého jednotlivého slova (word-level timestamps) a rozdělí ho na krátké dynamické bloky (např. po 2–3 slovech).
  2) Celý přepis se následně odešle do vašeho lokálního modelu `google/gemma-4-12b-qat` v Bionic Studiu, který podle okolního kontextu opraví přeslechnutá nebo nesmyslná slova při zachování přesného časování.
• V textovém editoru vlevo dole pak můžete jakýkoliv řádek ještě ručně doladit.

KROK 2: Vzhled titulků a živý náhled
• Vpravo nahoře vidíte živý náhled titulku v reálném čase.
• Můžete měnit:
  - Font (Arial Black, Impact, Segoe UI Bold, Trebuchet MS, Verdana, Tahoma...), velikost písma, tloušťku a barvu obrysu.
  - Hlavní barvu textu a barvu právě vyslovovaného slova (Karaoke efekt) včetně vlastního kapátka/palety barev (tlačítko 🎨).
  - Pozici na obrazovce (Dole, Uprostřed, Nahoře), výšku od okraje, VELKÁ PÍSMENA (ALL CAPS), Pop-in animaci při nástupu titulku nebo tmavý podkladový box.
  - Cílové rozlišení a poměr stran: 9:16 Vertikální (1080×1920 nebo 4K 2160×3840 pro Reels/Shorts/TikTok), 16:9 Horizontální (1920×1080 nebo 4K pro YouTube), 1:1 Čtverec, 4:5 Portrét nebo Původní rozlišení zdrojového videa.

KROK 3: Jak vložit vygenerované titulky do DaVinci Resolve (3 snadné způsoby)
Klikněte na „🎬 2. Vygenerovat Reels Video Overlay / Titulky“. Podle zvoleného režimu exportu postupujte v DaVinci Resolve takto:

  🅰️ ZPŮSOB 1 (DOPORUČENO – Nejjednodušší a 100% kvalita):
     Režim: „DaVinci Resolve Overlay: Průhledné .MOV (Alpha kanál)“
     1. V DaVinci Resolve otevřete záložku Edit.
     2. Na stopu Video 1 (V1) vložte své původní video.
     3. Vygenerovaný soubor `..._reels_overlay_alpha.mov` přetáhněte myší přímo nad něj do stopy Video 2 (V2).
     4. Hotovo! Soubor obsahuje přímý průhledný Alfa kanál (QuickTime Animation ARGB), takže titulky včetně animací, obrysů, stínů a Karaoke zvýraznění okamžitě visí nad původním videem bez nutnosti jakéhokoliv klíčování.

  🅱️ ZPŮSOB 2 (Extrémně malý soubor přes GPU NVENC):
     Režim: „DaVinci Resolve Overlay: Černé pozadí .MP4“
     1. Vložte vygenerovaný `.mp4` soubor v DaVinci Resolve do stopy Video 2 (V2) nad původní video.
     2. Označte klip ve stopě V2, vpravo nahoře otevřete „Inspector“ -> sekce „Composite“ a změňte „Composite Mode“ z Normal na „Screen“ nebo „Add“. Černé pozadí zmizí a zůstanou jen titulky.

  🅲 ZPŮSOB 3 (Zelené plátno / Green Screen nebo přímé vypálení):
     • Při volbě „Zelené plátno .MP4 (#00FF00)“ vložte klip do V2 a v záložce Edit / Fusion použijte efekt „3D Keyer“ (klikněte kapátkem na zelenou).
     • Při volbě „Přímé vypálení do videa (Burn-in)“ program rovnou vyrenderuje kompletní hotové video s titulky vypálenými přímo do obrazu (nepotřebujete vůbec otevírat střihový program).
     • Současně se vždy vedle videa automaticky uloží i soubory `.ASS` a `.SRT`, které můžete v DaVinci Resolve naimportovat přes `File -> Import -> Subtitle`.
"""
        help_box.insert("1.0", guide_text)
        help_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # DRAG & DROP A SPRÁVA SOUBORŮ
    # ------------------------------------------------------------------
    def _setup_drag_and_drop(self):
        if not HAS_DND:
            return
        for widget in (self, self.drop_frame, self.file_list_frame):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop_files)
            except Exception:
                pass

    def _on_drop_files(self, event):
        raw = event.data
        if not raw:
            return
        paths = self.tk.splitlist(raw)
        self._add_file_paths(paths)

    def _on_add_files(self):
        filetypes = [
            ("Všechna podporovaná média", "*.mp4 *.mov *.mkv *.avi *.webm *.flv *.wmv *.m4v *.mp3 *.aac *.m4a *.wav *.flac *.ogg *.opus"),
            ("Video soubory", "*.mp4 *.mov *.mkv *.avi *.webm *.flv *.wmv *.m4v"),
            ("Audio soubory", "*.mp3 *.aac *.m4a *.wav *.flac *.ogg *.opus"),
            ("Všechny soubory", "*.*"),
        ]
        paths = filedialog.askopenfilenames(title="Vyberte video nebo audio soubory", filetypes=filetypes)
        if paths:
            self._add_file_paths(paths)

    def _add_file_paths(self, paths):
        added = 0
        for p in paths:
            clean_p = p.strip("{}").strip('"')
            if os.path.isdir(clean_p):
                for root, _, files in os.walk(clean_p):
                    for f in files:
                        if Path(f).suffix.lower() in ALL_SUPPORTED_EXTS:
                            full = os.path.join(root, f)
                            if full not in self.files:
                                self.files.append(full)
                                added += 1
            elif os.path.isfile(clean_p):
                if clean_p not in self.files:
                    self.files.append(clean_p)
                    added += 1

        if added > 0:
            if not self.selected_file and self.files:
                self.selected_file = self.files[0]
            self._rebuild_file_list()
            self._update_selected_file_info()
            self.lbl_status.configure(text=f"Přidáno {added} souborů. Celkem ve frontě: {len(self.files)}.")

    def _on_clear_files(self):
        if self.is_busy:
            return
        self.files.clear()
        self.selected_file = None
        self._rebuild_file_list()
        self.lbl_file_info.configure(text="Žádný soubor není vybrán.\nPřidejte video nebo audio výše.")
        self.lbl_status.configure(text="Seznam souborů byl vyčištěn.")

    def _rebuild_file_list(self):
        for child in self.file_list_frame.winfo_children():
            child.destroy()

        self.lbl_queue_count.configure(text=f"Fronta souborů ({len(self.files)}):")

        for idx, fpath in enumerate(self.files):
            is_sel = fpath == self.selected_file
            ext = Path(fpath).suffix.lower()
            icon = "🎬" if ext in SUPPORTED_VIDEO_EXTS else "🎵"
            bg = ("#bfdbfe", "#1d4ed8") if is_sel else ("#e2e8f0", "#334155")

            row_f = ctk.CTkFrame(self.file_list_frame, corner_radius=6, fg_color=bg)
            row_f.grid(row=idx, column=0, padx=4, pady=3, sticky="ew")
            row_f.grid_columnconfigure(0, weight=1)

            name_btn = ctk.CTkButton(
                row_f,
                text=f"{icon} { os.path.basename(fpath) }",
                anchor="w",
                fg_color="transparent",
                text_color=("black", "white"),
                hover_color=("#93c5fd", "#2563eb"),
                height=28,
                command=lambda p=fpath: self._select_file(p),
            )
            name_btn.grid(row=0, column=0, padx=4, pady=2, sticky="ew")

            del_btn = ctk.CTkButton(
                row_f,
                text="✕",
                width=26,
                height=26,
                fg_color="transparent",
                hover_color="#dc2626",
                command=lambda p=fpath: self._remove_single_file(p),
            )
            del_btn.grid(row=0, column=1, padx=4, pady=2)

    def _select_file(self, fpath: str):
        self.selected_file = fpath
        self._rebuild_file_list()
        self._update_selected_file_info()

    def _remove_single_file(self, fpath: str):
        if self.is_busy:
            return
        if fpath in self.files:
            self.files.remove(fpath)
        if self.selected_file == fpath:
            self.selected_file = self.files[0] if self.files else None
        self._rebuild_file_list()
        self._update_selected_file_info()

    def _update_selected_file_info(self):
        if not self.selected_file or not os.path.exists(self.selected_file):
            self.lbl_file_info.configure(text="Žádný soubor není vybrán.")
            return

        fpath = self.selected_file
        self.lbl_file_info.configure(text=f"Načítám informace o {os.path.basename(fpath)}...")

        def _worker():
            info = probe_media_info(fpath)
            self.selected_file_info = info
            txt = (
                f"📄 {os.path.basename(fpath)}\n"
                f"⏱️ Délka: {info['duration_str']}  |  📦 Velikost: {info['size_mb']} MB\n"
                f"🎞️ Video: {info['video_info']}\n"
                f"🔊 Audio: {info['audio_info']}"
            )
            self.after(0, lambda: self.lbl_file_info.configure(text=txt))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_pick_output_dir(self):
        d = filedialog.askdirectory(title="Vyberte výstupní složku")
        if d:
            self.output_dir = d
            self.entry_out_dir.delete(0, "end")
            self.entry_out_dir.insert(0, d)
            self._save_settings()

    def _on_open_output_dir(self):
        target = self.entry_out_dir.get().strip()
        if not target and self.selected_file:
            target = os.path.dirname(self.selected_file)
        if not target or not os.path.isdir(target):
            target = os.path.dirname(os.path.abspath(__file__))
        os.startfile(target)

    def _resolve_output_path(self, input_path: str, new_ext: str, suffix_tag: str = "") -> str:
        custom_dir = self.entry_out_dir.get().strip()
        out_folder = custom_dir if (custom_dir and os.path.isdir(custom_dir)) else os.path.dirname(input_path)
        stem = Path(input_path).stem
        candidate = os.path.join(out_folder, f"{stem}{suffix_tag}.{new_ext.lstrip('.')}")
        # Zabránit přepsání vstupního souboru pokud se shoduje cesta
        if os.path.abspath(candidate).lower() == os.path.abspath(input_path).lower():
            candidate = os.path.join(out_folder, f"{stem}_converted.{new_ext.lstrip('.')}")
        return candidate

    def _set_busy(self, busy: bool, status_msg: str = ""):
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        self.btn_run_audio.configure(state=state)
        self.btn_run_video.configure(state=state)
        self.btn_run_yt.configure(state=state)
        self.btn_probe_yt.configure(state=state)
        self.btn_reels_transcribe.configure(state=state)
        self.btn_reels_recorrect.configure(state=state)
        self.btn_reels_export.configure(state=state)
        self.btn_transcribe.configure(state=state)
        self.btn_send_bionic.configure(state=state)
        self.btn_add_files.configure(state=state)
        self.btn_clear_files.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if status_msg:
            self.lbl_status.configure(text=status_msg)

    def _on_cancel_task(self):
        if self.is_busy:
            self.cancel_event.set()
            self.lbl_status.configure(text="Ruším probíhající operaci...")

    # ------------------------------------------------------------------
    # AKCE 1: EXTRAKCE A KONVERZE AUDIA
    # ------------------------------------------------------------------
    def _start_audio_conversion(self):
        if not self.files:
            messagebox.showwarning("Žádné soubory", "Nejprve prosím přidejte alespoň jeden video nebo audio soubor do fronty vlevo.")
            return

        target_fmt = self.combo_audio_fmt.get().split(" ")[0].lower()
        bitrate = self.combo_audio_bitrate.get().split(" ")[0]
        sr = self.combo_audio_sr.get().split(" (")[0]
        ch = self.combo_audio_ch.get()
        norm = bool(self.chk_audio_norm.get())

        self.cancel_event.clear()
        self._set_busy(True, "Zahajuji extrakci / konverzi audia...")
        self.progress_bar.set(0.0)

        files_queue = list(self.files)

        def _worker():
            success_count = 0
            errors = []
            total_files = len(files_queue)

            for idx, fpath in enumerate(files_queue, start=1):
                if self.cancel_event.is_set():
                    break

                fname = os.path.basename(fpath)
                info = probe_media_info(fpath)
                dur = info.get("duration_sec", 0.0)
                out_path = self._resolve_output_path(fpath, target_fmt)

                cmd = build_audio_command(
                    input_path=fpath,
                    output_path=out_path,
                    target_format=target_fmt,
                    bitrate=bitrate,
                    sample_rate=sr,
                    channels=ch,
                    normalize=norm,
                )

                def _prog(frac: float, msg: str, i=idx, fn=fname):
                    overall = ((i - 1) + frac) / total_files
                    self.after(0, lambda: self.progress_bar.set(overall))
                    self.after(0, lambda: self.lbl_status.configure(text=f"[{i}/{total_files}] {fn} — {msg}"))

                ok, err_msg = run_ffmpeg_with_progress(cmd, dur, _prog, self.cancel_event)
                if ok:
                    success_count += 1
                else:
                    errors.append(f"{fname}: {err_msg}")

            def _finish():
                self.progress_bar.set(1.0 if success_count == total_files else 0.0)
                if self.cancel_event.is_set():
                    self._set_busy(False, f"Operace zrušena. Dokončeno {success_count}/{total_files} souborů.")
                elif errors:
                    self._set_busy(False, f"Hotovo s chybami ({success_count}/{total_files} úspěšně).")
                    messagebox.showerror("Chyba při konverzi", "\n\n".join(errors[:3]))
                else:
                    self._set_busy(False, f"✅ Úspěšně převedeno {success_count} audio souborů do formátu .{target_fmt.upper()}!")
                    messagebox.showinfo(
                        "Hotovo",
                        f"Úspěšně vyexportováno {success_count} souborů do formátu .{target_fmt.upper()}!",
                    )

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # AKCE 2: KONVERZE VIDEA (MP4 <-> MOV <-> MKV...)
    # ------------------------------------------------------------------
    def _start_video_conversion(self):
        if not self.files:
            messagebox.showwarning("Žádné soubory", "Nejprve prosím přidejte video soubory do fronty vlevo.")
            return

        target_fmt = self.combo_video_fmt.get().split(" ")[0].lower()
        mode = self.combo_video_mode.get()
        quality = self.combo_video_quality.get()
        res = self.combo_video_res.get()

        self.cancel_event.clear()
        self._set_busy(True, "Zahajuji konverzi videa...")
        self.progress_bar.set(0.0)

        files_queue = list(self.files)

        def _worker():
            success_count = 0
            errors = []
            total_files = len(files_queue)

            for idx, fpath in enumerate(files_queue, start=1):
                if self.cancel_event.is_set():
                    break

                fname = os.path.basename(fpath)
                info = probe_media_info(fpath)
                dur = info.get("duration_sec", 0.0)
                out_path = self._resolve_output_path(fpath, target_fmt)

                cmd = build_video_command(
                    input_path=fpath,
                    output_path=out_path,
                    target_format=target_fmt,
                    mode=mode,
                    resolution=res,
                    quality_preset=quality,
                )

                def _prog(frac: float, msg: str, i=idx, fn=fname):
                    overall = ((i - 1) + frac) / total_files
                    self.after(0, lambda: self.progress_bar.set(overall))
                    self.after(0, lambda: self.lbl_status.configure(text=f"[{i}/{total_files}] {fn} — {msg}"))

                ok, err_msg = run_ffmpeg_with_progress(cmd, dur, _prog, self.cancel_event)
                if ok:
                    success_count += 1
                else:
                    errors.append(f"{fname}: {err_msg}")

            def _finish():
                self.progress_bar.set(1.0 if success_count == total_files else 0.0)
                if self.cancel_event.is_set():
                    self._set_busy(False, f"Operace zrušena. Dokončeno {success_count}/{total_files} souborů.")
                elif errors:
                    self._set_busy(False, f"Hotovo s chybami ({success_count}/{total_files} úspěšně).")
                    messagebox.showerror("Chyba při konverzi videa", "\n\n".join(errors[:3]))
                else:
                    self._set_busy(False, f"✅ Úspěšně převedeno {success_count} videí do formátu .{target_fmt.upper()}!")
                    messagebox.showinfo(
                        "Hotovo",
                        f"Úspěšně převedeno {success_count} videí do formátu .{target_fmt.upper()}!",
                    )

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # AKCE 2.5: STAHOVÁNÍ Z YOUTUBE (MP4 / MOV / AUDIO)
    # ------------------------------------------------------------------
    def _on_paste_yt_url(self):
        try:
            clip = self.clipboard_get().strip()
            if clip:
                self.entry_yt_url.delete(0, "end")
                self.entry_yt_url.insert(0, clip)
                self._on_probe_yt_url()
        except Exception:
            pass

    def _on_probe_yt_url(self):
        url = self.entry_yt_url.get().strip()
        if not url:
            messagebox.showwarning("Chybí URL", "Zadejte nebo vložte prosím odkaz na YouTube video.")
            return

        self.lbl_yt_info.configure(text="🔄 Načítám informace o YouTube videu...", text_color=("gray30", "gray70"))

        def _worker():
            try:
                info = fetch_youtube_info(url)
                res_list = ", ".join(f"{h}p" for h in info["heights"][:6]) if info["heights"] else "Standardní"
                txt = (
                    f"🎬 Název: {info['title']}\n"
                    f"👤 Kanál: {info['uploader']}   |   ⏱️ Délka: {info['duration_str']}   |   🎞️ Dostupná rozlišení: {res_list}"
                )
                self.after(0, lambda: self.lbl_yt_info.configure(text=txt, text_color=("#15803d", "#4ade80")))
            except Exception as e:
                self.after(0, lambda err=str(e): self.lbl_yt_info.configure(
                    text=f"❌ Nepodařilo se načíst info o videu: {err}",
                    text_color=("#b91c1c", "#f87171"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _get_youtube_download_folder(self) -> str:
        custom_dir = self.entry_out_dir.get().strip()
        if custom_dir and os.path.isdir(custom_dir):
            return custom_dir
        default_dl = os.path.join(str(Path.home()), "Downloads")
        if os.path.isdir(default_dl):
            return default_dl
        out_fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        os.makedirs(out_fallback, exist_ok=True)
        return out_fallback

    def _start_youtube_download(self):
        url = self.entry_yt_url.get().strip()
        if not url:
            messagebox.showwarning("Chybí odkaz", "Nejprve prosím vložte odkaz na YouTube video.")
            return

        target_fmt = self.combo_yt_fmt.get()
        max_res = self.combo_yt_res.get()
        auto_queue = bool(self.chk_yt_auto_queue.get())
        out_folder = self._get_youtube_download_folder()

        self.cancel_event.clear()
        self._set_busy(True, "Připravuji stahování z YouTube...")
        self.progress_bar.set(0.02)

        def _on_prog(frac: float, msg: str):
            self.after(0, lambda: self.progress_bar.set(frac))
            self.after(0, lambda: self.lbl_status.configure(text=msg))

        def _worker():
            try:
                saved_file = download_youtube_media(
                    url=url,
                    output_dir=out_folder,
                    target_format=target_fmt,
                    max_resolution=max_res,
                    progress_callback=_on_prog,
                    cancel_event=self.cancel_event,
                )

                def _done():
                    self.progress_bar.set(1.0)
                    if auto_queue and os.path.isfile(saved_file):
                        if saved_file not in self.files:
                            self.files.append(saved_file)
                        self.selected_file = saved_file
                        self._rebuild_file_list()
                        self._update_selected_file_info()
                    self._set_busy(False, f"✅ Staženo z YouTube: {os.path.basename(saved_file)} (složka: {out_folder})")
                    messagebox.showinfo(
                        "YouTube stahování dokončeno",
                        f"Soubor byl úspěšně stažen a uložen:\n{saved_file}",
                    )

                self.after(0, _done)
            except Exception as e:
                def _err(err_msg=str(e)):
                    self.progress_bar.set(0.0)
                    self._set_busy(False, f"❌ Chyba při stahování z YouTube: {err_msg}")
                    if not self.cancel_event.is_set():
                        messagebox.showerror("Chyba stahování z YouTube", err_msg)

                self.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # AKCE 3: AI PŘEPIS AUDIA (FASTER-WHISPER)
    # ------------------------------------------------------------------
    def _start_transcription(self):
        target_file = self.selected_file or (self.files[0] if self.files else None)
        if not target_file or not os.path.exists(target_file):
            messagebox.showwarning("Žádný soubor", "Nejprve prosím vyberte vlevo soubor, který chcete přepsat pomocí AI.")
            return

        self._save_settings()
        model_size = self.combo_whisper_model.get()
        lang = self.combo_whisper_lang.get()
        device_pref = self.combo_whisper_device.get()

        self.cancel_event.clear()
        self._set_busy(True, f"Připravuji AI přepis pro {os.path.basename(target_file)}...")
        self.progress_bar.set(0.02)

        self.txt_transcript.delete("1.0", "end")
        show_ts = bool(self.chk_show_timestamps.get())

        def _on_segment(seg: dict):
            line = f"{format_timestamp_short(seg['start'])} {seg['text']}\n" if show_ts else f"{seg['text']} "
            self.after(0, lambda l=line: self._append_transcript_live(l))

        def _on_prog(frac: float, msg: str):
            self.after(0, lambda: self.progress_bar.set(frac))
            self.after(0, lambda: self.lbl_status.configure(text=msg))

        def _worker():
            try:
                res = self.transcriber.transcribe(
                    media_path=target_file,
                    model_size=model_size,
                    language=lang,
                    device_preference=device_pref,
                    progress_callback=_on_prog,
                    segment_callback=_on_segment,
                    cancel_event=self.cancel_event,
                )
                self.last_transcription = res
                self.after(0, lambda: self._set_busy(
                    False,
                    f"✅ AI Přepis dokončen ({len(res.segments)} segmentů, jazyk: {res.detected_language.upper()})."
                ))
            except Exception as e:
                self.after(0, lambda: self._set_busy(False, f"❌ Chyba přepisu: {str(e)}"))
                if not self.cancel_event.is_set():
                    self.after(0, lambda err=str(e): messagebox.showerror("Chyba při AI přepisu", err))

        threading.Thread(target=_worker, daemon=True).start()

    def _append_transcript_live(self, text: str):
        self.txt_transcript.insert("end", text)
        self.txt_transcript.see("end")

    def _refresh_transcript_view(self):
        if not self.last_transcription:
            return
        show_ts = bool(self.chk_show_timestamps.get())
        self.txt_transcript.delete("1.0", "end")
        self.txt_transcript.insert("1.0", self.last_transcription.to_plain_text(include_timestamps=show_ts))

    def _copy_transcript(self):
        content = self.txt_transcript.get("1.0", "end").strip()
        if not content:
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.lbl_status.configure(text="📋 Text byl zkopírován do schránky.")

    def _export_transcript(self, fmt: str):
        if not self.last_transcription and fmt in {"srt", "vtt"}:
            messagebox.showwarning("Chybí časová data", "Pro export titulků (.SRT / .VTT) nejprve spusťte AI přepis.")
            return

        default_name = Path(self.selected_file).stem if self.selected_file else "prepis"
        save_path = filedialog.asksaveasfilename(
            title=f"Uložit přepis jako .{fmt.upper()}",
            initialfile=f"{default_name}.{fmt}",
            defaultextension=f".{fmt}",
            filetypes=[(f"Soubor {fmt.upper()}", f"*.{fmt}"), ("Všechny soubory", "*.*")],
        )
        if not save_path:
            return

        if fmt == "srt" and self.last_transcription:
            data = self.last_transcription.to_srt()
        elif fmt == "vtt" and self.last_transcription:
            data = self.last_transcription.to_vtt()
        else:
            data = self.txt_transcript.get("1.0", "end").strip()

        with open(save_path, "w", encoding="utf-8") as f:
            f.write(data)
        self.lbl_status.configure(text=f"💾 Přepis uložen do: {save_path}")

    # ------------------------------------------------------------------
    # AKCE 4: BIONIC STUDIO / LM STUDIO INTEGRACE
    # ------------------------------------------------------------------
    def _refresh_bionic_status_bg(self):
        ok, msg, _ = self.bionic_client.check_connection()
        color = ("#15803d", "#4ade80") if ok else ("#b91c1c", "#f87171")
        prefix = "🟢 " if ok else "🔴 "
        def _upd():
            self.lbl_bionic_status.configure(text=prefix + msg, text_color=color)
            if ok and self.entry_bionic_url.get().strip() != self.bionic_client.base_url:
                self.entry_bionic_url.delete(0, "end")
                self.entry_bionic_url.insert(0, self.bionic_client.base_url)
        self.after(0, _upd)

    def _on_test_bionic(self):
        url = self.entry_bionic_url.get().strip()
        self.bionic_client.base_url = url.rstrip("/")
        self._save_settings()
        self.lbl_bionic_status.configure(text="🔄 Testuji spojení...", text_color=("gray30", "gray70"))
        threading.Thread(target=self._refresh_bionic_status_bg, daemon=True).start()

    def _start_bionic_processing(self):
        raw_text = self.txt_transcript.get("1.0", "end").strip()
        if not raw_text or raw_text.startswith("Zde se v reálném čase zobrazí"):
            messagebox.showwarning("Prázdný text", "Nejprve prosím vytvořte AI přepis audia nebo vložte text do pole.")
            return

        url = self.entry_bionic_url.get().strip()
        self.bionic_client.base_url = url.rstrip("/")
        self._save_settings()

        task_prompt = self.combo_bionic_task.get()
        self._set_busy(True, "🧠 Odesílám přepis do Bionic Studia ke zpracování...")
        self.progress_bar.set(0.3)

        def _worker():
            ok, result_text = self.bionic_client.process_transcript(raw_text, task_prompt)

            def _done():
                self.progress_bar.set(1.0 if ok else 0.0)
                if ok:
                    sep = "\n\n" + "=" * 65 + "\n🧠 VÝSTUP Z BIONIC STUDIA:\n" + "=" * 65 + "\n\n"
                    self.txt_transcript.insert("end", sep + result_text + "\n")
                    self.txt_transcript.see("end")
                    self._set_busy(False, "✅ Bionic Studio úspěšně zpracovalo přepis!")
                else:
                    self._set_busy(False, "❌ Nelze zpracovat v Bionic Studiu.")
                    messagebox.showerror("Bionic Studio nedostupné", result_text)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # AKCE 5: REELS TITULKY, GEMMA 4 12B KONTEXTOVÁ OPRAVA & DAVINCI OVERLAY
    # ------------------------------------------------------------------
    def _on_preset_pcolor(self, choice: str):
        import re
        m = re.search(r"#([0-9A-Fa-f]{6})", choice)
        if m:
            self.reels_primary_hex = "#" + m.group(1).upper()
            self._update_reels_preview()

    def _on_preset_hcolor(self, choice: str):
        import re
        m = re.search(r"#([0-9A-Fa-f]{6})", choice)
        if m:
            self.reels_highlight_hex = "#" + m.group(1).upper()
            self._update_reels_preview()

    def _pick_reels_color(self, target: str):
        initial = (
            self.reels_primary_hex if target == "primary"
            else (self.reels_highlight_hex if target == "highlight" else self.reels_outline_hex)
        )
        rgb, hex_val = colorchooser.askcolor(color=initial, title="Vyberte barvu pro Reels titulky")
        if hex_val:
            hex_clean = hex_val.upper()
            if target == "primary":
                self.reels_primary_hex = hex_clean
                self.combo_reels_pcolor.set(f"Vlastní ({hex_clean})")
            elif target == "highlight":
                self.reels_highlight_hex = hex_clean
                self.combo_reels_hcolor.set(f"Vlastní ({hex_clean})")
            else:
                self.reels_outline_hex = hex_clean
            self._update_reels_preview()

    def _resolve_reels_resolution(self) -> tuple[int, int]:
        res_choice = self.combo_reels_res.get()
        if "2160x3840" in res_choice:
            return 2160, 3840
        if "1920x1080" in res_choice:
            return 1920, 1080
        if "3840x2160" in res_choice:
            return 3840, 2160
        if "1080x1080" in res_choice:
            return 1080, 1080
        if "1080x1350" in res_choice:
            return 1080, 1350
        if "Podle zdrojového" in res_choice and self.selected_file_info:
            w = int(self.selected_file_info.get("width") or 1080)
            h = int(self.selected_file_info.get("height") or 1920)
            return w, h
        return 1080, 1920

    def _get_current_reels_style(self) -> ReelsSubtitleStyle:
        font_name = self.combo_reels_font.get()
        font_size = int(self.combo_reels_fontsize.get().split(" ")[0])
        outline_w = int(self.combo_reels_outline.get().split(" ")[0])
        margin_v = int(self.combo_reels_marginv.get().split(" ")[0])
        w_px, h_px = self._resolve_reels_resolution()

        # Škálovat velikost písma, pokud uživatel zvolí 4K rozlišení
        scale_factor = max(w_px, h_px) / 1920.0
        scaled_font_size = max(24, int(round(font_size * scale_factor)))
        scaled_outline = max(0, int(round(outline_w * scale_factor)))
        scaled_margin_v = max(20, int(round(margin_v * scale_factor)))

        return ReelsSubtitleStyle(
            font_name=font_name,
            font_size=scaled_font_size,
            primary_color=self.reels_primary_hex,
            highlight_color=self.reels_highlight_hex,
            outline_color=self.reels_outline_hex,
            outline_width=scaled_outline,
            shadow_depth=3 if scaled_outline > 0 else 0,
            box_background=bool(self.chk_reels_box.get()),
            uppercase=bool(self.chk_reels_caps.get()),
            karaoke_highlight=bool(self.chk_reels_karaoke.get()),
            pop_animation=bool(self.chk_reels_pop.get()),
            alignment=self.combo_reels_pos.get(),
            margin_v=scaled_margin_v,
            play_res_x=w_px,
            play_res_y=h_px,
        )

    def _update_reels_preview(self):
        try:
            style = self._get_current_reels_style()
            pil_img = render_style_preview_image(style, preview_size=(360, 135))
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(360, 135))
            self.lbl_reels_preview.configure(image=ctk_img, text="")
            self.lbl_reels_preview._image_ref = ctk_img
        except Exception:
            pass

    def _get_max_words_choice(self) -> int:
        val = self.combo_reels_words.get()
        if val.startswith("1 "):
            return 1
        if val.startswith("2–3"):
            return 3
        if val.startswith("4–5"):
            return 5
        return 7

    def _start_reels_transcribe_and_correct(self):
        target_file = self.selected_file or (self.files[0] if self.files else None)
        if not target_file or not os.path.exists(target_file):
            messagebox.showwarning("Žádný soubor", "Nejprve prosím vyberte vlevo video nebo audio soubor.")
            return

        self._save_settings()
        model_size = self.combo_whisper_model.get()
        lang = self.combo_whisper_lang.get()
        device_pref = self.combo_whisper_device.get()
        max_words = self._get_max_words_choice()
        use_gemma = bool(self.chk_reels_use_gemma.get())
        hint_text = self.entry_reels_hint.get().strip()

        t_start = parse_time_input(self.entry_reels_start.get())
        t_end = parse_time_input(self.entry_reels_end.get())

        self.cancel_event.clear()
        self._set_busy(True, f"🎙️ Spouštím Whisper Turbo s word-level časováním pro {os.path.basename(target_file)}...")
        self.progress_bar.set(0.02)
        self.txt_reels_editor.delete("1.0", "end")

        def _on_prog(frac: float, msg: str):
            scaled = frac * (0.55 if use_gemma else 1.0)
            self.after(0, lambda: self.progress_bar.set(scaled))
            self.after(0, lambda: self.lbl_status.configure(text=msg))

        def _worker():
            try:
                res = self.transcriber.transcribe(
                    media_path=target_file,
                    model_size=model_size,
                    language=lang,
                    device_preference=device_pref,
                    word_timestamps=True,
                    trim_start_sec=t_start,
                    trim_end_sec=t_end,
                    progress_callback=_on_prog,
                    cancel_event=self.cancel_event,
                )

                chunks = group_words_into_reels_chunks(
                    res.segments,
                    max_words_per_chunk=max_words,
                )
                self.reels_chunks = chunks
                initial_editor_txt = format_chunks_for_editor(chunks)
                self.after(0, lambda t=initial_editor_txt: self._set_reels_editor_text(t))

                status_note = f"✅ Whisper přepsal {len(chunks)} Reels titulků."
                if use_gemma and chunks and not self.cancel_event.is_set():
                    ok_g, corrected_chunks, g_msg = correct_chunks_with_gemma(
                        chunks=chunks,
                        bionic_client=self.bionic_client,
                        custom_hint=hint_text,
                        progress_callback=lambda f, m: (
                            self.after(0, lambda: self.progress_bar.set(f)),
                            self.after(0, lambda: self.lbl_status.configure(text=m)),
                        ),
                    )
                    if ok_g:
                        self.reels_chunks = corrected_chunks
                        corr_txt = format_chunks_for_editor(corrected_chunks)
                        self.after(0, lambda t=corr_txt: self._set_reels_editor_text(t))
                        status_note = f"✅ Hotovo! {g_msg}"
                    else:
                        status_note = f"✅ Whisper hotov ({len(chunks)} titulků). Info z Gemmy: {g_msg}"

                self.after(0, lambda: self.progress_bar.set(1.0))
                self.after(0, lambda s=status_note: self._set_busy(False, s))

            except Exception as e:
                self.after(0, lambda: self._set_busy(False, f"❌ Chyba při přípravě Reels titulků: {str(e)}"))
                if not self.cancel_event.is_set():
                    self.after(0, lambda err=str(e): messagebox.showerror("Chyba", err))

        threading.Thread(target=_worker, daemon=True).start()

    def _set_reels_editor_text(self, text: str):
        self.txt_reels_editor.delete("1.0", "end")
        self.txt_reels_editor.insert("1.0", text)

    def _start_reels_gemma_only(self):
        raw_editor = self.txt_reels_editor.get("1.0", "end").strip()
        chunks = parse_chunks_from_editor(raw_editor, self.reels_chunks)
        if not chunks:
            messagebox.showwarning("Žádné titulky", "V editoru nejsou žádné časované titulky [MM:SS -> MM:SS]. Nejprve spusťte přepis.")
            return

        hint_text = self.entry_reels_hint.get().strip()
        self.cancel_event.clear()
        self._set_busy(True, "🧠 Odesílám titulky do lokálního modelu Gemma 4 12B QAT ke kontextové opravě...")
        self.progress_bar.set(0.3)

        def _worker():
            ok_g, corrected_chunks, g_msg = correct_chunks_with_gemma(
                chunks=chunks,
                bionic_client=self.bionic_client,
                custom_hint=hint_text,
            )

            def _done():
                self.progress_bar.set(1.0 if ok_g else 0.0)
                if ok_g:
                    self.reels_chunks = corrected_chunks
                    self._set_reels_editor_text(format_chunks_for_editor(corrected_chunks))
                    self._set_busy(False, f"✅ {g_msg}")
                else:
                    self._set_busy(False, f"❌ {g_msg}")
                    messagebox.showwarning("Gemma 4 12B korektura", g_msg)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_reels_export(self):
        raw_editor = self.txt_reels_editor.get("1.0", "end").strip()
        chunks = parse_chunks_from_editor(raw_editor, self.reels_chunks)
        if not chunks:
            messagebox.showwarning(
                "Žádné titulky",
                "Nejprve prosím spusťte krok '1. Přepsat (Whisper Turbo) + Opravit (Gemma 4 12B)' vlevo.",
            )
            return

        self.reels_chunks = chunks
        target_file = self.selected_file or (self.files[0] if self.files else None)
        export_mode = self.combo_reels_export_mode.get()
        fps = int(self.combo_reels_fps.get().split(" ")[0])
        style = self._get_current_reels_style()

        t_start = parse_time_input(self.entry_reels_start.get())
        t_end = parse_time_input(self.entry_reels_end.get())
        reset_zero = "00:00" in self.combo_reels_timeline_mode.get()

        # Určit příponu a výchozí název výstupního souboru
        if "Průhledné .MOV" in export_mode or "Hotové .MOV" in export_mode:
            ext = "mov"
            tag = "_reels_overlay_alpha" if "Průhledné" in export_mode else "_reels_subtitled"
        elif "Pouze titulkové" in export_mode:
            ext = "ass"
            tag = "_reels_subtitles"
        else:
            ext = "mp4"
            if "Černé pozadí" in export_mode:
                tag = "_reels_overlay_black"
            elif "Zelené plátno" in export_mode:
                tag = "_reels_overlay_greenscreen"
            else:
                tag = "_reels_subtitled"

        if target_file and os.path.exists(target_file):
            out_path = self._resolve_output_path(target_file, ext, suffix_tag=tag)
            total_dur = float(self.selected_file_info.get("duration_sec") or 0.0)
            if total_dur <= 0:
                info = probe_media_info(target_file)
                total_dur = float(info.get("duration_sec") or 0.0)
        else:
            out_folder = self._get_youtube_download_folder()
            out_path = os.path.join(out_folder, f"reels_overlay{tag}.{ext}")
            total_dur = max(ch["end"] for ch in chunks) + 0.5

        if total_dur <= 0:
            total_dur = max(ch["end"] for ch in chunks) + 0.5

        self.cancel_event.clear()
        self._set_busy(True, f"🎬 Generuji Reels Video Overlay ({os.path.basename(out_path)})...")
        self.progress_bar.set(0.05)

        def _on_prog(frac: float, msg: str):
            self.after(0, lambda: self.progress_bar.set(frac))
            self.after(0, lambda: self.lbl_status.configure(text=msg))

        def _worker():
            ok, msg_or_path = render_reels_video_overlay(
                chunks=chunks,
                style=style,
                output_path=out_path,
                export_mode=export_mode,
                total_duration_sec=total_dur,
                fps=fps,
                source_video_path=target_file,
                trim_start_sec=t_start,
                trim_end_sec=t_end,
                reset_timeline_to_zero=reset_zero,
                progress_callback=_on_prog,
                cancel_event=self.cancel_event,
            )

            def _done():
                self.progress_bar.set(1.0 if ok else 0.0)
                if ok:
                    self._set_busy(False, f"✅ Reels Overlay vyexportován: {out_path}")
                    davinci_tip = (
                        "\n\n💡 TIP PRO DAVINCI RESOLVE:\n"
                        "• Průhledné .MOV: Přetáhněte soubor rovnou do Video Track 2 (V2) nad vaše původní video – pozadí je již 100% průhledné!\n"
                        "• Černé pozadí .MP4: Vložte do V2 a v Inspectoru -> Composite Mode zvolte 'Screen' nebo 'Add'.\n"
                        "• Současně byly vedle videa uloženy i titulkové soubory .ASS a .SRT."
                    )
                    messagebox.showinfo(
                        "Reels Titulky & Overlay hotovy!",
                        f"Výstup byl úspěšně uložen:\n{msg_or_path}{davinci_tip}",
                    )
                else:
                    self._set_busy(False, f"❌ Chyba při renderu Reels titulků: {msg_or_path}")
                    if not self.cancel_event.is_set():
                        messagebox.showerror("Chyba při generování Reels Overlaye", msg_or_path)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()


if __name__ == "__main__":
    app = BionicMediaStudioApp()
    app.mainloop()
