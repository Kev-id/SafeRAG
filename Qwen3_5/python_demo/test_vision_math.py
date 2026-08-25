"""Regression tests for vision_math.py -- pure numpy/PIL, no chat.so / TPU.

Run from the python_demo dir with any python that has numpy + PIL:
    python test_vision_math.py
Exit code 0 on success, 1 on failure (also usable via pytest).
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vision_math as vm  # noqa: E402

_passed = 0
_failed = 0


def expect(cond, label):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}")


def test_smart_resize():
    print("smart_resize")
    # 640x480 with factor 32, bounds roughly matching the model
    h, w = vm.smart_resize(640, 480, factor=32, min_pixels=4096, max_pixels=1700000)
    expect((h, w) == (640, 480), "640x480 stays 640x480")
    h, w = vm.smart_resize(1024, 768, factor=32, min_pixels=4096, max_pixels=800000)
    expect(h % 32 == 0 and w % 32 == 0, f"multiple of factor: {h}x{w}")
    expect(h * w <= 800000, f"within max_pixels: {h * w} <= 800000")
    h, w = vm.smart_resize(32, 32, factor=32, min_pixels=65536, max_pixels=2000000)
    expect(h >= 32 and w >= 32 and h * w >= 65536, f"scales up past min: {h}x{w}")
    try:
        vm.smart_resize(10000, 10, factor=32, min_pixels=4096, max_pixels=2000000)
        expect(False, "extreme aspect ratio raises")
    except ValueError:
        expect(True, "extreme aspect ratio raises")


def _flat(patch_row, ch, ph, pw, patch_size):
    """flat slot of a (ch, ph, pw) triplet within one merged patch's feature row."""
    return ch * (patch_size * patch_size) + ph * patch_size + pw


def test_patchify_order():
    print("patchify layout ordering")
    # 64x64 RGB -> grid 4x4, merge 2 -> 16 merged patch blocks, each row = 3*256
    arr = np.zeros((3, 64, 64), dtype=np.float32)
    arr[0, 0, 0] = 1.0    # R, pixel (0,0)
    arr[2, 63, 63] = 9.0  # B, pixel (63,63)
    pv, gh, gw = vm.patchify_image(arr, patch_size=16, merge_size=2, temporal_patch_size=1)
    expect((gh, gw) == (4, 4), f"grid = {gh}x{gw}")
    expect(pv.shape == (16, 3 * 16 * 16), f"pixel_values shape {pv.shape}")

    # block(0,0): R(0,0) is at flat patch row 0 (bh,bw,mh,mw = 0,0,0,0), slot (R,0,0)
    expect(pv[0, _flat(0, 0, 0, 0, 16)] == 1.0, "R(0,0) at block0 slot (R,0,0)")

    # Flat patch row layout is a raster over (bh,bw,mh,mw); grid (3,3) after
    # merge has bh=1,bw=1,mh=1,mw=1 -> row = 1*8+1*4+1*2+1 = 15; B(63,63) sits at
    # slot (ch=2, ph=15, pw=15) = 2*256+255 = 767.
    expect(pv[15, _flat(0, 2, 15, 15, 16)] == 9.0, "B(63,63) at row15 slot (B,15,15)")


def test_rot_pos():
    print("rot_pos")
    g = (1, 2, 4)  # h=2,w=4 -> 8 tokens, merge into 1x2 blocks
    pos = vm.rot_pos(g, 2)
    expect(pos.shape == (8, 2), f"rot_pos shape {pos.shape}")
    expect(pos.dtype == np.int32, "rot_pos int32")
    # first token: block(0,0), intra(0,0) -> (0, 0)
    expect(tuple(pos[0]) == (0, 0), "first token (0,0)")
    # last token: block(0,1), intra(1,1) -> (1, 3)
    expect(tuple(pos[-1]) == (1, 3), f"last token {tuple(pos[-1])}")


def test_fast_interp():
    print("fast_pos_embed_interpolate")
    idx, wgt = vm.fast_pos_embed_interpolate((1, 2, 4), 48, 2)
    expect(idx.shape == (8, 4) and wgt.shape == (8, 4), f"shapes idx={idx.shape} wgt={wgt.shape}")
    expect(wgt.dtype == np.float32 and idx.dtype == np.int32, "dtypes")
    # weights for 4 corners sum to 1 per row
    expect(np.allclose(wgt.sum(axis=1), 1.0, atol=1e-5), "corner weights sum to 1")


def test_rope_index():
    print("get_rope_index")
    # ids: [0, vision_start, image_pad, image_pad, 9, 10]  (1 image, hw/4=2 pads -> gh*gw=8 => 2 merged)
    ids = np.array([[0, 248053, 248056, 248056, 9, 10]])
    grid = np.array([[1, 4, 8]])  # gh=4, gw=8 -> gh*gw=32 -> merged 8; but pads only 2 -> mismatch avoided
    # Use a consistent config: gh*gw//4 must equal number of pads. Here we keep pads=2 and grid tiny.
    grid = np.array([[1, 2, 4]])  # gh*gw=8, merged=2
    rope = vm.get_rope_index(ids, grid, 248056, 248053, 2)
    expect(rope.shape == (3, 1, 6), f"rope shape {rope.shape}")
    expect(rope.dtype == np.int32, "rope int32")
    # text before image: 2 tokens [0, vision_start] get 0,1 in every plane; image segment
    # (merged length 2) gets t/h/w indices offset by text_len+st_idx (matches pipeline.py).
    expect(int(rope[0, 0, 0]) == 0 and int(rope[0, 0, 1]) == 1, "text tokens get 0,1")
    # image plane0 (t_index) after text: offset by text_len=2 -> both are 2
    expect(int(rope[0, 0, 2]) == 2 and int(rope[0, 0, 3]) == 2, "image t_index offset by text_len")
    # w plane differentiates the two merged cols -> 2,3
    expect(int(rope[2, 0, 2]) == 2 and int(rope[2, 0, 3]) == 3, "image w_index 2,3")


def test_expand_pads():
    print("expand_image_pads")
    ids = np.array([[0, 248053, 248056, 9]])
    grid = np.array([[1, 4, 4]])  # gh=4,gw=4 -> 16 -> merged 4
    out = vm.expand_image_pads(ids, grid, 248056, 2)
    n_pads = int((out[0] == 248056).sum())
    expect(n_pads == 4, f"expanded to {n_pads} pads (expect 4)")
    expect(out.shape[1] == 1 + 1 + 4 + 1, f"final length {out.shape[1]} (expect 7)")


if __name__ == "__main__":
    test_smart_resize()
    test_patchify_order()
    test_rot_pos()
    test_fast_interp()
    test_rope_index()
    test_expand_pads()
    print(f"\npassed={_passed} failed={_failed}")
    sys.exit(1 if _failed else 0)
