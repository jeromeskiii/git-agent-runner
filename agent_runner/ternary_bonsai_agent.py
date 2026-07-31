"""Local wrapper for a Ternary-Bonsai GGUF chat agent.

This module intentionally stays lightweight: it loads the model via
``llama_cpp.Llama.from_pretrained`` and prints a single chat completion for the
prompt provided on stdin (or via ``--prompt``).
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Ternary-Bonsai via llama-cpp-python")
    parser.add_argument("--repo-id", required=True, help="Hugging Face repo id for the GGUF model")
    parser.add_argument("--filename", required=True, help="GGUF filename inside the repo")
    parser.add_argument(
        "--prompt",
        default=None,
        help="Optional prompt text. If omitted, read the full prompt from stdin.",
    )
    return parser


def _read_prompt(prompt: str | None) -> str:
    if prompt is not None:
        return prompt.strip()
    return sys.stdin.read().strip()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    prompt = _read_prompt(args.prompt)
    if not prompt:
        print("No prompt supplied on stdin or --prompt", file=sys.stderr)
        return 2

    try:
        from llama_cpp import Llama
    except ImportError as exc:
        print(
            "llama-cpp-python is not installed. Install it before using ternary-bonsai.",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 1

    llm = Llama.from_pretrained(
        repo_id=args.repo_id,
        filename=args.filename,
    )

    result = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": "You are a concise coding agent."},
            {"role": "user", "content": prompt},
        ]
    )

    choice = result["choices"][0]["message"]["content"]
    print(choice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
