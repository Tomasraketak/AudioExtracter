# 🎬 Bionic Media & Audio Studio (RTX 5060 Ti & Ryzen 9600 Edition)

Moderní, plně lokální desktopová aplikace s grafickým rozhraním (GUI) pro:
1. **Extrakci audia z videí** (`MP4`, `MOV`, `MKV`, `AVI`, `WEBM`, `FLV`, `WMV` ➔ `MP3`, `AAC`, `M4A`, `WAV`, `FLAC`, `OGG`, `OPUS`).
2. **Vzájemnou konverzi audio a video formátů** (`AAC ⇄ MP3 ⇄ WAV ⇄ FLAC` a `MP4 ⇄ MOV ⇄ MKV ⇄ WEBM ⇄ AVI` včetně bleskového *Stream Copy* režimu a GPU enkódování přes **NVIDIA NVENC**).
3. **Stahování YouTube videí a audia podle odkazu** přímo do formátů `MP4`, `MOV`, `MKV`, `MP3`, `AAC` nebo `WAV` s automatickým přidáním do fronty.
4. **Lokální AI přepis audia a videa (Speech-to-Text)** pomocí **Faster-Whisper** s přímým využitím tvého modelu v **Bionic Studiu** na **NVIDIA GeForce RTX 5060 Ti (16 GB VRAM)** a vícejádrovém procesoru **AMD Ryzen 9600**.
5. **Propojení s Bionic Studiem / LM Studiem** přes lokální API pro okamžité shrnutí přepisů, vytváření poznámek, akčních bodů a překladů.

---

## ⚡ Instalace a spuštění jedním příkazem (včetně zástupce na Ploše)

Otevřete **Příkazový řádek (CMD)** a vložte tento jediný příkaz:

```cmd
cd /d "c:\Projekty\AudioExtracter\AudioExtracter" && call setup.bat
```

### Co tento příkaz automaticky udělá?
1. Vytvoří izolované Python prostředí (`.venv`).
2. Nainstaluje veškeré potřebné knihovny, **přibalený FFmpeg engine** (nemusíte nic instalovat ručně do Windows) a **NVIDIA CUDA 12** knihovny pro vaši RTX 5060 Ti.
3. Vygeneruje ikonu aplikace (`app_icon.ico`) a **automaticky vytvoří zástupce `Bionic Media & Audio Studio` přímo na vaší Ploše**.
4. Rovnou spustí samotnou aplikaci.

> **Další spouštění:** Kdykoliv později už stačí poklepat na zástupce **Bionic Media & Audio Studio** na Ploše (nebo spustit soubor [`run.bat`](file:///c:/Projekty/AudioExtracter/AudioExtracter/run.bat)).

---

## 📖 Podrobný návod k použití

### 1. Nahrání souborů (Drag & Drop)
* **Přetažení myší:** Chyťte libovolné video (`MP4`, `MOV`, `MKV`...) nebo audio (`AAC`, `MP3`, `WAV`...) v Průzkumníku Windows a přetáhněte ho do modrého pole v levém panelu.
* **Tlačítko `➕ Vybrat soubory`:** Umožní vybrat jeden nebo desítky souborů najednou (dávkové zpracování).
* **Technické informace:** Po kliknutí na soubor v seznamu uvidíte dole vlevo jeho přesnou délku, velikost v MB, použitý video kodek, rozlišení i parametry zvukové stopy.
* **Výstupní složka:** Pokud necháte pole výstupní složky prázdné, nové soubory se uloží přímo do stejné složky vedle originálu.

---

### 2. Extrakce audia z videa & Konverze audio formátů (`AAC ⇄ MP3 ⇄ WAV...`)
Přejděte na první záložku **🎵 Extrakce & Konverze Audia**:
* **Cílový formát:** Vyberte `MP3`, `AAC`, `M4A`, `WAV` (bezztrátový PCM), `FLAC` (bezztrátový komprimovaný), `OGG` nebo `OPUS`.
* **Kvalita / Bitrate:** Nastavte datový tok od `64k` (mluvené slovo) přes `192k` (standard) až po `320k` (studiová kvalita).
* **Vzorkovací frekvence a kanály:** Můžete ponechat původní hodnoty, nebo vynutit `48000 Hz` / `44100 Hz`, případně převést zvuk na `Stereo (2.0)` či `Mono (1.0)`.
* **Vyrovnání hlasitosti (EBU R128):** Zaškrtnutím volby automaticky vyrovnáte příliš tiché nebo přebuzené nahrávky na vysílací standard.
* Klikněte na **🎵 Extrahovat / Převést Audio** – zpracují se všechny soubory ve frontě.

---

### 3. Konverze videa (`MP4 ⇄ MOV ⇄ MKV ⇄ WEBM ⇄ AVI`)
Přejděte na druhou záložku **🎬 Konverze Videa (MP4 ⇄ MOV)**:
* **Cílový formát:** Zvolte `MP4`, `MOV`, `MKV`, `WEBM` nebo `AVI`.
* **Režim enkódování / Akcelerace:**
  * **Stream Copy (Bleskový převod bez rekomprese):** Pouze změní kontejner (např. `MOV ➔ MP4` nebo `MP4 ➔ MOV`) bez jakéhokoliv překódování obrazu. Trvá jen zlomek vteřiny a zachová 100% kvalitu.
  * **GPU: NVIDIA NVENC H.264 / HEVC (RTX 5060 Ti):** Využije hardwarový enkodér na vaší grafické kartě. Ideální, pokud zároveň měníte rozlišení (`1080p`, `720p`, `480p`) nebo chcete zmenšit velikost videa pomocí nastavení kvality (`CRF / CQ`).
  * **CPU: x264 (Ryzen 9600):** Softwarové kódování přes procesor.

---

### 3.5. Stahování YouTube videí a audia (`MP4 / MOV / MP3 / AAC / WAV`)
Přejděte na záložku **📥 YouTube Stahovač (MP4 / MOV)**:
* Vložte odkaz na YouTube video (nebo klikněte na **📋 Vložit ze schránky**, což rovnou načte název videa, kanál, délku a dostupná rozlišení).
* Zvolte cílový formát (`MP4`, `MOV`, `MKV`, nebo čisté audio `MP3`, `AAC`, `WAV`) a maximální rozlišení (`1080p Full HD`, `4K / 1440p`, `720p`, `480p`).
* Klikněte na **📥 Stáhnout z YouTube** – po stažení se soubor automaticky zařadí do levé fronty a rovnou vybere, takže na něj můžete ihned pustit **AI přepis** nebo další konverzi!

---

### 4. Lokální AI Přepis audia a videa (přímo z modelu v Bionic Studiu – bez stahování!)
Přejděte na třetí záložku **🤖 AI Přepis & Bionic Studio**:
1. Vyberte vlevo video nebo audio soubor.
2. Zvolte **AI Model**:
   * `Bionic Studio: whisper-large-v3-turbo (Lokální model - bez stahování)` *(Výchozí)* – aplikace automaticky najde váš již stažený model v Bionic Studiu (`~/.lmstudio/models/handy-computer/whisper-large-v3-turbo-gguf/whisper-large-v3-turbo-Q8_0.gguf`) a spustí jej přímo na vaší **RTX 5060 Ti (16 GB VRAM)** v režimu `float16` za pomocí CUDA knihoven z Bionic Studia **100% offline bez stahování z internetu**.
3. Zvolte **Jazyk audia** (`Auto`, `Čeština`, `Angličtina`...) a **Výpočetní jednotku** (`GPU - NVIDIA CUDA` nebo `CPU - AMD Ryzen 9600`).
4. Klikněte na **🎙️ Spustit AI Přepis**.
5. Přepis naskakuje živě po větách přímo do textového editoru. Následně ho můžete jedním kliknutím:
   * Zkopírovat do schránky (`📋 Kopírovat text`)
   * Uložit jako čistý text (`💾 Uložit .TXT`)
   * Uložit jako časované titulky k videu (`🎬 Uložit titulky .SRT` nebo `🌐 Uložit .VTT`)

---

### 5. Propojení s Bionic Studiem / LM Studiem
Aplikace obsahuje přímého klienta pro lokální AI server **Bionic Studio / LM Studio**:
1. Aplikace si **automaticky načte port vašeho běžícího Bionic Studia** ze souboru `~/.lmstudio/apps/bionic/.internal/http-server-config.json` (např. `http://localhost:1235/v1` nebo `1234`).
2. V naší aplikaci na záložce **🤖 AI Přepis & Bionic Studio** uvidíte rovnou stav 🟢 **Připojeno (google/gemma-4-12b-qat + Whisper Turbo nalezen)**.
3. Vyberte z roletky **AI Úkol** (např. *Vytvoř strukturované shrnutí*, *Uprav přepis do čtivého článku*, *Vypiš úkoly Action Items*, *Přelož přepis*) a klikněte na **✨ Zpracovat v Bionic Studiu**.

---

## 🗂️ Struktura projektu
* [`app.py`](file:///c:/Projekty/AudioExtracter/AudioExtracter/app.py) – Hlavní GUI aplikace (CustomTkinter + Drag & Drop).
* [`media_engine.py`](file:///c:/Projekty/AudioExtracter/AudioExtracter/media_engine.py) – FFmpeg jádro pro analýzu médií, extrakci zvuku, hlasitostní normalizaci a GPU NVENC konverze.
* [`ai_engine.py`](file:///c:/Projekty/AudioExtracter/AudioExtracter/ai_engine.py) – Modul pro lokální AI přepis s podporou modelu z Bionic Studia, CUDA 12 z Bionic Studia a komunikaci s Bionic Studio API.
* [`gguf_whisper_loader.py`](file:///c:/Projekty/AudioExtracter/AudioExtracter/gguf_whisper_loader.py) – Offline loader a konvertor pro stažený `whisper-large-v3-turbo-Q8_0.gguf` model přímo z Bionic Studia.
* [`setup.bat`](file:///c:/Projekty/AudioExtracter/AudioExtracter/setup.bat) – Jednoklikový instalátor prostředí, závislostí a zástupce na Ploše.
* [`run.bat`](file:///c:/Projekty/AudioExtracter/AudioExtracter/run.bat) – Rychlý spouštěč aplikace bez konzole.
* [`create_shortcut.py`](file:///c:/Projekty/AudioExtracter/AudioExtracter/create_shortcut.py) & [`generate_icon.py`](file:///c:/Projekty/AudioExtracter/AudioExtracter/generate_icon.py) – Generátor ikony a zástupce na Plochu Windows.
