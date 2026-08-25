# ==============================================================================
# Copyright (C) 2026  (SafeRAG)  Qwen3.5 vision-capable inference (no torch).
#
# Wraps the same C++ `chat` module as pipeline_text.py, but additionally runs
# image input on the TPU ViT.  The image preprocessing and positional-index
# math live in vision_math.py (pure numpy/PIL); nothing here imports torch,
# qwen_vl_utils or AutoProcessor.
#
# CLI (same usage as pipeline.py; omit -p to enter interactive multi-turn chat):
#   python pipeline_vision_light.py -m ../path/model.bmodel -c ../config \
#       -p "这张图里有什么 @/path/image.png"
#
# The class API mirrors server.py's run_chat so the HTTP layer can reuse it
# verbatim later:
#   m = Qwen3_5(args)
#   text = m.run_image([{"role":"user","content":[{"type":"text","text":"..."},
#                        {"type":"image_url","image_url":{"url":"data:..."}}]}])
#
# NOTE on --vision_t: the bmodel's ViT input width (VIT_DIMS) is not exposed
# by chat.cpp.  VIT_DIMS == 3 * vision_t * 16 * 16, so vision_t is 1 if the
# encoder reads 768-d features per image patch, or 2 if it reads 1536-d.
# Transformers' current fast image processor emits 1536-d (temporal_patch_size=2)
# rows even for a single image (verified 1:1 against Qwen2VLImageProcessorPil),
# so --vision_t defaults to 2.  forward_vit's C-side assert fails loudly if the
# value mismatches your bmodel -- if so, retry with --vision_t 1.
# ==============================================================================

import argparse
import json
import math
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat  # noqa: E402

from transformers import AutoTokenizer  # noqa: E402

from vision_math import (  # noqa: E402
    expand_image_pads,
    fast_pos_embed_interpolate,
    get_rope_index,
    load_and_prep_image,
    rot_pos,
)


class Qwen3_5:
    def __init__(self, args):
        self.device = args.devid

        # model
        self.model = chat.Qwen3_5()
        self.model.init(self.device, args.model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(args.config_path, trust_remote_code=True)

        # special ids
        self.ID_IM_END = self.tokenizer.convert_tokens_to_ids("<|im_end|>")
        self.ID_IMAGE_PAD = self.tokenizer.convert_tokens_to_ids("<|image_pad|>")
        self.ID_VISION_START = self.tokenizer.convert_tokens_to_ids("<|vision_start|>")

        # vision geometry -- read from the shipped configs, no magic numbers
        cfg_path = os.path.join(args.config_path, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            qcfg = json.load(f)
        vcfg = qcfg.get("vision_config", {})
        self.patch_size = vcfg.get("patch_size", 16)
        self.spatial_merge_size = vcfg.get("spatial_merge_size", 2)
        n_pos = vcfg.get("num_position_embeddings", 2304)
        self.num_grid_per_side = int(round(math.sqrt(n_pos)))

        pre_path = os.path.join(args.config_path, "preprocessor_config.json")
        with open(pre_path, "r", encoding="utf-8") as f:
            pcfg = json.load(f)
        self.image_mean = pcfg.get("image_mean", [0.5, 0.5, 0.5])
        self.image_std = pcfg.get("image_std", [0.5, 0.5, 0.5])

        # resize bounds (same defaults as pipeline.py image_message)
        self.MIN_PIXELS = 4 * 32 * 32
        self.MAX_PIXELS = self.model.MAX_PIXELS

        # temporal dimension of the ViT patch features; transforms emits 1536-d
        # (vision_t=2) rows even for a single image -- see module docstring
        self.vision_t = int(getattr(args, "vision_t", 2))

        self.support_history = self.model.support_history
        self.max_posid = 0
        self.history_max_posid = 0

    def __del__(self):
        self.model.deinit()

    # ------------------------------------------------------------------
    # Input parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_image_items(messages):
        """Yield the image source string of every image item, in order."""
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, (list, tuple)):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image_url" and isinstance(item.get("image_url"), dict):
                    yield item["image_url"].get("url")
                elif isinstance(item.get("image"), str):
                    yield item["image"]

    def _collect_visuals(self, messages):
        """Decode every image into (pixel_values, image_grid_thw)."""
        pixel_rows, grids = [], []
        for src in self._iter_image_items(messages):
            patches, grid_thw = load_and_prep_image(
                src,
                self.patch_size,
                self.spatial_merge_size,
                self.MIN_PIXELS,
                self.MAX_PIXELS,
                self.image_mean,
                self.image_std,
                self.vision_t,
            )
            pixel_rows.append(patches)
            grids.append(grid_thw)
        if not pixel_rows:
            return None, None
        pixel_values = np.concatenate(pixel_rows, axis=0)
        image_grid_thw = np.array(grids, dtype=np.int64)
        return pixel_values, image_grid_thw

    # ------------------------------------------------------------------
    # Forward helpers
    # ------------------------------------------------------------------

    def forward_prefill(self, position_ids):
        if self.model.history_length == 0 or not self.support_history:
            self.history_max_posid = 0
            return self.model.forward_first(position_ids)
        self.max_posid += self.history_max_posid
        position_ids = position_ids + self.history_max_posid
        return self.model.forward_first(position_ids)

    def _vit_process(self, input_ids, pixel_values, image_grid_thw):
        """Run the TPU ViT for each image and splice its embeddings into the
        text-embedding buffer, mirroring pipeline.vit_process_image."""
        vit_token_list = np.flatnonzero(input_ids[0] == self.ID_VISION_START)
        pre_patches = 0
        for idx, vit_offset in enumerate(vit_token_list):
            grid_thw = image_grid_thw[idx]
            num_patches = int(np.prod(grid_thw))
            hidden_states = pixel_values[pre_patches:pre_patches + num_patches]
            pos_ids = rot_pos(grid_thw, self.spatial_merge_size)
            pos_idx, pos_weight = fast_pos_embed_interpolate(
                grid_thw, self.num_grid_per_side, self.spatial_merge_size
            )
            self.model.forward_vit(
                hidden_states.astype(np.float32),
                pos_ids.astype(np.int32),
                pos_idx.astype(np.int32),
                pos_weight.astype(np.float32),
                np.asarray(grid_thw, dtype=np.int32),
                int(vit_offset) + 1,
            )
            pre_patches += num_patches

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run_image(self, messages, max_tokens=None, clear_history=True, verbose=False):
        """Stateless single-turn image+text inference (mirrors server.run_chat).

        `messages` is OpenAI-format; content may be a str or a list of
        {type:text} / {type:image_url,image_url:{url:...}} items.  Returns the
        assistant's text with <think> blocks stripped.
        """
        if not messages:
            raise ValueError("messages must not be empty")
        _t_start = time.time()
        pixel_values, image_grid_thw = self._collect_visuals(messages)
        _t_prep = time.time()
        has_vision = pixel_values is not None

        if clear_history:
            self.model.clear_history()
            self.history_max_posid = 0

        # Render the template to a plain string first, then encode it.  The
        # older transformers on the TPU box cannot tokenize=True over messages
        # whose content carries image dicts (it feeds non-str into
        # encode_batch); the two-step form works on every version.
        rendered = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        tokenized = self.tokenizer(rendered, add_special_tokens=False, return_tensors="np")
        input_ids = np.asarray(tokenized["input_ids"])
        if has_vision:
            input_ids = expand_image_pads(input_ids, image_grid_thw, self.ID_IMAGE_PAD,
                                          self.spatial_merge_size)
        token_len = input_ids.shape[1]

        max_input = self.model.SEQLEN if self.model.support_history else self.model.MAX_INPUT_LENGTH
        if token_len > max_input:
            raise ValueError(
                f"Input length {token_len} exceeds maximum {max_input}"
            )
        # multi-turn guard (same as pipeline.run_once): drop history before it
        # would overflow the KV cache.
        if not clear_history and self.support_history:
            if (token_len + self.model.history_length > self.model.SEQLEN - 128) or \
               (self.model.history_length > self.model.PREFILL_KV_LENGTH):
                print("Warning: History is full, clearing it to continue.")
                self.model.clear_history()
                self.history_max_posid = 0

        # ---- prefill ----
        self.model.forward_embed(input_ids.astype(np.int32))
        _t_embed = time.time()
        if has_vision:
            self._vit_process(input_ids, pixel_values, image_grid_thw)
            _t_vit = time.time()
            rope = get_rope_index(input_ids, image_grid_thw, self.ID_IMAGE_PAD,
                                  self.ID_VISION_START, self.spatial_merge_size)
            position_ids = rope[:, 0, :]  # (3, L); NOT rope[0], which is one plane only
            self.max_posid = int(position_ids.max())
        else:
            position_ids = np.tile(np.arange(token_len), 3).astype(np.int32)
            self.max_posid = token_len - 1
        token = self.forward_prefill(position_ids.astype(np.int32).reshape(-1))
        _t_prefill = time.time()

        # ---- autoregressive decode ----
        full_word_tokens = []
        text = ""
        tok_num = 0
        while (
            token != self.ID_IM_END
            and self.model.history_length < self.model.SEQLEN
            and (max_tokens is None or tok_num < max_tokens)
        ):
            full_word_tokens.append(token)
            word = self.tokenizer.decode(full_word_tokens, skip_special_tokens=True)
            if "�" not in word:
                if len(full_word_tokens) == 1:
                    pre_word = word
                    word = self.tokenizer.decode(
                        [token, token], skip_special_tokens=True
                    )[len(pre_word):]
                text += word
                if verbose:
                    sys.stdout.write(word)
                    sys.stdout.flush()
                full_word_tokens = []
            self.max_posid += 1
            position_ids = np.array(
                [self.max_posid, self.max_posid, self.max_posid], dtype=np.int32
            )
            token = self.model.forward_next(position_ids)
            tok_num += 1

        self.history_max_posid = self.max_posid + 2
        _t_end = time.time()
        self.last_stats = {
            "prep_s": _t_prep - _t_start,   # image decode/resize/normalize + tokenize
            "embed_s": _t_embed - _t_prep,  # embedding (TPU)
            "vision_s": _t_vit - _t_embed if has_vision else 0.0,  # ViT (TPU)
            "prefill_s": _t_prefill - (_t_vit if has_vision else _t_embed),  # LLM prefill -> first token
            "ttft_s": _t_prefill - _t_start,
            "tokens": tok_num,
            "gen_s": _t_end - _t_prefill,
        }
        if verbose:
            s = self.last_stats
            sys.stdout.write(
                f"\n[prep {s['prep_s']:.2f}s | embed {s['embed_s']:.2f}s"
                f" | vit {s['vision_s']:.2f}s | lm_prefill {s['prefill_s']:.2f}s"
                f" | TTFT {s['ttft_s']:.2f}s | decode {s['tokens']}tok/{s['gen_s']:.2f}s]\n"
            )
            sys.stdout.flush()
        return self._strip_thinking(text)

    def chat(self, max_tokens=None):
        """Interactive multi-turn chat (mirrors pipeline.chat).  Attach an image
        with @<path>, read a prompt file with @<path-to>.txt/.md; /clear, /exit."""
        print("""\n=================================================================
1. If you want to quit, please enter one of [/q, /quit, /exit]
2. To create a new chat session, please enter one of [/clear, /new]
3. To ask about an image, include @<path> in your question
4. To use the contents of a .txt or .md file as your question, include @<path>
=================================================================""")
        while True:
            input_str = input("\nQuestion: ")
            if input_str in ["/exit", "/q", "/quit"]:
                break
            if input_str in ["/clear", "/new", "/c"]:
                print("New chat session created.")
                self.model.clear_history()
                self.history_max_posid = 0
                continue
            prompt, media = _extract_media(input_str)
            content = []
            if media:
                content.append({"type": "image", "image": media})
            if prompt:
                content.append({"type": "text", "text": prompt})
            if not content:
                continue
            messages = [{"role": "user", "content": content}]
            self.run_image(messages, max_tokens=max_tokens, clear_history=False, verbose=True)
            print()

    @staticmethod
    def _strip_thinking(text):
        """Remove <think>...</think> blocks from model output."""
        return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# CLI (pipeline.py-compatible: -p "prompt @image.png")
# ---------------------------------------------------------------------------

def _read_prompt_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().rstrip("\r\n")
    except OSError as e:
        print(f"Cannot open prompt file [ {path} ]: {e}")
        sys.exit(1)


def _extract_media(input_str):
    media_paths, text_tokens = [], []
    for t in input_str.split():
        if t.startswith("@") and len(t) > 1:
            path = t[1:]
            if path.lower().endswith((".txt", ".md")):
                text_tokens.append(_read_prompt_file(path))
            else:
                media_paths.append(path)
        else:
            text_tokens.append(t)
    if len(media_paths) > 1:
        print("Only one media file is supported, using: {}".format(media_paths[0]))
    return " ".join(text_tokens), (media_paths[0] if media_paths else "")


def main(args):
    model = Qwen3_5(args)
    if args.prompt is None:
        model.chat(max_tokens=args.max_tokens)
        return

    prompt, media = _extract_media(args.prompt)
    content = []
    if media:
        content.append({"type": "image", "image": media})
    if prompt:
        content.append({"type": "text", "text": prompt})
    if not content:
        content.append({"type": "text", "text": ""})
    messages = [{"role": "user", "content": content}]

    start = time.time()
    model.run_image(messages, max_tokens=args.max_tokens, verbose=True)
    elapsed = time.time() - start
    print(f"\n[elapsed {elapsed:.2f}s]")
    print(f"\n[elapsed {elapsed:.2f}s]")
    if model.support_history:
        print(f"Total Tokens: {model.model.history_length}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Qwen3.5 vision+text inference (no torch)")
    parser.add_argument("-m", "--model_path", type=str, required=True,
                        help="path to the bmodel file")
    parser.add_argument("-c", "--config_path", type=str, default="../config",
                        help="path to the processor config directory")
    parser.add_argument("-d", "--devid", type=int, default=0, help="TPU device ID")
    parser.add_argument("-p", "--prompt", type=str, default=None,
                        help="prompt text; attach an image with @<path>")
    parser.add_argument("--max_tokens", type=int, default=None,
                        help="stop after this many generated tokens")
    parser.add_argument("--vision_t", type=int, default=2,
                        help="ViT patch feature temporal dim: 2 (1536-d, default) or 1 (768-d)")
    args = parser.parse_args()
    main(args)
