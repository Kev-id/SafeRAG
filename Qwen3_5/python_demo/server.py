#copyright (C) 2025 Sophgo Technologies Inc.  All rights reserved.
#
# TPU-MLIR is licensed under the 2-Clause BSD License except for the
# third-party components.
#
# ==============================================================================
#
# FastAPI server wrapping pipeline_text.py as an OpenAI-compatible chat API.
#
# Usage:
#   python server.py -m ../path/to/model.bmodel -c ../config --port 8000
#
# Dependencies: fastapi, uvicorn, pydantic  (pip install fastapi uvicorn)
# ==============================================================================

import time
import uuid
import sys
import os
import re
from threading import Lock
from typing import List, Optional, AsyncGenerator
import json

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_text import Qwen3_5


# ---------------------------------------------------------------------------
# Pydantic schemas (OpenAI-compatible)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "tpu-qwen3.5"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ---------------------------------------------------------------------------
# Streaming (SSE) schemas
# ---------------------------------------------------------------------------

class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage = Field(default_factory=DeltaMessage)
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[StreamChoice]


# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

_model: Optional[Qwen3_5] = None
_model_lock = Lock()


def get_model() -> Qwen3_5:
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not yet loaded")
    return _model


def _strip_thinking(text: str) -> str:
    """Remove  <think>...</think>  blocks from model output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# OpenAI-compatible inference
# ---------------------------------------------------------------------------

def run_chat(messages: List[ChatMessage]) -> str:
    """
    Stateless multi-turn inference.

    Tokenizes the full conversation via apply_chat_template, runs the model,
    and returns the assistant's reply.  Thread-safe via a global lock.
    """
    m = get_model()

    # The chat template (config/chat_template.jinja line 4-5) accepts plain
    # string content, so we can pass OpenAI-format messages directly.
    qwen_msgs = [{"role": msg.role, "content": msg.content} for msg in messages]

    with _model_lock:
        m.model.clear_history()
        m.history_max_posid = 0

        inputs = m.tokenizer.apply_chat_template(
            qwen_msgs,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="np",
        )
        token_len = inputs.input_ids.size

        max_input = m.model.SEQLEN if m.model.support_history else m.model.MAX_INPUT_LENGTH
        if token_len > max_input:
            raise HTTPException(
                status_code=400,
                detail=f"Input length {token_len} exceeds maximum {max_input}",
            )

        # ---- prefill ----
        m.model.forward_embed(inputs.input_ids)
        position_ids = 3 * [i for i in range(token_len)]
        m.max_posid = token_len - 1

        token = m.forward_prefill(np.array(position_ids, dtype=np.int32))

        # ---- autoregressive decode ----
        full_word_tokens: List[int] = []
        text = ""
        tok_num = 0
        im_end = m.ID_IM_END

        while token != im_end and m.model.history_length < m.model.SEQLEN:
            full_word_tokens.append(token)
            word = m.tokenizer.decode(full_word_tokens, skip_special_tokens=True)
            if "�" not in word:
                if len(full_word_tokens) == 1:
                    pre_word = word
                    word = m.tokenizer.decode(
                        [token, token], skip_special_tokens=True
                    )[len(pre_word):]
                text += word
                full_word_tokens = []
            m.max_posid += 1
            position_ids = np.array(
                [m.max_posid, m.max_posid, m.max_posid], dtype=np.int32
            )
            token = m.model.forward_next(position_ids)
            tok_num += 1

        m.history_max_posid = m.max_posid + 2

    return _strip_thinking(text)


def run_chat_stream(messages: List[ChatMessage], model_name: str, chat_id: str):
    """SSE generator — yields "data: {...}\n\n" lines for streaming."""
    m = get_model()
    created = int(time.time())

    qwen_msgs = [{"role": msg.role, "content": msg.content} for msg in messages]

    with _model_lock:
        m.model.clear_history()
        m.history_max_posid = 0

        inputs = m.tokenizer.apply_chat_template(
            qwen_msgs,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="np",
        )
        token_len = inputs.input_ids.size

        max_input = m.model.SEQLEN if m.model.support_history else m.model.MAX_INPUT_LENGTH
        if token_len > max_input:
            err_chunk = ChatCompletionChunk(
                id=chat_id, created=created, model=model_name,
                choices=[StreamChoice(finish_reason="error")],
            )
            yield f"data: {err_chunk.model_dump_json()}\n\n"
            return

        # ---- prefill ----
        m.model.forward_embed(inputs.input_ids)
        position_ids = 3 * [i for i in range(token_len)]
        m.max_posid = token_len - 1

        token = m.forward_prefill(np.array(position_ids, dtype=np.int32))

        # ---- autoregressive decode ----
        full_word_tokens: List[int] = []
        text = ""
        im_end = m.ID_IM_END

        # First chunk with role
        first_chunk = ChatCompletionChunk(
            id=chat_id, created=created, model=model_name,
            choices=[StreamChoice(delta=DeltaMessage(role="assistant"))],
        )
        yield f"data: {first_chunk.model_dump_json()}\n\n"

        while token != im_end and m.model.history_length < m.model.SEQLEN:
            full_word_tokens.append(token)
            word = m.tokenizer.decode(full_word_tokens, skip_special_tokens=True)
            if "�" not in word:
                if len(full_word_tokens) == 1:
                    pre_word = word
                    word = m.tokenizer.decode(
                        [token, token], skip_special_tokens=True
                    )[len(pre_word):]
                text += word
                # Send this word as a delta chunk
                chunk = ChatCompletionChunk(
                    id=chat_id, created=created, model=model_name,
                    choices=[StreamChoice(delta=DeltaMessage(content=word))],
                )
                yield f"data: {chunk.model_dump_json()}\n\n"
                full_word_tokens = []
            m.max_posid += 1
            position_ids = np.array(
                [m.max_posid, m.max_posid, m.max_posid], dtype=np.int32
            )
            token = m.model.forward_next(position_ids)

        m.history_max_posid = m.max_posid + 2

        # Final chunk
        final_chunk = ChatCompletionChunk(
            id=chat_id, created=created, model=model_name,
            choices=[StreamChoice(delta=DeltaMessage(), finish_reason="stop")],
        )
        yield f"data: {final_chunk.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Qwen3.5 Text API", version="1.0.0")


@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# OpenAI-compatible model list  (needed by Cherry Studio / ChatBox / etc.)
# ---------------------------------------------------------------------------

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "sophgo"


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


@app.get("/v1/models")
async def list_models():
    return ModelList(data=[ModelInfo(id="tpu-qwen3.5")])


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint (streaming + non-streaming)."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if req.stream:
        return StreamingResponse(
            run_chat_stream(req.messages, req.model, chat_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    text = run_chat(req.messages)
    return ChatCompletionResponse(
        id=chat_id,
        created=int(time.time()),
        model=req.model,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=text),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Qwen3.5 Text API Server")
    parser.add_argument("-m", "--model_path", type=str, required=True,
                        help="Path to the bmodel file")
    parser.add_argument("-c", "--config_path", type=str, default="../config",
                        help="Path to the processor config directory")
    parser.add_argument("-d", "--devid", type=int, default=0,
                        help="TPU device ID")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Bind address")
    parser.add_argument("--port", type=int, default=8000,
                        help="Listen port")
    args = parser.parse_args()

    # Build a namespace compatible with Qwen3_5.__init__
    class ModelArgs:
        pass
    model_args = ModelArgs()
    model_args.devid = args.devid
    model_args.model_path = args.model_path
    model_args.config_path = args.config_path

    print(f"Loading model from {args.model_path} ...")
    _model = Qwen3_5(model_args)
    print("Model loaded.")

    uvicorn.run(app, host=args.host, port=args.port)

