#!/usr/bin/env python3
"""Quick LLM connectivity test - sends a single prompt and prints the response.

Usage:
    # Uses .env config (FALLBACK_LLM_MODEL, FALLBACK_LLM_BASE_URL, etc.)
    .venv/bin/python test_llm.py

    # Override model/base_url inline
    .venv/bin/python test_llm.py --model ollama/llama3.2:3b --base-url http://localhost:11434

    # Custom prompt
    .venv/bin/python test_llm.py --prompt "Explain Python decorators in one sentence"
"""

import argparse
import os
import sys
import time

# Load .env before importing litellm
from pathlib import Path
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

import litellm


def main():
    parser = argparse.ArgumentParser(description="Test LLM connectivity")
    parser.add_argument("--model", default=os.environ.get("FALLBACK_LLM_MODEL", "ollama/llama3.1"))
    parser.add_argument("--base-url", default=os.environ.get("FALLBACK_LLM_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("FALLBACK_LLM_API_KEY", ""))
    parser.add_argument("--prompt", default="Say hello in one sentence.")
    parser.add_argument("--stream", action="store_true", default=True)
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    args = parser.parse_args()

    print(f"Model:    {args.model}")
    print(f"Base URL: {args.base_url or '(provider default)'}")
    print(f"Stream:   {args.stream}")
    print(f"Prompt:   {args.prompt}")
    print("-" * 50)

    kwargs = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "stream": args.stream,
        "timeout": 30,
    }
    if args.api_key:
        kwargs["api_key"] = args.api_key
    if args.base_url:
        kwargs["base_url"] = args.base_url
        kwargs["api_base"] = args.base_url

    start = time.time()
    try:
        if args.stream:
            print("Response: ", end="", flush=True)
            for chunk in litellm.completion(**kwargs):
                token = chunk.choices[0].delta.content
                if token:
                    print(token, end="", flush=True)
            print()
        else:
            resp = litellm.completion(**kwargs)
            print(f"Response: {resp.choices[0].message.content}")

        elapsed = time.time() - start
        print(f"\n✅ Success ({elapsed:.1f}s)")

    except Exception as e:
        elapsed = time.time() - start
        print(f"\n❌ Failed ({elapsed:.1f}s): {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
