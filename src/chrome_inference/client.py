from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8765"


def config_path() -> Path:
    home = os.environ.get("CHROME_INFERENCE_HOME")
    directory = Path(home).expanduser() if home else Path.home() / ".chrome-inference"
    return directory / "config.json"


def load_token() -> str:
    env_token = os.environ.get("CHROME_INFERENCE_TOKEN")
    if env_token:
        return env_token
    path = config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        token = config["token"]
        if isinstance(token, str) and token:
            return token
    except (OSError, KeyError, json.JSONDecodeError):
        pass
    raise SystemExit(
        f"No Chrome Inference token found. Start chrome-inference-server first or set CHROME_INFERENCE_TOKEN. Expected {path}"
    )


def request_json(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    base_url = os.environ.get("CHROME_INFERENCE_URL", DEFAULT_BASE_URL).rstrip("/")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {load_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Chrome Inference broker: {exc.reason}") from exc


def print_result(value: Any) -> None:
    if isinstance(value, dict) and set(value) == {"text"}:
        print(value["text"])
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(prog="chrome-inference")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capabilities")

    prompt = sub.add_parser("prompt")
    prompt.add_argument("text")
    prompt.add_argument("--system")
    prompt.add_argument("--language", default="en")

    classify = sub.add_parser("classify")
    classify.add_argument("text")
    classify.add_argument("--labels", nargs="+", required=True)
    classify.add_argument("--instruction")
    classify.add_argument("--language", default="en")

    summarize = sub.add_parser("summarize")
    summarize.add_argument("text")
    summarize.add_argument("--type", default="key-points", choices=["key-points", "tldr", "teaser", "headline"])
    summarize.add_argument("--format", default="plain-text", choices=["plain-text", "markdown"])
    summarize.add_argument("--length", default="medium", choices=["short", "medium", "long"])
    summarize.add_argument("--language", default="en")

    measure = sub.add_parser("measure")
    measure.add_argument("text")
    measure.add_argument("--system")
    measure.add_argument("--language", default="en")

    args = parser.parse_args()

    if args.command == "capabilities":
        result = request_json("GET", "/v1/capabilities")
    elif args.command == "prompt":
        result = request_json(
            "POST",
            "/v1/prompt",
            {"prompt": args.text, "system": args.system, "output_language": args.language},
        )
    elif args.command == "classify":
        result = request_json(
            "POST",
            "/v1/classify",
            {
                "input": args.text,
                "labels": args.labels,
                "instruction": args.instruction,
                "output_language": args.language,
            },
        )
    elif args.command == "summarize":
        result = request_json(
            "POST",
            "/v1/summarize",
            {
                "text": args.text,
                "type": args.type,
                "format": args.format,
                "length": args.length,
                "output_language": args.language,
            },
        )
    elif args.command == "measure":
        result = request_json(
            "POST",
            "/v1/measure",
            {"input": args.text, "system": args.system, "output_language": args.language},
        )
    else:
        parser.error("unknown command")
        return

    print_result(result)


if __name__ == "__main__":
    main()
