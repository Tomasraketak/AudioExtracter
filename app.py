import os
import sys
import json
import threading
import subprocess
from pathlib import Path
from tkinter import filedialog, messagebox
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
)

# Nastavení vzhledu
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

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
        self.geometry("1180x820")
        self.minsize(1020, 720)
        ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
        if os.path.exists(ico_path):
            try:
                self.iconbitmap(ico_path)
            except Exception:
                pass

        # Stav aplikace
        self.files: list[str] = []
        self.selected_file: str | None = None
        self.output_dir: str = ""
        self.is_busy: bool = False
        self.cancel_event = threading.Event()
        self.nvenc_available = check_nvenc_available()

        self.transcriber = WhisperTranscriber()
        self.last_transcription: TranscriptionResult | None = None
        self.bionic_client = BionicStudioClient()

        self._load_settings()
        self._build_ui()
        self._setup_drag_and_drop()

        # Automatický test Bionic Studio připojení na pozadí
        threading.Thread(target=self._refresh_bionic_status_bg, daemon=True).start()

    def _load_settings(self):
        self.settings = {
            "bionic_url": "http://localhost:1234/v1",
            "bionic_api_key": "lm-studio",
            "whisper_model": "large-v3-turbo (Doporučeno pro RTX 5060 Ti - extrémně rychlý a přesný)",
            "whisper_device": "GPU - NVIDIA CUDA (RTX 5060 Ti 16GB FP16)",
            "whisper_lang": "Auto (Automatická detekce)",
            "output_dir": "",
        }
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
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
        self.tab_ai = self.tabview.add("🤖 AI Přepis & Bionic Studio")
        self.tab_help = self.tabview.add("📖 Návod & Nastavení")

        self._build_tab_audio()
        self._build_tab_video()
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

        ctk.CTkLabel(whisper_bar, text="AI Model (Faster-Whisper):", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, padx=10, pady=(8, 2), sticky="w"
        )
        self.combo_whisper_model = ctk.CTkOptionMenu(
            whisper_bar,
            values=[
                "large-v3-turbo (Doporučeno pro RTX 5060 Ti - extrémně rychlý a přesný)",
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
4. LOKÁLNÍ AI PŘEPIS AUDIA (FASTER-WHISPER NA RTX 5060 Ti)
--------------------------------------------------------------------
• Přejděte na záložku „🤖 AI Přepis & Bionic Studio“.
• Vyberte vlevo soubor, který chcete přepsat (funguje přímo na videa MP4/MOV i na audio MP3/AAC/WAV – program si zvuk extrahuje automaticky na pozadí).
• Doporučený model: „large-v3-turbo“ – díky 16 GB VRAM na vaší RTX 5060 Ti běží v přesnosti FP16 několikanásobně rychleji než běžný Whisper při špičkové přesnosti v češtině i angličtině.
• Při prvním spuštění se zvolený model automaticky stáhne do složky `models_cache` (cca 1.5 GB). Další spuštění už jsou okamžitá a plně offline.
• Po dokončení můžete přepis uložit jako čistý text (.TXT) nebo jako časované titulky (.SRT / .VTT) přímo do videa.

--------------------------------------------------------------------
5. PROPOJENÍ S BIONIC STUDIEM / LM STUDIEM
--------------------------------------------------------------------
Jak propojit aplikaci s Bionic / LM Studiem:
1. Otevřete LM Studio / Bionic.
2. Načtěte svůj oblíbený lokální LLM model (např. Llama-3.1, Qwen-2.5, Mistral nebo Gemma).
3. V sekci Developer / Local Server zapněte lokální API server (standardně běží na adrese `http://localhost:1234/v1`).
4. V naší aplikaci klikněte na „🔄 Otestovat spojení“ – rozsvítí se zelený stav „Připojeno“.
5. Vyberte požadovaný úkol (Shrnutí, Článek, Akční body, Překlad) a klikněte na „✨ Zpracovat v Bionic Studiu“.
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
        self.after(0, lambda: self.lbl_bionic_status.configure(text=prefix + msg, text_color=color))

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


if __name__ == "__main__":
    app = BionicMediaStudioApp()
    app.mainloop()
