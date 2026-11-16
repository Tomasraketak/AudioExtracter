import os
import json
import struct
import shutil
import itertools
from pathlib import Path
from typing import Optional, Callable, Dict, Any, List, Tuple
import numpy as np


def find_local_bionic_whisper_gguf() -> Optional[str]:
    """
    Vyhledá na disku již stažený Whisper GGUF model z Bionic Studia / LM Studia
    (např. C:\\Users\\<user>\\.lmstudio\\models\\handy-computer\\whisper-large-v3-turbo-gguf\\whisper-large-v3-turbo-Q8_0.gguf).
    """
    home = Path.home()
    candidates = [
        home / ".lmstudio" / "models" / "handy-computer" / "whisper-large-v3-turbo-gguf" / "whisper-large-v3-turbo-Q8_0.gguf",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    # Obecnější prohledání ~/.lmstudio/models
    lms_models = home / ".lmstudio" / "models"
    if lms_models.is_dir():
        for root, _, files in os.walk(lms_models):
            for f in files:
                if "whisper" in f.lower() and f.lower().endswith(".gguf"):
                    return os.path.join(root, f)

    return None


class GGUFWhisperReader:
    """Čtečka metadat a tenzorů ze souboru Whisper GGUF (podporuje F32, F16 a Q8_0)."""

    def __init__(self, gguf_path: str):
        self.gguf_path = gguf_path
        self.f = open(gguf_path, "rb")
        self.metadata: Dict[str, Any] = {}
        self.tensors: Dict[str, Tuple[List[int], int, int]] = {}
        self.data_start: int = 0
        self._parse_header()

    def close(self):
        if self.f and not self.f.closed:
            self.f.close()

    def _read_str(self) -> str:
        n = struct.unpack("<Q", self.f.read(8))[0]
        return self.f.read(n).decode("utf-8", errors="replace")

    def _read_val(self, vtype: int) -> Any:
        if vtype == 8:
            return self._read_str()
        if vtype in (0, 1, 7):
            return struct.unpack("<B", self.f.read(1))[0]
        if vtype in (2, 3):
            return struct.unpack("<H", self.f.read(2))[0]
        if vtype == 4:
            return struct.unpack("<I", self.f.read(4))[0]
        if vtype == 5:
            return struct.unpack("<i", self.f.read(4))[0]
        if vtype == 6:
            return struct.unpack("<f", self.f.read(4))[0]
        if vtype in (10, 11):
            return struct.unpack("<Q", self.f.read(8))[0]
        if vtype == 12:
            return struct.unpack("<d", self.f.read(8))[0]
        if vtype == 9:
            at, al = struct.unpack("<IQ", self.f.read(12))
            return [self._read_val(at) for _ in range(al)]
        raise ValueError(f"Nepodporovaný GGUF metadata typ: {vtype}")

    def _parse_header(self):
        magic, ver, tc, kv = struct.unpack("<4sIQQ", self.f.read(24))
        if magic != b"GGUF":
            raise ValueError(f"Soubor {self.gguf_path} není platný GGUF soubor.")

        for _ in range(kv):
            k = self._read_str()
            vt = struct.unpack("<I", self.f.read(4))[0]
            self.metadata[k] = self._read_val(vt)

        for _ in range(tc):
            name = self._read_str()
            ndims = struct.unpack("<I", self.f.read(4))[0]
            dims = [struct.unpack("<Q", self.f.read(8))[0] for _ in range(ndims)]
            ttype = struct.unpack("<I", self.f.read(4))[0]
            offset = struct.unpack("<Q", self.f.read(8))[0]
            self.tensors[name] = (dims, ttype, offset)

        pos = self.f.tell()
        alignment = self.metadata.get("general.alignment", 32)
        self.data_start = (pos + alignment - 1) // alignment * alignment

    def read_tensor_f32(self, name: str) -> np.ndarray:
        if name not in self.tensors:
            raise KeyError(f"Tenzor '{name}' nebyl v GGUF souboru nalezen.")
        dims, ttype, offset = self.tensors[name]
        self.f.seek(self.data_start + offset)
        n_el = 1
        for d in dims:
            n_el *= d

        if ttype == 0:  # F32
            arr = np.frombuffer(self.f.read(n_el * 4), dtype=np.float32).copy()
        elif ttype == 1:  # F16
            arr = np.frombuffer(self.f.read(n_el * 2), dtype=np.float16).astype(np.float32)
        elif ttype == 8:  # Q8_0 (block size 32: 2B fp16 scale + 32x int8)
            n_blocks = n_el // 32
            raw = self.f.read(n_blocks * 34)
            dt = np.dtype([("d", "<f2"), ("qs", "i1", (32,))])
            blocks = np.frombuffer(raw, dtype=dt)
            arr = (blocks["d"][:, None].astype(np.float32) * blocks["qs"].astype(np.float32)).reshape(-1)
        else:
            raise ValueError(f"Nepodporovaný typ tenzoru {ttype} pro {name}")

        return arr.reshape(dims[::-1])


def convert_bionic_gguf_to_ct2(
    gguf_path: str,
    output_dir: str,
    quantization: str = "float16",
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> str:
    """
    Převede lokální Bionic Studio Whisper GGUF model (100% offline, bez jakéhokoliv stahování z internetu)
    do CTranslate2 adresáře pro okamžitou GPU/CPU akceleraci.
    """
    model_bin = os.path.join(output_dir, "model.bin")
    tok_json = os.path.join(output_dir, "tokenizer.json")
    preproc_json = os.path.join(output_dir, "preprocessor_config.json")
    if os.path.isfile(model_bin) and os.path.isfile(tok_json) and os.path.isfile(preproc_json):
        return output_dir

    from ctranslate2.specs import whisper_spec, common_spec
    from ctranslate2.converters import utils as ct2_utils

    if progress_callback:
        progress_callback(0.04, f"Načítám lokální model z Bionic Studia ({os.path.basename(gguf_path)})...")

    reader = GGUFWhisperReader(gguf_path)
    try:
        meta = reader.metadata
        enc_layers = int(meta.get("stt.whisper.encoder.n_layers", 32))
        enc_heads = int(meta.get("stt.whisper.encoder.n_heads", 20))
        dec_layers = int(meta.get("stt.whisper.decoder.n_layers", 4))
        dec_heads = int(meta.get("stt.whisper.decoder.n_heads", 20))
        num_mels = int(meta.get("stt.whisper.encoder.num_mel_bins", 128))

        spec = whisper_spec.WhisperSpec(
            num_encoder_layers=enc_layers,
            num_encoder_heads=enc_heads,
            num_decoder_layers=dec_layers,
            num_decoder_heads=dec_heads,
        )

        # 1. Encoder vstupní vrstvy
        spec.encoder.conv1.weight = reader.read_tensor_f32("enc.conv.0.weight")
        spec.encoder.conv1.bias = reader.read_tensor_f32("enc.conv.0.bias")
        spec.encoder.conv2.weight = reader.read_tensor_f32("enc.conv.1.weight")
        spec.encoder.conv2.bias = reader.read_tensor_f32("enc.conv.1.bias")
        spec.encoder.position_encodings.encodings = reader.read_tensor_f32("enc.pos_emb.weight")
        spec.encoder.layer_norm.gamma = reader.read_tensor_f32("enc.final_norm.weight")
        spec.encoder.layer_norm.beta = reader.read_tensor_f32("enc.final_norm.bias")

        def _make_linear(prefix: str, has_bias: bool = True) -> common_spec.LinearSpec:
            ls = common_spec.LinearSpec()
            ls.weight = reader.read_tensor_f32(f"{prefix}.weight")
            if has_bias and f"{prefix}.bias" in reader.tensors:
                ls.bias = reader.read_tensor_f32(f"{prefix}.bias")
            return ls

        # 2. Encoder bloky
        for i in range(enc_layers):
            if progress_callback and i % 4 == 0:
                frac = 0.05 + 0.45 * (i / enc_layers)
                progress_callback(frac, f"Připravuji lokální model z Bionic Studia (Encoder {i+1}/{enc_layers})...")

            layer_spec = spec.encoder.layer[i]
            p = f"enc.blocks.{i}"

            layer_spec.self_attention.layer_norm.gamma = reader.read_tensor_f32(f"{p}.norm_attn.weight")
            layer_spec.self_attention.layer_norm.beta = reader.read_tensor_f32(f"{p}.norm_attn.bias")

            q_lin = _make_linear(f"{p}.attn.q", True)
            k_lin = _make_linear(f"{p}.attn.k", False)
            v_lin = _make_linear(f"{p}.attn.v", True)
            ct2_utils.fuse_linear(layer_spec.self_attention.linear[0], [q_lin, k_lin, v_lin])

            out_lin = _make_linear(f"{p}.attn.out", True)
            layer_spec.self_attention.linear[1].weight = out_lin.weight
            layer_spec.self_attention.linear[1].bias = out_lin.bias

            layer_spec.ffn.layer_norm.gamma = reader.read_tensor_f32(f"{p}.norm_ffn.weight")
            layer_spec.ffn.layer_norm.beta = reader.read_tensor_f32(f"{p}.norm_ffn.bias")

            layer_spec.ffn.linear_0.weight = reader.read_tensor_f32(f"{p}.ffn.fc1.weight")
            layer_spec.ffn.linear_0.bias = reader.read_tensor_f32(f"{p}.ffn.fc1.bias")
            layer_spec.ffn.linear_1.weight = reader.read_tensor_f32(f"{p}.ffn.fc2.weight")
            layer_spec.ffn.linear_1.bias = reader.read_tensor_f32(f"{p}.ffn.fc2.bias")

        # 3. Decoder vstupní vrstvy + sdílená projekce
        if progress_callback:
            progress_callback(0.55, "Připravuji lokální model z Bionic Studia (Decoder & Slovník)...")

        tok_emb_weight = reader.read_tensor_f32("dec.token_embd.weight")
        spec.decoder.embeddings.weight = tok_emb_weight
        spec.decoder.projection.weight = tok_emb_weight
        spec.decoder.position_encodings.encodings = reader.read_tensor_f32("dec.pos_emb.weight")
        spec.decoder.layer_norm.gamma = reader.read_tensor_f32("dec.final_norm.weight")
        spec.decoder.layer_norm.beta = reader.read_tensor_f32("dec.final_norm.bias")

        # 4. Decoder bloky
        for i in range(dec_layers):
            layer_spec = spec.decoder.layer[i]
            p = f"dec.blocks.{i}"

            # Self-attention
            layer_spec.self_attention.layer_norm.gamma = reader.read_tensor_f32(f"{p}.norm_self.weight")
            layer_spec.self_attention.layer_norm.beta = reader.read_tensor_f32(f"{p}.norm_self.bias")
            sq = _make_linear(f"{p}.self_attn.q", True)
            sk = _make_linear(f"{p}.self_attn.k", False)
            sv = _make_linear(f"{p}.self_attn.v", True)
            ct2_utils.fuse_linear(layer_spec.self_attention.linear[0], [sq, sk, sv])
            sout = _make_linear(f"{p}.self_attn.out", True)
            layer_spec.self_attention.linear[1].weight = sout.weight
            layer_spec.self_attention.linear[1].bias = sout.bias

            # Cross-attention
            layer_spec.attention.layer_norm.gamma = reader.read_tensor_f32(f"{p}.norm_cross.weight")
            layer_spec.attention.layer_norm.beta = reader.read_tensor_f32(f"{p}.norm_cross.bias")
            cq = _make_linear(f"{p}.cross_attn.q", True)
            ck = _make_linear(f"{p}.cross_attn.k", False)
            cv = _make_linear(f"{p}.cross_attn.v", True)
            ct2_utils.fuse_linear(layer_spec.attention.linear[0], [cq])
            ct2_utils.fuse_linear(layer_spec.attention.linear[1], [ck, cv])
            cout = _make_linear(f"{p}.cross_attn.out", True)
            layer_spec.attention.linear[2].weight = cout.weight
            layer_spec.attention.linear[2].bias = cout.bias

            # FFN
            layer_spec.ffn.layer_norm.gamma = reader.read_tensor_f32(f"{p}.norm_ffn.weight")
            layer_spec.ffn.layer_norm.beta = reader.read_tensor_f32(f"{p}.norm_ffn.bias")
            layer_spec.ffn.linear_0.weight = reader.read_tensor_f32(f"{p}.ffn.fc1.weight")
            layer_spec.ffn.linear_0.bias = reader.read_tensor_f32(f"{p}.ffn.fc1.bias")
            layer_spec.ffn.linear_1.weight = reader.read_tensor_f32(f"{p}.ffn.fc2.weight")
            layer_spec.ffn.linear_1.bias = reader.read_tensor_f32(f"{p}.ffn.fc2.bias")

        # 5. Konfigurace a slovník
        tokens: List[str] = meta["tokenizer.ggml.tokens"]
        merges_raw: List[str] = meta["tokenizer.ggml.merges"]
        spec.register_vocabulary(tokens)

        config = spec.get_default_config()
        config.suppress_ids = [int(x) for x in meta.get("stt.whisper.suppress_tokens", [])]
        config.suppress_ids_begin = [int(x) for x in meta.get("stt.whisper.begin_suppress_tokens", [220, 50257])]
        # Jazykové tokeny 50259..50358 (100 jazyků)
        config.lang_ids = list(range(50259, 50359))
        config.alignment_heads = list(
            itertools.product(range(dec_layers // 2, dec_layers), range(dec_heads))
        )
        spec._config = config

        if progress_callback:
            progress_callback(0.75, "Optimalizuji tenzory pro RTX 5060 Ti (FP16)...")

        spec.validate()
        spec.optimize(quantization=quantization)

        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(output_dir, exist_ok=True)
        spec.save(output_dir)

        # 6. Uložit preprocessor_config.json
        preproc_cfg = {
            "chunk_length": int(meta.get("stt.frontend.chunk_length", 30)),
            "feature_extractor_type": "WhisperFeatureExtractor",
            "feature_size": num_mels,
            "hop_length": int(meta.get("stt.frontend.hop_length", 160)),
            "n_fft": int(meta.get("stt.frontend.n_fft", 400)),
            "n_samples": int(meta.get("stt.frontend.n_samples", 480000)),
            "nb_max_frames": int(meta.get("stt.frontend.nb_max_frames", 3000)),
            "padding_side": "right",
            "padding_value": 0.0,
            "processor_class": "WhisperProcessor",
            "return_attention_mask": False,
            "sampling_rate": int(meta.get("stt.frontend.sample_rate", 16000)),
        }
        with open(preproc_json, "w", encoding="utf-8") as f_pre:
            json.dump(preproc_cfg, f_pre, indent=2)

        # 7. Uložit plnohodnotný HuggingFace tokenizer.json přímo z GGUF metadat
        merges = [tuple(m.split(" ")) for m in merges_raw]
        vocab = {t: idx for idx, t in enumerate(tokens[:50257])}
        added_tokens = [
            {
                "id": idx,
                "content": tokens[idx],
                "single_word": False,
                "lstrip": False,
                "rstrip": False,
                "normalized": False,
                "special": True,
            }
            for idx in range(50257, len(tokens))
        ]
        tok_dict = {
            "version": "1.0",
            "truncation": None,
            "padding": None,
            "added_tokens": added_tokens,
            "normalizer": None,
            "pre_tokenizer": {
                "type": "ByteLevel",
                "add_prefix_space": False,
                "trim_offsets": True,
                "use_regex": True,
            },
            "post_processor": None,
            "decoder": {
                "type": "ByteLevel",
                "add_prefix_space": True,
                "trim_offsets": True,
                "use_regex": True,
            },
            "model": {
                "type": "BPE",
                "dropout": None,
                "unk_token": None,
                "continuing_subword_prefix": "",
                "end_of_word_suffix": "",
                "fuse_unk": False,
                "byte_fallback": False,
                "vocab": vocab,
                "merges": merges,
            },
        }
        with open(tok_json, "w", encoding="utf-8") as f_tok:
            json.dump(tok_dict, f_tok, ensure_ascii=False)

        if progress_callback:
            progress_callback(0.95, "Lokální model z Bionic Studia připraven!")

        return output_dir
    finally:
        reader.close()
