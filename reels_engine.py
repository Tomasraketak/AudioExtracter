import os
import re
import json
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Tuple
from PIL import Image, ImageDraw, ImageFont

from media_engine import get_ffmpeg_exe, probe_media_info, CREATE_NO_WINDOW


def parse_time_input(time_str: str) -> Optional[float]:
    """
    Převede textový čas (např. '01:25', '00:01:25.5', '85') na sekundy (float).
    Vrací None, pokud je řetězec prázdný nebo 'konec' / 'end'.
    """
    if not time_str:
        return None
    s = time_str.strip().lower()
    if not s or s in {"konec", "end", "max", "full", "celé", "cele"}:
        return None

    parts = s.split(":")
    try:
        if len(parts) == 1:
            return max(0.0, float(parts[0].replace(",", ".")))
        elif len(parts) == 2:
            m = int(parts[0])
            sec = float(parts[1].replace(",", "."))
            return max(0.0, m * 60 + sec)
        elif len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            sec = float(parts[2].replace(",", "."))
            return max(0.0, h * 3600 + m * 60 + sec)
    except Exception:
        return None
    return None


def seconds_to_mmss(seconds: float) -> str:
    total = max(0.0, float(seconds))
    m = int(total) // 60
    s = total - (m * 60)
    return f"{m:02d}:{s:05.2f}"


def seconds_to_ass_time(seconds: float) -> str:
    """Formát času pro ASS titulky: H:MM:SS.cc (setiny sekundy)."""
    total = max(0.0, float(seconds))
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = int(total % 60)
    cs = int(round((total - int(total)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """
    Převede HTML barvu '#RRGGBB' na ASS formát '&HAABBGGRR' (alfa 00 = neprůhledná, FF = průhledná).
    """
    c = hex_color.strip().lstrip("#")
    if len(c) != 6:
        c = "FFFFFF"
    r = c[0:2]
    g = c[2:4]
    b = c[4:6]
    return f"&H{alpha:02X}{b.upper()}{g.upper()}{r.upper()}"


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    c = hex_color.strip().lstrip("#")
    if len(c) != 6:
        return (255, 255, 255)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def group_words_into_reels_chunks(
    segments: List[Dict[str, Any]],
    max_words_per_chunk: int = 3,
    max_chunk_duration: float = 2.6,
    pause_threshold: float = 0.45,
) -> List[Dict[str, Any]]:
    """
    Seskupí slova z Faster-Whisper přepisu do krátkých dynamických bloků (Reels / TikTok styl).
    Každý blok má:
    {
        "start": float,
        "end": float,
        "text": str,
        "words": [{"start": float, "end": float, "word": str}]
    }
    """
    all_words: List[Dict[str, Any]] = []
    for seg in segments:
        seg_words = seg.get("words")
        if seg_words:
            for w in seg_words:
                w_text = w.get("word", "").strip()
                if w_text:
                    all_words.append({
                        "start": float(w["start"]),
                        "end": float(w["end"]),
                        "word": w_text,
                    })
        else:
            # Fallback pokud segment nemá word_timestamps: rovnoměrně rozdělit slova v segmentu
            raw_words = [x for x in seg.get("text", "").strip().split() if x]
            if not raw_words:
                continue
            s_start = float(seg["start"])
            s_end = max(s_start + 0.1, float(seg["end"]))
            dur = s_end - s_start
            total_chars = sum(len(w) for w in raw_words) or 1
            cur = s_start
            for w_str in raw_words:
                w_dur = dur * (len(w_str) / total_chars)
                all_words.append({
                    "start": cur,
                    "end": cur + w_dur,
                    "word": w_str,
                })
                cur += w_dur

    if not all_words:
        return []

    chunks: List[Dict[str, Any]] = []
    current_words: List[Dict[str, Any]] = []

    for w in all_words:
        if not current_words:
            current_words.append(w)
            continue

        prev_w = current_words[-1]
        gap = w["start"] - prev_w["end"]
        chunk_dur = w["end"] - current_words[0]["start"]
        ends_sentence = prev_w["word"].endswith((".", "!", "?", ":", ";"))

        if (
            len(current_words) >= max_words_per_chunk
            or gap >= pause_threshold
            or chunk_dur >= max_chunk_duration
            or (ends_sentence and len(current_words) >= 2)
        ):
            chunks.append({
                "start": current_words[0]["start"],
                "end": current_words[-1]["end"],
                "text": " ".join(x["word"] for x in current_words),
                "words": current_words,
            })
            current_words = [w]
        else:
            current_words.append(w)

    if current_words:
        chunks.append({
            "start": current_words[0]["start"],
            "end": current_words[-1]["end"],
            "text": " ".join(x["word"] for x in current_words),
            "words": current_words,
        })

    return chunks


def update_chunk_words_from_edited_text(chunk: Dict[str, Any], new_text: str) -> Dict[str, Any]:
    """
    Aktualizuje text v bloku (např. po opravě modelem Gemma 4 12B QAT nebo po ruční úpravě uživatelem)
    a přepočítá časování jednotlivých slov tak, aby Karaoke zvýraznění sedělo na setinu sekundy.
    """
    clean_words = [w for w in new_text.strip().split() if w]
    old_words = chunk.get("words", [])
    start = float(chunk["start"])
    end = max(start + 0.08, float(chunk["end"]))

    if not clean_words:
        return {
            "start": start,
            "end": end,
            "text": "",
            "words": [],
        }

    if len(clean_words) == len(old_words):
        new_words_list = []
        for ow, nw_str in zip(old_words, clean_words):
            new_words_list.append({
                "start": float(ow["start"]),
                "end": float(ow["end"]),
                "word": nw_str,
            })
    else:
        dur = end - start
        total_chars = sum(max(1, len(w)) for w in clean_words)
        cur = start
        new_words_list = []
        for w_str in clean_words:
            w_dur = dur * (max(1, len(w_str)) / total_chars)
            new_words_list.append({
                "start": cur,
                "end": cur + w_dur,
                "word": w_str,
            })
            cur += w_dur

    return {
        "start": start,
        "end": end,
        "text": " ".join(clean_words),
        "words": new_words_list,
    }


def format_chunks_for_editor(chunks: List[Dict[str, Any]]) -> str:
    """
    Zformátuje Reels bloky do přehledného textu v GUI editoru:
    [00:01.20 -> 00:02.45] Text titulku
    """
    lines = []
    for ch in chunks:
        txt = ch.get("text", "").strip()
        if not txt:
            continue
        s_str = seconds_to_mmss(ch["start"])
        e_str = seconds_to_mmss(ch["end"])
        lines.append(f"[{s_str} -> {e_str}] {txt}")
    return "\n".join(lines)


def parse_chunks_from_editor(editor_text: str, existing_chunks: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Načte upravené titulky z GUI editoru zpět do struktury bloků se zachováním word-level časování.
    """
    pattern = re.compile(r"^\[(\d+:\d+(?:\.\d+)?)\s*->\s*(\d+:\d+(?:\.\d+)?)\]\s*(.*)$")
    parsed: List[Dict[str, Any]] = []
    raw_lines = [ln.strip() for ln in editor_text.strip().splitlines() if ln.strip()]

    for idx, line in enumerate(raw_lines):
        m = pattern.match(line)
        if m:
            s_sec = parse_time_input(m.group(1)) or 0.0
            e_sec = parse_time_input(m.group(2)) or (s_sec + 1.5)
            txt = m.group(3).strip()
            if not txt:
                continue
            if existing_chunks and idx < len(existing_chunks):
                base_ch = dict(existing_chunks[idx])
                base_ch["start"] = s_sec
                base_ch["end"] = e_sec
                parsed.append(update_chunk_words_from_edited_text(base_ch, txt))
            else:
                base_ch = {"start": s_sec, "end": e_sec, "text": txt, "words": []}
                parsed.append(update_chunk_words_from_edited_text(base_ch, txt))
        else:
            # Pokud uživatel smazal závorky s časem, zkusíme spárovat s existujícím indexem
            if existing_chunks and idx < len(existing_chunks):
                parsed.append(update_chunk_words_from_edited_text(existing_chunks[idx], line))

    return parsed


def correct_chunks_with_gemma(
    chunks: List[Dict[str, Any]],
    bionic_client,
    custom_hint: str = "",
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    """
    Odešle celý očíslovaný přepis do lokálního modelu Gemma 4 12B QAT v Bionic Studiu,
    aby opravil místa, která nedávají smysl (přeslechnutá slova), podle okolního kontextu.
    Zachová přesné přiřazení k časovým značkám.
    """
    if not chunks:
        return False, chunks, "Žádné titulky ke korektuře."

    ok, status_msg, llm_models = bionic_client.check_connection()
    if not ok:
        return False, chunks, f"Nelze se spojit s Bionic Studiem ({status_msg})."

    # Preferovat gemma-4-12b-qat pokud je v Bionic Studiu k dispozici
    chosen_model = None
    for m in llm_models:
        if "gemma" in m.lower():
            chosen_model = m
            break
    if not chosen_model and llm_models:
        chosen_model = llm_models[0]

    # Sestavit očíslovaný seznam řádků
    numbered_lines = []
    for i, ch in enumerate(chunks, start=1):
        numbered_lines.append(f"[{i}] {ch['text']}")
    full_numbered_text = "\n".join(numbered_lines)

    hint_part = f"\nKontext / téma videa od uživatele: {custom_hint.strip()}\n" if custom_hint.strip() else ""

    system_prompt = (
        "Jsi špičkový jazykový korektor a editor titulků pro krátká videa (Reels / Shorts / YouTube). "
        "Dostaneš očíslovaný přepis audia pořízený modelem Whisper. "
        "Tvým úkolem je projít CELÝ přepis, pochopit jeho kontext a téma, a opravit místa, kde text "
        "nedává smysl (špatně rozpoznaná nebo přeslechnutá slova, zkomolené názvy, odborné výrazy nebo "
        "gramatické nesmysly) podle toho, co bylo na základě okolního kontextu a zvukové podobnosti reálně řečeno.\n\n"
        "STRIKTNÍ PRAVIDLA VÝSTUPU:\n"
        "1. Zachovej přesně stejné číslování řádků [1], [2], [3]... až do konce.\n"
        "2. Každý řádek musí začínat svým číslem v hranaté závorce, např.: [1] Opravený text\n"
        "3. Neslučuj řádky dohromady a žádný řádek nevynechávej, aby sedělo časování titulků.\n"
        "4. Nevypisuj žádný úvodní ani závěrečný komentář, vypiš POUZE opravené očíslované řádky."
    )

    user_prompt = (
        f"Oprav prosím následující přepis audia na místech, kde text nedává smysl, podle okolního kontextu.{hint_part}\n\n"
        f"PŘEPIS K OPRAVĚ:\n{full_numbered_text}"
    )

    if progress_callback:
        progress_callback(0.55, f"🧠 Gemma 4 12B QAT analyzuje kontext a opravuje přepis ({len(chunks)} titulků)...")

    import requests
    url = f"{bionic_client.base_url}/chat/completions"
    payload = {
        "model": chosen_model or "google/gemma-4-12b-qat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
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
                "Authorization": f"Bearer {bionic_client.api_key}",
                "Content-Type": "application/json",
            },
            timeout=240,
        )
        if resp.status_code != 200:
            return False, chunks, f"Chyba Bionic Studio API ({resp.status_code}): {resp.text}"

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return False, chunks, "Gemma vrátila prázdnou odpověď."

        reply = choices[0].get("message", {}).get("content", "").strip()
        # Odstranit případné <think>...</think> bloky
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

        # Parsovat [i] řádky
        line_map: Dict[int, str] = {}
        for line in reply.splitlines():
            m = re.match(r"^\s*\[(\d+)\]\s*(.+)$", line.strip())
            if m:
                idx = int(m.group(1))
                txt = m.group(2).strip()
                if txt:
                    line_map[idx] = txt

        if not line_map:
            return False, chunks, "Odpověď z modelu neměla očekávaný očíslovaný formát, ponechávám původní přepis."

        updated_chunks: List[Dict[str, Any]] = []
        changed_count = 0
        for i, ch in enumerate(chunks, start=1):
            if i in line_map:
                new_txt = line_map[i]
                if new_txt != ch["text"]:
                    changed_count += 1
                updated_chunks.append(update_chunk_words_from_edited_text(ch, new_txt))
            else:
                updated_chunks.append(ch)

        return True, updated_chunks, f"Gemma 4 12B zkontrolovala kontext a upravila {changed_count}/{len(chunks)} bloků."

    except Exception as e:
        return False, chunks, f"Chyba při komunikaci s Gemma 4 12B: {str(e)}"


class ReelsSubtitleStyle:
    """Konfigurace vizuálního stylu Reels titulků."""

    def __init__(
        self,
        font_name: str = "Arial Black",
        font_size: int = 76,
        primary_color: str = "#FFFFFF",
        highlight_color: str = "#FFE600",
        outline_color: str = "#000000",
        back_color: str = "#000000",
        outline_width: int = 6,
        shadow_depth: int = 3,
        box_background: bool = False,
        uppercase: bool = True,
        karaoke_highlight: bool = True,
        pop_animation: bool = True,
        alignment: str = "Dole (Reels standard)",
        margin_v: int = 260,
        play_res_x: int = 1080,
        play_res_y: int = 1920,
    ):
        self.font_name = font_name
        self.font_size = int(font_size)
        self.primary_color = primary_color
        self.highlight_color = highlight_color
        self.outline_color = outline_color
        self.back_color = back_color
        self.outline_width = int(outline_width)
        self.shadow_depth = int(shadow_depth)
        self.box_background = bool(box_background)
        self.uppercase = bool(uppercase)
        self.karaoke_highlight = bool(karaoke_highlight)
        self.pop_animation = bool(pop_animation)
        self.alignment = alignment
        self.margin_v = int(margin_v)
        self.play_res_x = int(play_res_x)
        self.play_res_y = int(play_res_y)


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def generate_ass_subtitle_content(
    chunks: List[Dict[str, Any]],
    style: ReelsSubtitleStyle,
) -> str:
    """
    Vygeneruje kompletní .ASS skript (Advanced SubStation Alpha) s podporou:
    - Vlastního fontu, velikosti, obrysu, stínu nebo boxu na pozadí
    - Word-by-word Karaoke zvýrazňování aktivního slova v barvě `highlight_color`
    - Pop-in animace při nástupu titulku
    """
    prim_ass = hex_to_ass_color(style.primary_color, alpha=0)
    high_ass = hex_to_ass_color(style.highlight_color, alpha=0)
    out_ass = hex_to_ass_color(style.outline_color, alpha=0)
    back_ass = hex_to_ass_color(style.back_color, alpha=90 if style.box_background else 110)

    # ASS Alignment: 2 = Bottom Center, 5 = Middle Center, 8 = Top Center
    align_code = 2
    if "Uprostřed" in style.alignment or "Center" in style.alignment:
        align_code = 5
    elif "Horní" in style.alignment or "Top" in style.alignment:
        align_code = 8

    # BorderStyle: 1 = Outline + drop shadow, 3 = Opaque box
    border_style = 3 if style.box_background else 1

    margin_lr = max(40, int(style.play_res_x * 0.06))

    header = f"""[Script Info]
Title: Bionic Studio Reels Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {style.play_res_x}
PlayResY: {style.play_res_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ReelsStyle,{style.font_name},{style.font_size},{prim_ass},{high_ass},{out_ass},{back_ass},-1,0,0,0,100,100,0,0,{border_style},{style.outline_width},{style.shadow_depth},{align_code},{margin_lr},{margin_lr},{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    dialogue_lines = []
    pop_tag = r"{\fscx112\fscy112\t(0,75,\fscx100\fscy100)}" if style.pop_animation else ""

    for ch in chunks:
        words = ch.get("words", [])
        raw_text = ch.get("text", "").strip()
        if not raw_text:
            continue

        if style.karaoke_highlight and len(words) >= 1:
            for idx, w_info in enumerate(words):
                w_start = float(w_info["start"])
                w_end = float(words[idx + 1]["start"]) if (idx + 1 < len(words)) else float(ch["end"])
                if w_end <= w_start:
                    w_end = w_start + 0.06

                s_ass = seconds_to_ass_time(w_start)
                e_ass = seconds_to_ass_time(w_end)

                parts = []
                for j, w_item in enumerate(words):
                    word_str = w_item["word"].upper() if style.uppercase else w_item["word"]
                    word_escaped = _escape_ass_text(word_str)
                    if j == idx:
                        # Aktivní právě vyslovované slovo (barva highlight + mírné zvětšení)
                        parts.append(f"{{\\1c{high_ass}\\fscx106\\fscy106}}{word_escaped}{{\\1c{prim_ass}\\fscx100\\fscy100}}")
                    else:
                        parts.append(word_escaped)

                prefix = pop_tag if idx == 0 else ""
                line_text = prefix + " ".join(parts)
                dialogue_lines.append(f"Dialogue: 0,{s_ass},{e_ass},ReelsStyle,,0,0,0,,{line_text}")
        else:
            s_ass = seconds_to_ass_time(ch["start"])
            e_ass = seconds_to_ass_time(ch["end"])
            disp = raw_text.upper() if style.uppercase else raw_text
            disp_escaped = _escape_ass_text(disp)
            dialogue_lines.append(f"Dialogue: 0,{s_ass},{e_ass},ReelsStyle,,0,0,0,,{pop_tag}{disp_escaped}")

    return header + "\n".join(dialogue_lines) + "\n"


def render_style_preview_image(
    style: ReelsSubtitleStyle,
    sample_text: str = "TOHLE VIDEO JE BOMBA!",
    preview_size: Tuple[int, int] = (340, 210),
) -> Image.Image:
    """
    Vygeneruje PIL Image s živým náhledem Reels titulku (šachovnicové pozadí simulující průhlednost,
    obrys, stín, barva textu a zvýrazněné aktivní slovo).
    """
    w, h = preview_size
    img = Image.new("RGBA", (w, h), (15, 23, 42, 255))
    draw = ImageDraw.Draw(img)

    # Jemná šachovnice pro znázornění alfa kanálu (průhledného overlaye)
    tile = 16
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            if ((x // tile) + (y // tile)) % 2 == 0:
                draw.rectangle([x, y, x + tile, y + tile], fill=(30, 41, 59, 255))

    # Poměr velikosti písma vůči náhledu
    scaled_font_px = max(16, min(44, int(style.font_size * 0.36)))

    font_map = {
        "Arial Black": ["ariblk.ttf", "arialbd.ttf", "arial.ttf"],
        "Impact": ["impact.ttf", "arialbd.ttf"],
        "Segoe UI Bold": ["segoeuib.ttf", "segoeui.ttf"],
        "Trebuchet MS": ["trebucbd.ttf", "trebuc.ttf"],
        "Verdana": ["verdanab.ttf", "verdana.ttf"],
        "Tahoma": ["tahomabd.ttf", "tahoma.ttf"],
        "Franklin Gothic Medium": ["framd.ttf", "arialbd.ttf"],
        "Calibri": ["calibrib.ttf", "calibri.ttf"],
        "Consolas": ["consolab.ttf", "consola.ttf"],
        "Comic Sans MS": ["comicbd.ttf", "comic.ttf"],
    }
    candidates = font_map.get(style.font_name, ["arialbd.ttf", "segoeui.ttf"])
    pil_font = None
    win_fonts_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
    for fn in candidates:
        fp = os.path.join(win_fonts_dir, fn)
        if os.path.isfile(fp):
            try:
                pil_font = ImageFont.truetype(fp, scaled_font_px)
                break
            except Exception:
                pass
    if pil_font is None:
        pil_font = ImageFont.load_default()

    disp_text = sample_text.upper() if style.uppercase else sample_text
    words = disp_text.split()
    if not words:
        words = ["UKÁZKA", "TITULKŮ"]

    # Spočítat šířku celé řádky
    space_w = draw.textlength(" ", font=pil_font)
    word_widths = [draw.textlength(word, font=pil_font) for word in words]
    total_w = sum(word_widths) + space_w * max(0, len(words) - 1)

    # Vertikální pozice v náhledu
    if "Horní" in style.alignment:
        y_pos = int(h * 0.25)
    elif "Uprostřed" in style.alignment:
        y_pos = int(h * 0.50)
    else:
        y_pos = int(h * 0.72)

    x_start = max(12, int((w - total_w) / 2))

    # Pokud je zapnutý podkladový box
    if style.box_background:
        pad_x, pad_y = 12, 8
        draw.rounded_rectangle(
            [x_start - pad_x, y_pos - scaled_font_px // 2 - pad_y, x_start + total_w + pad_x, y_pos + scaled_font_px // 2 + pad_y],
            radius=8,
            fill=(0, 0, 0, 180),
        )

    prim_rgb = hex_to_rgb(style.primary_color)
    high_rgb = hex_to_rgb(style.highlight_color)
    out_rgb = hex_to_rgb(style.outline_color)
    stroke_w = max(0, min(6, int(round(style.outline_width * 0.45))))

    cur_x = x_start
    highlight_idx = min(1, len(words) - 1)  # Zvýraznit 2. slovo jako ukázku Karaoke efektu
    for i, (word, ww) in enumerate(zip(words, word_widths)):
        fill_col = high_rgb if (style.karaoke_highlight and i == highlight_idx) else prim_rgb
        # Stín
        if style.shadow_depth > 0 and not style.box_background:
            sh_off = max(1, min(4, style.shadow_depth))
            draw.text(
                (cur_x + sh_off, y_pos - scaled_font_px // 2 + sh_off),
                word,
                font=pil_font,
                fill=(0, 0, 0, 210),
            )
        draw.text(
            (cur_x, y_pos - scaled_font_px // 2),
            word,
            font=pil_font,
            fill=fill_col,
            stroke_width=stroke_w,
            stroke_fill=out_rgb,
        )
        cur_x += ww + space_w

    # Popisek rozlišení v rohu náhledu
    badge = f"{style.play_res_x}×{style.play_res_y} px"
    draw.rounded_rectangle([8, 8, 118, 28], radius=5, fill=(15, 23, 42, 210))
    draw.text((14, 11), badge, fill=(148, 163, 184, 255))

    return img


def render_reels_video_overlay(
    chunks: List[Dict[str, Any]],
    style: ReelsSubtitleStyle,
    output_path: str,
    export_mode: str,
    total_duration_sec: float,
    fps: int = 30,
    source_video_path: Optional[str] = None,
    trim_start_sec: Optional[float] = None,
    trim_end_sec: Optional[float] = None,
    reset_timeline_to_zero: bool = True,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bool, str]:
    """
    Vyrenderuje výsledné Reels titulky podle zvoleného režimu:
    1. 'Průhledné .MOV (Alpha kanál...' -> QuickTime Animation (qtrle, argb) s průhledným pozadím pro DaVinci Resolve V2 stopu
    2. 'Černé pozadí .MP4...' -> H.264 NVENC na černém pozadí (pro DaVinci Resolve Composite Mode -> Screen / Add)
    3. 'Zelené plátno .MP4...' -> H.264 NVENC na #00FF00 pozadí (pro DaVinci Resolve 3D Keyer / Delta Keyer)
    4. 'Přímé vypálení do videa...' -> Vypálí titulky přímo do zdrojového videa (včetně případného ořezu a změny rozlišení)
    5. 'Pouze soubor titulků .ASS + .SRT...' -> Uloží .ASS a .SRT soubory
    """
    if not chunks:
        return False, "Seznam titulků je prázdný."

    # Filtrovat a případně posunout časování podle trim_start_sec / trim_end_sec
    t_start = trim_start_sec if (trim_start_sec is not None and trim_start_sec > 0) else 0.0
    t_end = trim_end_sec if (trim_end_sec is not None and trim_end_sec > t_start) else total_duration_sec
    if t_end <= t_start:
        t_end = max(t_start + 1.0, max(ch["end"] for ch in chunks))

    active_chunks: List[Dict[str, Any]] = []
    for ch in chunks:
        if ch["end"] <= t_start or ch["start"] >= t_end:
            continue
        c_copy = {
            "start": max(t_start, float(ch["start"])),
            "end": min(t_end, float(ch["end"])),
            "text": ch["text"],
            "words": [],
        }
        if reset_timeline_to_zero and t_start > 0:
            c_copy["start"] -= t_start
            c_copy["end"] -= t_start

        for w in ch.get("words", []):
            ws = max(t_start, float(w["start"]))
            we = min(t_end, float(w["end"]))
            if reset_timeline_to_zero and t_start > 0:
                ws -= t_start
                we -= t_start
            c_copy["words"].append({"start": ws, "end": max(ws + 0.04, we), "word": w["word"]})

        active_chunks.append(c_copy)

    if not active_chunks:
        return False, "Ve zvoleném časovém rozmezí (Od – Do) se nenachází žádné titulky."

    render_duration = (t_end - t_start) if reset_timeline_to_zero else t_end
    render_duration = max(1.0, render_duration)

    # Uložit vždy doprovodný .ASS a .SRT soubor vedle výstupu
    base_no_ext = os.path.splitext(output_path)[0]
    ass_file_path = base_no_ext + ".ass"
    srt_file_path = base_no_ext + ".srt"

    ass_content = generate_ass_subtitle_content(active_chunks, style)
    with open(ass_file_path, "w", encoding="utf-8") as f_ass:
        f_ass.write(ass_content)

    # Vygenerovat také .SRT
    from ai_engine import format_timestamp_srt
    srt_blocks = []
    for idx, ch in enumerate(active_chunks, start=1):
        srt_blocks.append(
            f"{idx}\n{format_timestamp_srt(ch['start'])} --> {format_timestamp_srt(ch['end'])}\n"
            f"{ch['text'].upper() if style.uppercase else ch['text']}"
        )
    with open(srt_file_path, "w", encoding="utf-8") as f_srt:
        f_srt.write("\n\n".join(srt_blocks))

    if "Pouze soubor titulků" in export_mode:
        if progress_callback:
            progress_callback(1.0, "Hotovo! Soubory .ASS a .SRT byly uloženy.")
        return True, f"Titulky uloženy do:\n{ass_file_path}\n{srt_file_path}"

    # Pro FFmpeg `ass` filtr použijeme dočasný adresář s jednoduchým názvem `subtitles.ass` a cwd,
    # čímž 100% obejdeme jakékoliv problémy s dvojtečkami nebo diakritikou v cestách na Windows!
    ffmpeg = get_ffmpeg_exe()
    tmp_dir = tempfile.mkdtemp(prefix="bionic_reels_")
    tmp_ass_name = "subtitles.ass"
    tmp_ass_full = os.path.join(tmp_dir, tmp_ass_name)
    with open(tmp_ass_full, "w", encoding="utf-8") as f_tmp:
        f_tmp.write(ass_content)

    w_px = style.play_res_x
    h_px = style.play_res_y

    try:
        if "Průhledné .MOV" in export_mode:
            # Průhledné QuickTime Animation (qtrle) s alfa kanálem (argb) - nativně podporované v DaVinci Resolve Free i Studio!
            cmd = [
                ffmpeg, "-y", "-hide_banner",
                "-f", "lavfi",
                "-i", f"color=c=black@0.0:s={w_px}x{h_px}:r={fps}:d={render_duration:.2f},format=rgba",
                "-vf", f"ass=filename={tmp_ass_name}:alpha=1",
                "-c:v", "qtrle",
                "-pix_fmt", "argb",
                output_path,
            ]
        elif "Zelené plátno" in export_mode:
            cmd = [
                ffmpeg, "-y", "-hide_banner",
                "-f", "lavfi",
                "-i", f"color=c=0x00FF00:s={w_px}x{h_px}:r={fps}:d={render_duration:.2f}",
                "-vf", f"ass=filename={tmp_ass_name}",
                "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                output_path,
            ]
        elif "Černé pozadí" in export_mode:
            cmd = [
                ffmpeg, "-y", "-hide_banner",
                "-f", "lavfi",
                "-i", f"color=c=black:s={w_px}x{h_px}:r={fps}:d={render_duration:.2f}",
                "-vf", f"ass=filename={tmp_ass_name}",
                "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                output_path,
            ]
        else:
            # Přímé vypálení do zdrojového videa (Burn-in)
            if not source_video_path or not os.path.isfile(source_video_path):
                return False, "Pro přímé vypálení titulků do videa musí být vybrán zdrojový soubor."

            src_info = probe_media_info(source_video_path)
            cmd = [ffmpeg, "-y", "-hide_banner"]
            if t_start > 0 and reset_timeline_to_zero:
                cmd += ["-ss", f"{t_start:.2f}"]
            if trim_end_sec is not None and trim_end_sec > t_start and reset_timeline_to_zero:
                cmd += ["-to", f"{t_end:.2f}"]

            cmd += ["-i", source_video_path]

            if src_info.get("has_video"):
                # Přizpůsobit rozlišení s doplněním černých pruhů (pad) pokud se mění poměr stran a vypálit ASS
                vf_chain = (
                    f"scale={w_px}:{h_px}:force_original_aspect_ratio=decrease,"
                    f"pad={w_px}:{h_px}:(ow-iw)/2:(oh-ih)/2:black,"
                    f"ass=filename={tmp_ass_name}"
                )
                cmd += [
                    "-vf", vf_chain,
                    "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    output_path,
                ]
            else:
                # Pokud je zdrojem pouze audio soubor (MP3/AAC/WAV), vytvoříme tmavé video se zvukem a titulky!
                cmd = [
                    ffmpeg, "-y", "-hide_banner",
                    "-f", "lavfi", "-i", f"color=c=0x111827:s={w_px}x{h_px}:r={fps}:d={render_duration:.2f}",
                ]
                if t_start > 0 and reset_timeline_to_zero:
                    cmd += ["-ss", f"{t_start:.2f}"]
                if trim_end_sec is not None and trim_end_sec > t_start and reset_timeline_to_zero:
                    cmd += ["-to", f"{t_end:.2f}"]
                cmd += [
                    "-i", source_video_path,
                    "-vf", f"ass=filename={tmp_ass_name}",
                    "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    output_path,
                ]

        # Spuštění FFmpeg s cwd=tmp_dir a čtením progressu
        proc = subprocess.Popen(
            cmd,
            cwd=tmp_dir,
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
                return False, "Generování video overlaye bylo zrušeno."

            line = proc.stderr.readline()
            if not line and proc.poll() is not None:
                break

            if line:
                stderr_lines.append(line)
                t_match = time_pattern.search(line)
                if t_match and render_duration > 0 and progress_callback:
                    h, m, s = int(t_match.group(1)), int(t_match.group(2)), float(t_match.group(3))
                    cur_sec = h * 3600 + m * 60 + s
                    frac = min(0.99, max(0.0, cur_sec / render_duration))
                    sp_match = speed_pattern.search(line)
                    sp_str = f" ({sp_match.group(1)})" if sp_match else ""
                    progress_callback(frac, f"Renderuji Reels titulky: {int(frac * 100)} %{sp_str}")

        ret = proc.wait()
        if ret == 0:
            if progress_callback:
                progress_callback(1.0, "✅ Reels titulky a Video Overlay úspěšně vygenerovány!")
            return True, output_path
        else:
            err_tail = "".join(stderr_lines[-15:])
            return False, f"FFmpeg vrátil chybu ({ret}):\n{err_tail}"

    finally:
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
