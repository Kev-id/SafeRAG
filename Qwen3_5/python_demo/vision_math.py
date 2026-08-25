# ==============================================================================
# Copyright (C) 2026  (SafeRAG)  vision math helpers for Qwen3.5 image input.
#
# Pure numpy / PIL reimplementation of the Qwen2-VL image preprocessing chain,
# so the HTTP server can accept images WITHOUT torch / qwen_vl_utils /
# AutoProcessor.  The actual ViT encoder runs on the TPU via chat.forward_vit;
# these helpers only turn a decoded image into the exact pixel/position tensors
# forward_vit expects.
#
# Algorithm source of truth (transformers, Qwen2VLImageProcessorFast / Pil):
#   - smart_resize .. models/qwen2_vl/image_processing_pil_qwen2_vl.py:57
#   - patchify      .. models/qwen2_vl/image_processing_pil_qwen2_vl.py:152
#   - rot_pos / fast_pos_embed_interpolate / get_rope_index
#                    .. Qwen3_5/python_demo/pipeline.py
#
# This module never imports torch.  Testable standalone:  no chat.so needed.
# ==============================================================================

import base64
import io
import math

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Image loading / resizing / normalization  (replaces AutoProcessor)
# ---------------------------------------------------------------------------

def open_image(src):
    """Open an image from a local path or a base64 data URI. Returns RGB PIL image."""
    if isinstance(src, str) and src.startswith("data:"):
        _, b64 = src.split(",", 1)
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
    else:
        img = Image.open(src)
    if getattr(img, "is_animated", False):
        img.seek(0)  # GIF: take the first frame
    return img.convert("RGB")


def smart_resize(height, width, factor=28, min_pixels=56 * 56, max_pixels=14 * 14 * 4 * 1280):
    """Resize dimensions to suit Qwen2-VL:
    1. both are divisible by `factor`,
    2. total pixels stay within [min_pixels, max_pixels],
    3. aspect ratio kept as close as possible.
    Verbatim reimplementation of transformers.image_processing_pil_qwen2_vl.smart_resize.
    """
    if max(height, width) / min(height, width) > 200:
        raise ValueError(
            f"absolute aspect ratio must be smaller than 200, got {max(height, width) / min(height, width)}"
        )
    h_bar = round(height / factor) * factor
    w_bar = round(width / factor) * factor
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def resize_image(img, new_h, new_w):
    """BICUBIC resize (matches the processor's default resample)."""
    return img.resize((int(new_w), int(new_h)), Image.BICUBIC)


def normalize_chw(arr, image_mean, image_std):
    """rescale 1/255 + (x - mean) / std per channel. `arr` is float32 (C, H, W)."""
    arr = arr * (1.0 / 255.0)
    mean = np.asarray(image_mean, dtype=np.float32)[:, None, None]
    std = np.asarray(image_std, dtype=np.float32)[:, None, None]
    return (arr - mean) / std


def patchify_image(image, patch_size, merge_size, temporal_patch_size):
    """Split a normalized (C,H,W) image into the flat VIS patch layout that
    chat.forward_vit consumes: rows = (block_row, block_col, intra_row,
    intra_col), each row = channel planes [R, G, B] each patch_size^2,
    optionally replicated `temporal_patch_size` times (matches
    image_processing_pil_qwen2_vl.patchify).  Returns (pixel_values, grid_h,
    grid_w) where grid = resized dims // patch_size.

    Number of VIS patches returned is padded/ordered to grid_h * grid_w rows.
    """
    channel, resized_height, resized_width = np.asarray(image).shape
    grid_h, grid_w = resized_height // patch_size, resized_width // patch_size
    patches = image.reshape(
        channel,
        grid_h // merge_size,
        merge_size,
        patch_size,
        grid_w // merge_size,
        merge_size,
        patch_size,
    )
    # (grid_h/m, grid_w/m, m, m, C, ph, pw)  -- matches rot_pos() order
    patches = np.transpose(patches, (1, 4, 2, 5, 0, 3, 6))
    if temporal_patch_size != 1:
        patches = np.broadcast_to(
            patches[:, :, :, :, :, None, :, :],
            (*patches.shape[:5], temporal_patch_size, *patches.shape[5:]),
        )
    flatten = patches.reshape(grid_h * grid_w, channel * temporal_patch_size * patch_size * patch_size)
    return flatten, grid_h, grid_w


def load_and_prep_image(src, patch_size, merge_size, min_pixels, max_pixels,
                        image_mean, image_std, temporal_patch_size=1):
    """Full AutoProcessor-free single image pipeline.

    Returns (pixel_values, grid_thw) where grid_thw = [1, grid_h, grid_w]
    (grid units = resized_dims // patch_size).
    """
    img = open_image(src)
    height, width = img.size[1], img.size[0]
    factor = patch_size * merge_size
    new_h, new_w = smart_resize(height, width, factor, min_pixels, max_pixels)
    img = resize_image(img, new_h, new_w)

    arr = np.asarray(img, dtype=np.float32).transpose(2, 0, 1)  # (C, H, W)
    arr = normalize_chw(arr, image_mean, image_std)
    patches, grid_h, grid_w = patchify_image(arr, patch_size, merge_size, temporal_patch_size)
    return patches, (1, grid_h, grid_w)


# ---------------------------------------------------------------------------
# Rotational / positional indices for the ViT and the LLM  (pipeline.py, numpy)
# ---------------------------------------------------------------------------

def rot_pos(grid_thw, merge_size):
    """M-RoPE 2D position ids for one image: rows (block_row, block_col,
    intra_row, intra_col) -- must match patchify_image's row order."""
    num_frames, height, width = (int(x) for x in grid_thw)
    total_tokens = num_frames * height * width
    pos_ids = np.empty((total_tokens, 2), dtype=np.int32)

    merged_h, merged_w = height // merge_size, width // merge_size
    block_rows = np.arange(merged_h)
    block_cols = np.arange(merged_w)
    intra_row = np.arange(merge_size)
    intra_col = np.arange(merge_size)

    row_idx = block_rows[:, None, None, None] * merge_size + intra_row[None, None, :, None]
    col_idx = block_cols[None, :, None, None] * merge_size + intra_col[None, None, None, :]
    row_idx = np.broadcast_to(row_idx, (merged_h, merged_w, merge_size, merge_size))
    col_idx = np.broadcast_to(col_idx, (merged_h, merged_w, merge_size, merge_size))
    coords = np.stack((row_idx, col_idx), axis=-1).reshape(-1, 2)
    if num_frames > 1:
        coords = np.repeat(coords, num_frames, axis=0)
    pos_ids[:] = coords
    return pos_ids


def fast_pos_embed_interpolate(grid_thw, num_grid_per_side, merge_size):
    """Bilinear interpolation indices/weights from an arbitrary grid size to
    the pretrained `num_grid_per_side`^2 M-RoPE grid.  Returns (pos_ids,
    pos_weights) each shape (t*h*w, 4), matching pipeline.fast_pos_embed_interpolate."""
    t, h, w = (int(x) for x in grid_thw)
    m = merge_size

    h_idxs = np.linspace(0, num_grid_per_side - 1, h)
    w_idxs = np.linspace(0, num_grid_per_side - 1, w)
    h_idxs_floor = np.floor(h_idxs).astype(np.int64)
    w_idxs_floor = np.floor(w_idxs).astype(np.int64)
    h_idxs_ceil = np.clip(h_idxs_floor + 1, 0, num_grid_per_side - 1)
    w_idxs_ceil = np.clip(w_idxs_floor + 1, 0, num_grid_per_side - 1)

    dh = h_idxs - h_idxs_floor
    dw = w_idxs - w_idxs_floor

    base_h = h_idxs_floor * num_grid_per_side
    base_h_ceil = h_idxs_ceil * num_grid_per_side

    indices = [
        np.add.outer(base_h, w_idxs_floor).ravel(),
        np.add.outer(base_h, w_idxs_ceil).ravel(),
        np.add.outer(base_h_ceil, w_idxs_floor).ravel(),
        np.add.outer(base_h_ceil, w_idxs_ceil).ravel(),
    ]
    weights = [
        np.multiply.outer((1 - dh), (1 - dw)).ravel(),
        np.multiply.outer((1 - dh), dw).ravel(),
        np.multiply.outer(dh, (1 - dw)).ravel(),
        np.multiply.outer(dh, dw).ravel(),
    ]

    idx_tensor = np.stack(indices, axis=0).astype(np.int32)
    weight_tensor = np.stack(weights, axis=0).astype(np.float32)
    order = (1, 2, 4, 3, 5, 0)  # same permute as pipeline.py
    idx_tensor = idx_tensor.reshape(4, t, h // m, m, w // m, m).transpose(order).reshape(t * h * w, 4)
    weight_tensor = weight_tensor.reshape(4, t, h // m, m, w // m, m).transpose(order).reshape(t * h * w, 4)
    return idx_tensor, weight_tensor


def get_rope_index(input_ids, grid_thw, pad_id, vision_start_id, merge_size, is_video=False):
    """Build the 3-plane mRoPE position ids for the whole (already expanded)
    input.  numpy version of pipeline.get_rope_index.  Returns (3, 1, L) int32.
    `is_video=False` means pad_id is the image pad and each image reads its own
    grid row; videos reuse a single grid row (see pipeline.py get_rope_index)."""
    ids = np.asarray(input_ids)
    position_ids = np.ones((3, ids.shape[0], ids.shape[1]), dtype=np.int64)
    for b in range(ids.shape[0]):
        row = ids[b]
        vision_starts = np.flatnonzero(row == vision_start_id)
        image_nums = len(vision_starts)
        tokens = row.tolist()
        lists = []
        st = 0
        remain_images = image_nums
        gi = 0
        for _ in range(image_nums):
            if pad_id in tokens and remain_images > 0:
                ed_image = tokens.index(pad_id, st)
            else:
                ed_image = len(tokens) + 1
            if not is_video:
                t, h, w = (int(x) for x in grid_thw[gi])
            else:
                t, h, w = 1, int(grid_thw[0][1]), int(grid_thw[0][2])
            gi += 1
            remain_images -= 1
            ed = ed_image

            llm_grid_t, llm_grid_h, llm_grid_w = t, h // merge_size, w // merge_size
            text_len = ed - st
            st_idx = int(lists[-1].max()) + 1 if lists else 0
            lists.append(np.broadcast_to(np.arange(text_len)[None, :], (3, text_len)) + st_idx)

            t_index = np.broadcast_to(np.arange(llm_grid_t)[:, None], (llm_grid_t, llm_grid_h * llm_grid_w)).ravel()
            h_index = np.broadcast_to(np.arange(llm_grid_h)[None, :, None], (llm_grid_t, llm_grid_h, llm_grid_w)).ravel()
            w_index = np.broadcast_to(np.arange(llm_grid_w)[None, None, :], (llm_grid_t, llm_grid_h, llm_grid_w)).ravel()
            lists.append(np.stack((t_index, h_index, w_index)) + text_len + st_idx)

            st = ed + llm_grid_t * llm_grid_h * llm_grid_w

        if st < len(tokens):
            st_idx = int(lists[-1].max()) + 1 if lists else 0
            text_len = len(tokens) - st
            lists.append(np.broadcast_to(np.arange(text_len)[None, :], (3, text_len)) + st_idx)

        llm_positions = np.concatenate(lists, axis=1).reshape(3, -1)
        position_ids[:, b, :] = llm_positions
    return position_ids.astype(np.int32)


# ---------------------------------------------------------------------------
# Chat-template placeholder expansion
# ---------------------------------------------------------------------------

def expand_image_pads(input_ids, image_grid_thw, image_pad_id, merge_size):
    """Expand every single <|image_pad|> placeholder into grid_h*grid_w // 4
    tokens (one per merged LLM embedding slot), so the ViT output written by
    forward_vit lands exactly on the right positions.  Returns (1, L') int64."""
    ids = list(np.asarray(input_ids)[0])
    out = []
    gi = 0
    for tok in ids:
        out.append(tok)
        if tok == image_pad_id:
            _, gh, gw = (int(x) for x in image_grid_thw[gi])
            n_emb = max(1, (gh * gw) // (merge_size * merge_size))
            out.extend([image_pad_id] * (n_emb - 1))
            gi += 1
    return np.array([out], dtype=np.int64)