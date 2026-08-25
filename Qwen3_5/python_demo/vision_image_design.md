# 设计：server.py 图片推理接入（无 torch 方案）

> 状态：待审阅
> 目标：让现有 OpenAI 兼容服务 `server.py` 支持图片输入（base64 data URI 或本地路径），在不引入 torch / qwen_vl_utils 的前提下。
> 原理：ViT 已经编译进 bmodel，由 `chat.cpp` 的 `forward_vit` 在 TPU 上执行；Python 侧只需要自己完成「图像解码→切 patch→归一化」和「M-RoPE / mRoPE 位置索引」两段预处理。

---

## 1. 依赖矩阵

| 包 | 现状 | 结论 |
|---|---|---|
| `transformers` | 已用（pipeline_text 的 `AutoTokenizer`） | **保留**；tokenizer + chat template 不需要 torch |
| `numpy` | 现网已用 | 保留 |
| `pybind11` `chat.so` | 已编译（aarch64-linux） | 保留，不改 C++ |
| `pillow` (PIL) | 未装 | **新增唯一依赖**：图像解码 / resize |
| `torch` | —— | **不需要**，视觉预处理全部 numpy 手写 |
| `qwen_vl_utils` / `AutoProcessor` | —— | 不需要（它们的逻辑被手写替代） |

> 为什么 tokenizer 不需要 torch：`transformers` 的 `AutoTokenizer` 底层是 rust 的 `tokenizers` 库，纯 CPU；`pipeline_text.py` 首行注释已声明"Text-only variant drops torch"，证明该环境 tokenizer 可跑。视觉部分唯一的重依赖是 Qwen2VL 的 `Qwen2VLImageProcessorFast`，它 `import torch`，必须替换。

---

## 2. 文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `Qwen3_5/python_demo/pipeline_vision_light.py` | **新建** | 轻量多模态推理类，`AutoTokenizer` + numpy + PIL，impl 视觉链路 |
| `Qwen3_5/python_demo/server.py` | **改** | schema 支持 `content` 数组；新增视觉路由；图片 base64 解析 |
| `Qwen3_5/python_demo/vision_image_design.md` | 新建 | 本文档 |
| （可选）`Qwen3_5/python_demo/tests/test_vision_math.py` | 新建 | 纯数学单测（见 §6） |

> 不能直接复用 `pipeline.py`：其顶层 `import torch`、`import qwen_vl_utils`（line 11-12），import 即炸。必须新建一个从 `pipeline_text.py` 派生、补充视觉段的 light 类。

---

## 3. 新模块 `pipeline_vision_light.py` 设计

### 3.1 类结构（对齐 `pipeline_text.Qwen3_5`）

继承原有字段：`model`（c++ chat 对象）、`tokenizer`、`ID_IM_END`、`support_history`、`max_posid`、`history_max_posid`。
新增字段（从 `config.json` / bmodel 读取）：

| 字段 | 值来源 |
|---|---|
| `ID_VISION_START` | `tokenizer.convert_tokens_to_ids('<|vision_start|>')` = 248053 |
| `ID_IMAGE_PAD` | = 248056（`config.json "image_token_id"`） |
| `ID_VISION_END` | = 248054 |
| `spatial_merge_size` | 2（vision_config） |
| `num_grid_per_side` | 48（`vision_config.num_position_embeddings=2304` → 48²，注意：**必须读配置，不能写死**，若该字段随模型变化会错位码） |
| `patch_size` | 16（preprocessor_config） |
| `MAX_PIXELS` | `self.model.MAX_PIXELS`（bmodel 给出，chat.cpp:308） |
| `image_mean/image_std` | `[0.5,0.5,0.5]`（preprocessor_config） |
| `mrope_section` | `[11,11,10]`、`mrope_interleaved=true`（config.json text_config.rope_parameters） |

### 3.2 图像解码与张量化（替代 AutoProcessor）

新增静态工具 `_numpy_visual_prep(messages)`：

```
对每个含 image 的 content item：
  1. base64 data URI → bytes        「data:image/png;base64,xxx」去掉前缀；或本地路径直接 open
  2. PIL 解码 → RGB（GIF/多帧取第一帧）
  3. smart_resize(h, w, patch_size=16,
                  min_pixels=4*32*32, max_pixels=MAX_PIXELS)  → 16 的倍数且保长宽比
  4. (x/255 - 0.5) / 0.5 归一化浮点
  5. 切成 patch_size×patch_size 不重叠块，每块展平
     → pixel_values 行序 = 行优先扫过 16×16 块，块内排列 = [R16×16, G16×16, B16×16]
     → 形状 (hw, 768)，hw = 图宽/16 × 图高/16
  6. 记录 image_grid_thw = [1, h/16, w/16]
返回: (pixel_values_total = concat 所有图, image_grid_thw_list)
```

**smart_resize 必须复刻 `Qwen2VLImageProcessorFast` 的算法**（Qwen2.5-VL smart_resize）：从原图尺寸出发二分/翻倍缩放，使 `总像素 ∈ [min_pixels, max_pixels]`，最后取整到 patch_size 的倍数；超长宽比（>200）抛错。**这是正确性第一大风险点**，见 §6 对拍。

### 3.3 视觉推理编排（对齐 `pipeline.py run_once` 的 image 分支）

```
def run_image(messages, max_tokens):
  (#a) 重构一条消息模板：每个 image item → chat_template 渲染出
       <|vision_start|><|image_pad|><|vision_end|>（template render_content，见 chat_template.jinja:18）
  (#b) tokenizer.apply_chat_template(tokenize=True) → input_ids，此时每图只有 1 个 image_pad
  (#c) <关键> image_pad 展开：对每张图，在其 vision_start 之后插入 (hw/4 - 1) 个 ID_IMAGE_PAD，
       使该图视觉占位 token 数 = hw/4（= merge 后视觉 embedding 数）。返回 numpy input_ids (1, L)
  (#d) token_len 校验（<= SEQLEN 或 MAX_INPUT_LENGTH，同现有 run_chat）
  (#e) model.forward_embed(input_ids)
  (#f) vit_process_image：
        按 input_ids 中 vision_start 出现顺序，逐图调 model.forward_vit(
             pixel_values[i], rot_pos(grid_thw_i),
             *fast_pos_embed_interpolate(grid_thw_i), grid_thw_i, vit_offset)
        vit_offset = 该图 vision_start 位置 + 1
  (#g) position_ids = get_rope_index(input_ids, image_grid_thw, ID_IMAGE_PAD)  （numpy 重写）
       的 float/numpy 版本；max_posid = position_ids.max()
  (#h) token = forward_prefill(position_ids)
  (#i) 自回归 decode：完全复用现有 run_chat 的循环（word-merge、im_end、max_tokens）
  (#j) 返回 _strip_thinking(text)
```

### 3.4 三个 numpy 重写函数（对照 `pipeline.py` torch 版）

| 函数 | 原 torch 实现 | numpy 重写要点 |
|---|---|---|
| `rot_pos(grid_thw)` | pipeline.py:98-129 | reshape/broadcast/hstack 均为 numpy 一等公民；`coords` 用 `np.stack`，多帧用 `np.repeat`。输入输出 int32，与 chat.cpp:438 断言对齐 |
| `fast_pos_embed_interpolate(grid_thw)` | pipeline.py:131-175 | 双线性插值索引/权重：`np.linspace/np.floor/np.stack`，最后两次 `transpose+reshape` 用 `np.transpose(0,2,4,1,3,5)` 对应 toractual permute(1,2,4,3,5,0) 需仔细换算 axis；形状断言 hw×4、hw×4 |
| `get_rope_index(input_ids, grid_thw, pad_id)` | pipeline.py:202-262 | `np.where`/`np.flatnonzero` 找 vision_start；对每个 image 段构造 `t/h/w_index` 的 `arange + expand`，拼到 (3, L) int32。除最后一维外无 tricky，逐行为 torch 直接翻译 |

> 三者都不含 autograd/CUDA，`pipeline.py` 里用的 torch 操作（arange/stack/expand/cat/prod/where）全部在 numpy 有 1:1 对应。

### 3.5 image_pad 展开的数学一致性（最重要校验点）

三处必须同时为 `hw/4`，否则视觉 embedding 错位：

| # | 位置 | 数量 |
|---|---|---|
| 1 | input_ids 中该图 image_pad 占位 token 数 | hw/4 |
| 2 | `get_rope_index` 中该图 image 段 position 长度（`llm_grid_t*llm_grid_h*llm_grid_w`） | hw/4 |
| 3 | `chat.cpp` 写入视觉 embed 的字节数 `vit_size = hw/4 * HIDDEN_SIZE * 2`（chat.cpp:468） | hw/4 |

一致性的来源：ViT 输出在 [chat.cpp:467-470](chat.cpp#L467-L470) 以 `hw/4` 个 hidden 按偏移 `vit_offset*HIDDEN_SIZE` 写入，`forward_vit` 的输入 `pixel_values` 却有 `hw` 行 —— 差值是 merge_size²=4（每 2×2 patch 合成一个 embedding）。

---

## 4. `server.py` 改造

### 4.1 Schema

```python
class ImageUrl(BaseModel):
    url: str            # 绝对 URL 或 data:image/...;base64,... （网络 URL 暂不支持，见风险）

class ContentItem(BaseModel):
    type: str = "text"  # "text" | "image_url"
    text: Optional[str] = None
    image_url: Optional[ImageUrl] = None

class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[ContentItem]]   # 纯字符串保持向后兼容
```

OpenAI 标准请求体：`{"role":"user","content":[{"type":"text","text":"这张图里是什么?"},{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]}`

### 4.2 推理路径

- `has_vision = 任一 message.content 含 image_url item`
- 有 → 调 `pipeline_vision_light` 的 `run_image`；无 → 保持走现有 `run_chat` / `run_chat_stream`
- 图片分支同样扔 `asyncio.to_thread`，同样持全局锁；**流式与非流式共享 `§3.3` 的 (#a)-(#h) prefill 段**，差异只在 decode 输出（逐字 chunk 依然沿用现有 word-merge 逻辑）

### 4.3 锁与并发

复用 `_model_lock`（串行化 + `/health busy` 语义不变）。图片请求 token_len 更大、prefill 更重，`busy` 持续时间自然更长，无需改 `/health`。

---

## 5. 数据流（ASCII）

```
请求 content 数组
  │  content: [{text}, {image_url:"data:image/png;base64,..."}]
  ▼
collect_images(): base64→PIL→smart_resize→normalize→patch
  │  pixel_values (hw,768)  +  image_grid_thw [1,h/16,w/16]
  ▼
apply_chat_template(tokenize=True) ──► render 出 <|vision_start|><|image_pad|><|vision_end|>
  │                                      （每图单个 image_pad）
  ▼
expand_image_pad: 每图 image_pad → hw/4 个        ★ 三处对齐（§3.5）
  ▼
forward_embed(input_ids)
  ▼
vit_process_image:
  for each image i:
     rot_pos(grid) + fast_pos_embed_interpolate(grid)
     forward_vit(pixel_values[i], pos_ids, pos_idx, pos_weight, grid, self.ID_VISION_START 位置+1)
     ──► TPU: ViT → hw/4 个 embedding，写好 dev_buffer
  ▼
get_rope_index(input_ids, grid_thw, ID_IMAGE_PAD)  →  (3, L) int32
forward_prefill(position_ids)  →  首 token
  ▼
现有自回归 decode 循环（word-merge / im_end / max_tokens）→ 文本
```

---

## 6. 正确性校验点清单（逐条人工核对）

1. **pixel_values 排列**：行序（行优先扫 16×16 patch）+ 块内通道优先排序 —— 必须与 `Qwen2VLImageProcessorFast` 的 `F.unfold` 输出语义一致。**对拍方法**：在装有 torch 的任意机器跑一次 `AutoProcessor` 对该测试图的输出，dump `pixel_values / image_grid_thw / 展开后 input_ids / get_rope_index`，交给本模块做断言对比。这是最大风险点，建议最先验证。
2. **smart_resize 边界**：原图超长宽比抛错；`min_pixels>max_pixels` 抛错；结果必须 16 倍数，宽高不塌缩到小于 patch_size。
3. **num_grid_per_side=48 的插值语义**：`fast_pos_embed_interpolate` 的 48 必须来自 `num_position_embeddings` 而非魔法数；prefill 时该函数对 t=1 的原图 grid 总是成立。
4. **多图顺序**：input_ids 中 vision_start 出现顺序 == image_grid_thw 列表顺序 == pixel_values 中该图 patch 段的顺序；vit_offset 递增拼接。
5. **image_pad 展开三处一致**（§3.5）。
6. **get_rope_index 图像段 vs 文本段**：vision_start 计入前一文本段并为普通 token，不被 LLM mRoPE 特殊处理（复刻自 pipeline.py 的逻辑照搬）。
7. **chat.cpp assert**：[chat.cpp:437-440](chat.cpp#L437-L440) —— `pixel_values.size(){=hw*768}`、`position_ids.size(){=hw*2}`、`pos_idx/pos_weight{=hw*4}`；`pixel_values` 传 float32、`HIDDEN_SIZE` 与 `dev_buffer` 偏移不越界。
8. **超长输入**：多图 token 数超限时返回 400，语义同 run_chat。
9. **纯文本回归**：无图请求必须 100% 走原路径，输出与现网一致。

---

## 7. 测试计划

- **纯数学单测**（无需 TPU、无需 torch）：固定小尺寸（如 32×48 图）手算 rot_pos / interp / get_rope_index 期望值断言。
- **对拍测试**（一次性，开发机有 torch）：dump 基准 → 无 torch 环境回归。
- **端到端**（需 TPU 盒子，拿到 bmodel 后）：发一张真实图，检查首层/文本内容；但视觉 embedding 是否正确难以用输出纯靠人眼判定——建议先用"指认图中物体颜色/数量"之类的强信号 prompt（如有 ground truth 图）。

---

## 8. 风险与边界

| 项 | 说明 |
|---|---|
| patch 排列语义不确定 | 最高风险，必须对拍（§6.1） |
| 网络图片 URL 下载 | OpenAI `image_url` 允许 http URL；当前版本只支持 **base64 data URI 或本地路径**，规避了服务端出网。如需 URL，加 `requests` 拉取再 base64，复杂度可接受但未含在本设计内 |
| GIF/多帧 | 取第一帧；如需完整支持做动画理解另设计 |
| 视频 | 需视频解码（opencv/decord/av）逐帧序列化，逻辑同图片但成本高，**不在本期** |
| `mrope_section`/`interleaved` 硬编码 | 从 config.json 读取而非硬编码 |
| `num_grid_per_side` 硬编码 48 | 从 `num_position_embeddings` 开方取整读取 |
| server 兼容 | `content` 用 `Union[str, List]`，Pydantic v2 下顺序 `str` 在前，老客户端不受影响 |

---

## 9. 后续可扩展（不做）

- 视频帧推理（同思路 + 抽帧）
- 网络 URL 图片
- 多图更多测试集与 benchmark
```

---

## 10. 待你审阅的决策点

1. **是否接受新增 `pillow` 依赖**（唯一新依赖）。
2. **图片输入形态**：只支持 base64 data URI + 本地路径（推荐），还是也要网络 URL？
3. **本期是否包含多图**（实现上已支持，主要是测试/鲁棒性权衡）。
4. 确认后我按 §4 的 schema + §3 的模块顺序开工，先落地 `pipeline_vision_light.py` + 对拍 dump 脚本，再改 `server.py`。