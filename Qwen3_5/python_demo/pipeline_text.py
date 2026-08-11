#opyright (C) 2025 Sophgo Technologies Inc.  All rights reserved.
#
# TPU-MLIR is licensed under the 2-Clause BSD License except for the
# third-party components.
#
# ==============================================================================
#
# Text-only variant of pipeline.py — drops torch / qwen_vl_utils and all
# image/video paths, so only transformers + numpy + the local chat module
# are required.
# ==============================================================================

import time
import argparse
from transformers import AutoTokenizer
import chat
import sys
import numpy as np


class Qwen3_5():

    def __init__(self, args):
        # devid
        self.device = args.devid

        # load model
        self.model = chat.Qwen3_5()
        self.model.init(self.device, args.model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(args.config_path, trust_remote_code=True)
        self.ID_IM_END = self.tokenizer.convert_tokens_to_ids("<|im_end|>")
        self.support_history = self.model.support_history
        self.max_posid = 0
        self.history_max_posid = 0

    def __del__(self):
        self.model.deinit()

    def text_message(self):
        # yapf: disable
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": self.input_str}],
        }]
        # yapf: enable
        return messages

    def process(self, messages):
        return self.tokenizer.apply_chat_template(messages,
                                                  tokenize=True,
                                                  add_generation_prompt=True,
                                                  return_dict=True,
                                                  return_tensors="np")

    def forward_prefill(self, position_ids):
        if self.model.history_length == 0 or not self.support_history:
            self.history_max_posid = 0
            return self.model.forward_first(position_ids)
        self.max_posid += self.history_max_posid
        position_ids = position_ids + self.history_max_posid
        return self.model.forward_first(position_ids)

    def run_once(self, input_str):
        """
        Run a single inference turn programmatically.

        Returns the generated text, or None if the input could not be processed.
        """
        self.input_str = input_str

        messages = self.text_message()
        inputs = self.process(messages)
        token_len = inputs.input_ids.size
        max_input_tokens = self.model.SEQLEN if self.model.support_history \
            else self.model.MAX_INPUT_LENGTH
        if token_len > max_input_tokens:
            print(
                "Error: The maximum question length should be shorter than {} but we get {} instead."
                .format(max_input_tokens, token_len))
            return None
        if self.support_history:
            if (token_len + self.model.history_length > self.model.SEQLEN - 128) or \
            (self.model.history_length > self.model.PREFILL_KV_LENGTH):
                print("Warning: History is full and clear it to continue.")
                self.model.clear_history()
                self.history_max_posid = 0
        print("\nAnswer:")

        # Chat
        first_start = time.time()
        self.model.forward_embed(inputs.input_ids)
        position_ids = 3 * [i for i in range(token_len)]
        self.max_posid = token_len - 1
        token = self.forward_prefill(np.array(position_ids, dtype=np.int32))
        first_end = time.time()
        tok_num = 0
        # Following tokens
        full_word_tokens = []
        text = ""
        while token not in [self.ID_IM_END] and self.model.history_length < self.model.SEQLEN:
            full_word_tokens.append(token)
            word = self.tokenizer.decode(full_word_tokens, skip_special_tokens=True)
            if "�" not in word:
                if len(full_word_tokens) == 1:
                    pre_word = word
                    word = self.tokenizer.decode([token, token],
                                                 skip_special_tokens=True)[len(pre_word):]
                text += word
                print(word, flush=True, end="")
                full_word_tokens = []
            self.max_posid += 1
            position_ids = np.array([self.max_posid, self.max_posid, self.max_posid],
                                    dtype=np.int32)
            token = self.model.forward_next(position_ids)
            tok_num += 1
        self.history_max_posid = self.max_posid + 2
        next_end = time.time()
        first_duration = first_end - first_start
        next_duration = next_end - first_end
        tps = tok_num / next_duration if next_duration > 0 else 0.0
        print(f"\nFTL: {first_duration:.3f} s")
        print(f"TPS: {tps:.3f} tokens/s")
        if self.support_history:
            print(f"Total Tokens: {self.model.history_length}")
        return text

    def chat(self):
        """
        Start an interactive chat session.
        """
        # Instruct
        print("""\n=================================================================
1. If you want to quit, please enter one of [/q, /quit, /exit]
2. To create a new chat session, please enter one of [/clear, /new]
3. To use the contents of a .txt or .md file as your question, include @<path>
=================================================================""")
        # Stop Chatting with "/exit" input
        while True:
            input_str = input("\nQuestion: ")
            # Quit
            if input_str in ["/exit", "/q", "/quit"]:
                break
            if input_str in ["/clear", "/new", "/c"]:
                print("New chat session created.")
                self.model.clear_history()
                self.history_max_posid = 0
                continue

            input_str = extract_prompt_files(input_str)
            self.run_once(input_str)


def read_prompt_file(path):
    """Read a @-referenced .txt/.md file and return its contents."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError as e:
        print(f"Cannot open prompt file [ {path} ]: {e}")
        sys.exit(1)
    # Trim trailing newlines so the file behaves like a typed prompt.
    return content.rstrip('\r\n')


def extract_prompt_files(input_str):
    """Replace @<path> references to .txt/.md files with their contents."""
    text_tokens = []
    for t in input_str.split():
        if t.startswith("@") and len(t) > 1:
            path = t[1:]
            if path.lower().endswith((".txt", ".md")):
                text_tokens.append(read_prompt_file(path))
            else:
                text_tokens.append(t)
        else:
            text_tokens.append(t)
    return " ".join(text_tokens)


def main(args):
    model = Qwen3_5(args)
    if args.prompt is not None:
        # Programmatic (non-interactive) mode: run once and exit.
        prompt = extract_prompt_files(args.prompt)
        model.run_once(prompt)
    else:
        model.chat()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # yapf: disable
    parser.add_argument('-m', '--model_path', type=str, required=True,
                        help='path to the bmodel file')
    parser.add_argument('-c', '--config_path', type=str, default="../config",
                        help='path to the processor file')
    parser.add_argument('-d', '--devid', type=int, default=0, help='device ID to use')
    parser.add_argument('-p', '--prompt', type=str, default=None,
                        help='If set, run programmatically (non-interactive): a single inference is performed using this prompt and then the program exits. Include @<path> to read prompt text from a .txt/.md file.')
    # yapf: enable
    args = parser.parse_args()
    main(args)

