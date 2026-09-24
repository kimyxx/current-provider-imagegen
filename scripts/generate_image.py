#!/usr/bin/env python3
"""Generate an image through the provider configured in local Codex settings."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import struct
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier
    tomllib = None  # type: ignore[assignment]
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_OUTPUT = "output/imagegen/current-provider-image.png"
DEFAULT_QUALITY = "high"
DEFAULT_TIMEOUT = 180
DEFAULT_IMAGES_MODEL = "gpt-image-2"
DEFAULT_PARTIAL_IMAGES = 2
MAX_INPUT_BYTES = 50 * 1024 * 1024


def fail(message: str, code: int = 1) -> int:
    print(f"Error: {message}", file=sys.stderr)
    return code


def codex_config_path() -> Path:
    configured_root = os.environ.get("CODEX_HOME")
    if configured_root:
        return Path(configured_root).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def parse_simple_toml(text: str) -> dict[str, Any]:
    """Parse the small string/table subset needed from Codex config on Python 3.9."""
    parsed: dict[str, Any] = {}
    section: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = [part.strip() for part in line[1:-1].split(".")]
            cursor = parsed
            for part in section:
                child = cursor.setdefault(part, {})
                if not isinstance(child, dict):
                    raise RuntimeError(f"Invalid TOML section: {line}")
                cursor = child
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith(("\"", "'")) and raw_value[-1:] == raw_value[:1]:
            value: Any = raw_value[1:-1]
        elif raw_value in {"true", "false"}:
            value = raw_value == "true"
        else:
            value = raw_value
        cursor = parsed
        for part in section:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                raise RuntimeError(f"Invalid TOML key: {key}")
            cursor = child
        cursor[key] = value
    return parsed


def load_config(config_path: Path) -> dict[str, Any]:
    text = config_path.read_text(encoding="utf-8")
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"Cannot parse Codex config: {exc}") from exc
    return parse_simple_toml(text)


def load_provider() -> tuple[str, str, str]:
    config_path = codex_config_path()
    if not config_path.is_file():
        raise RuntimeError(f"Codex config not found: {config_path}")

    try:
        config = load_config(config_path)
    except OSError as exc:
        raise RuntimeError(f"Cannot read Codex config: {exc}") from exc

    provider_name = str(config.get("model_provider", "")).strip()
    if not provider_name:
        raise RuntimeError("model_provider is missing from Codex config")

    provider = config.get("model_providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        raise RuntimeError(f"Provider {provider_name!r} is not defined in Codex config")

    base_url = str(provider.get("base_url", "")).strip().rstrip("/")
    model = str(config.get("model", "")).strip()
    if not base_url or not model:
        raise RuntimeError("The active provider must define base_url and model")

    token = ""
    token_value = provider.get("experimental_bearer_token")
    if isinstance(token_value, str):
        token = token_value.strip()

    if not token:
        env_name = provider.get("env_key") or provider.get("api_key_env")
        if isinstance(env_name, str) and env_name.strip():
            token = os.environ.get(env_name.strip(), "").strip()

    if not token:
        raise RuntimeError(
            "No bearer token was found for the active provider. "
            "Configure the provider credential without placing it in this plugin."
        )

    return base_url, model, token


def read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt_file:
        raise RuntimeError("Use --prompt or --prompt-file, not both")
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if not path.is_file():
            raise RuntimeError(f"Prompt file not found: {path}")
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = (prompt or "").strip()
    if not value:
        raise RuntimeError("A non-empty --prompt or --prompt-file is required")
    return value


def read_reference(path_value: str) -> tuple[str, str]:
    path, mime_type = validate_reference(path_value)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return mime_type, f"data:{mime_type};base64,{encoded}"


def validate_reference(path_value: str) -> tuple[Path, str]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Reference image not found: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise RuntimeError(f"Reference image exceeds 50MB: {path}")

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise RuntimeError(f"Unsupported reference image type: {path.suffix}")

    return path, mime_type


def validate_mask(mask_value: str, image_paths: list[str]) -> None:
    if not image_paths:
        raise RuntimeError("--mask requires at least one --image reference")

    image_path, image_mime = validate_reference(image_paths[0])
    mask_path, mask_mime = validate_reference(mask_value)
    if mask_mime != "image/png":
        raise RuntimeError("Mask must be a PNG image with an alpha channel")
    if image_mime != mask_mime:
        raise RuntimeError("The mask and first reference image must use the same format")

    image_info_value = image_info(image_path.read_bytes())
    mask_raw = mask_path.read_bytes()
    mask_info = image_info(mask_raw)
    if not image_info_value or not mask_info or image_info_value[1:] != mask_info[1:]:
        raise RuntimeError("The mask and first reference image must have identical dimensions")
    if len(mask_raw) < 26 or mask_raw[25] not in {4, 6}:
        raise RuntimeError("Mask PNG must contain an alpha channel")


def build_input(prompt: str, image_paths: list[str]) -> str | list[dict[str, Any]]:
    if not image_paths:
        return prompt

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for image_path in image_paths:
        _mime_type, data_url = read_reference(image_path)
        content.append({"type": "input_image", "image_url": data_url})
    return [{"role": "user", "content": content}]


def add_aspect_hint(prompt: str, size_hint: Optional[str]) -> str:
    if not size_hint:
        return prompt
    return f"{prompt}\n\nOutput framing hint: {size_hint}."


def safe_error_body(body: str, token: str) -> str:
    cleaned = body.replace(token, "[redacted]")
    cleaned = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", cleaned)
    return cleaned[:2000]


class ProviderHTTPError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def post_json(endpoint: str, token: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ProviderHTTPError(
            exc.code,
            f"Provider returned HTTP {exc.code}: {safe_error_body(body, token)}",
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Provider network request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Provider request timed out") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Provider returned non-JSON data: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Provider returned an unexpected JSON shape")
    return parsed


def post_sse(
    endpoint: str,
    token: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[bytes, list[str]]:
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return read_sse_request(request, token, timeout)


def read_sse_request(
    request: Request,
    token: str,
    timeout: int,
) -> tuple[bytes, list[str]]:
    images: list[tuple[str, bytes]] = []
    event_types: list[str] = []

    def handle_frame(frame: str) -> None:
        data_lines = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if not data_lines:
            return
        data_text = "\n".join(data_lines)
        if data_text == "[DONE]":
            return
        try:
            event = json.loads(data_text)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if isinstance(event_type, str):
            event_types.append(event_type)
        if event.get("error"):
            error_body = json.dumps(event["error"], ensure_ascii=False)
            raise RuntimeError(f"Provider stream returned an error: {safe_error_body(error_body, token)}")

        candidates: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key in ("partial_image_b64", "b64_json", "image_base64", "result"):
                    candidate = value.get(key)
                    if isinstance(candidate, str):
                        candidates.append(candidate)
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(event)
        for candidate in candidates:
            try:
                raw = base64.b64decode(candidate, validate=True)
            except (binascii.Error, ValueError):
                continue
            if image_info(raw):
                images.append((event_type if isinstance(event_type, str) else "unknown", raw))
                return

    try:
        with urlopen(request, timeout=timeout) as response:
            frame_lines: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if frame_lines:
                        handle_frame("\n".join(frame_lines))
                        frame_lines = []
                    continue
                frame_lines.append(line)
            if frame_lines:
                handle_frame("\n".join(frame_lines))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ProviderHTTPError(
            exc.code,
            f"Provider returned HTTP {exc.code}: {safe_error_body(body, token)}",
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Provider network request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Provider stream timed out") from exc

    if not images:
        raise RuntimeError("Provider stream contained no decodable image")
    return images[-1][1], list(dict.fromkeys(event_types))


def build_multipart_request(
    endpoint: str,
    token: str,
    fields: dict[str, str],
    image_paths: list[str],
    accept: str,
    mask_path: Optional[str] = None,
) -> Request:
    boundary = f"----current-provider-imagegen-{secrets.token_hex(16)}"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    for index, path_value in enumerate(image_paths, start=1):
        path, mime_type = validate_reference(path_value)
        suffix = path.suffix.lower() or mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"reference-{index}{suffix}"
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            f'Content-Disposition: form-data; name="image[]"; filename="{filename}"\r\n'.encode("ascii")
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"))
        body.extend(path.read_bytes())
        body.extend(b"\r\n")

    if mask_path:
        path, mime_type = validate_reference(mask_path)
        filename = f"mask{path.suffix.lower() or '.png'}"
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            f'Content-Disposition: form-data; name="mask"; filename="{filename}"\r\n'.encode("ascii")
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"))
        body.extend(path.read_bytes())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    request = Request(
        endpoint,
        data=bytes(body),
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    return request


def post_multipart(
    endpoint: str,
    token: str,
    fields: dict[str, str],
    image_paths: list[str],
    timeout: int,
    mask_path: Optional[str] = None,
) -> dict[str, Any]:
    request = build_multipart_request(endpoint, token, fields, image_paths, "application/json", mask_path)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise ProviderHTTPError(
            exc.code,
            f"Provider returned HTTP {exc.code}: {safe_error_body(response_body, token)}",
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Provider network request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Provider request timed out") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Provider returned non-JSON data: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Provider returned an unexpected JSON shape")
    return parsed


def post_multipart_sse(
    endpoint: str,
    token: str,
    fields: dict[str, str],
    image_paths: list[str],
    timeout: int,
    mask_path: Optional[str] = None,
) -> tuple[bytes, list[str]]:
    request = build_multipart_request(endpoint, token, fields, image_paths, "text/event-stream", mask_path)
    return read_sse_request(request, token, timeout)


def maybe_decode(value: Any) -> Optional[bytes]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith("data:image/") and "," in candidate:
        candidate = candidate.split(",", 1)[1]
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return None
    if len(candidate) < 64:
        return None
    candidate = re.sub(r"\s+", "", candidate)
    try:
        raw = base64.b64decode(candidate + "=" * (-len(candidate) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if image_info(raw) else None


def candidate_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        priority_keys = (
            "b64_json",
            "image_base64",
            "result",
            "data",
            "image",
            "content",
        )
        for key in priority_keys:
            if key in value:
                yield value[key]
        for nested in value.values():
            yield from candidate_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from candidate_values(nested)
    elif isinstance(value, str):
        yield value


def extract_image(response: dict[str, Any]) -> bytes:
    for candidate in candidate_values(response):
        if isinstance(candidate, str):
            raw = maybe_decode(candidate)
            if raw:
                return raw
        elif isinstance(candidate, dict):
            raw = maybe_decode(candidate.get("b64_json"))
            if raw:
                return raw
    raise RuntimeError(
        "Provider response contained no decodable image. "
        "Expected an image_generation_call result or base64 image data."
    )


def image_info(raw: bytes) -> Optional[tuple[str, int, int]]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        width, height = struct.unpack(">II", raw[16:24])
        if width and height:
            return "PNG", width, height

    if raw.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(raw):
            if raw[index] != 0xFF:
                index += 1
                continue
            marker = raw[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(raw):
                break
            segment_length = struct.unpack(">H", raw[index:index + 2])[0]
            if segment_length < 2 or index + segment_length > len(raw):
                break
            if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
                if segment_length >= 7:
                    height, width = struct.unpack(">HH", raw[index + 3:index + 7])
                    if width and height:
                        return "JPEG", width, height
            index += segment_length

    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" and len(raw) >= 30:
        chunk = raw[12:16]
        if chunk == b"VP8X" and len(raw) >= 30:
            width = 1 + int.from_bytes(raw[24:27], "little")
            height = 1 + int.from_bytes(raw[27:30], "little")
            if width and height:
                return "WEBP", width, height

    if raw.startswith((b"GIF87a", b"GIF89a")) and len(raw) >= 10:
        width, height = struct.unpack("<HH", raw[6:10])
        if width and height:
            return "GIF", width, height

    return None


def write_image(raw: bytes, output_value: str, force: bool) -> tuple[Path, tuple[str, int, int]]:
    info = image_info(raw)
    if not info:
        raise RuntimeError("Decoded output is not a supported PNG, JPEG, WebP, or GIF image")

    output = Path(output_value).expanduser().resolve()
    if output.exists() and not force:
        raise RuntimeError(f"Output already exists: {output}; use --force to overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    if output.stat().st_size == 0:
        raise RuntimeError(f"Output file is empty: {output}")
    return output, info


def image_api_endpoints(base_url: str) -> list[str]:
    return [
        f"{base_url}/v1/images/generations",
        f"{base_url}/images/generations",
    ]


def image_edit_endpoints(base_url: str) -> list[str]:
    return [
        f"{base_url}/v1/images/edits",
        f"{base_url}/images/edits",
    ]


def build_images_payload(
    prompt: str,
    model: str,
    quality: str,
    size_hint: Optional[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "quality": quality,
    }
    if size_hint and re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size_hint):
        payload["size"] = size_hint
    return payload


def post_images_api(
    base_url: str,
    token: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    endpoints = image_api_endpoints(base_url)
    last_error: Optional[ProviderHTTPError] = None
    for index, endpoint in enumerate(endpoints):
        try:
            return endpoint, post_json(endpoint, token, payload, timeout)
        except ProviderHTTPError as exc:
            last_error = exc
            if exc.status not in {404, 405} or index == len(endpoints) - 1:
                raise
    raise last_error or RuntimeError("Images API request failed")


def post_images_sse(
    base_url: str,
    token: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[str, bytes, list[str]]:
    endpoints = image_api_endpoints(base_url)
    last_error: Optional[ProviderHTTPError] = None
    for index, endpoint in enumerate(endpoints):
        try:
            raw, events = post_sse(endpoint, token, payload, timeout)
            return endpoint, raw, events
        except ProviderHTTPError as exc:
            last_error = exc
            if exc.status not in {404, 405} or index == len(endpoints) - 1:
                raise
    raise last_error or RuntimeError("Images API stream request failed")


def post_image_edits_api(
    base_url: str,
    token: str,
    fields: dict[str, str],
    image_paths: list[str],
    timeout: int,
    mask_path: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    endpoints = image_edit_endpoints(base_url)
    last_error: Optional[ProviderHTTPError] = None
    for index, endpoint in enumerate(endpoints):
        try:
            return endpoint, post_multipart(endpoint, token, fields, image_paths, timeout, mask_path)
        except ProviderHTTPError as exc:
            last_error = exc
            if exc.status not in {404, 405} or index == len(endpoints) - 1:
                raise
    raise last_error or RuntimeError("Image edits API request failed")


def post_image_edits_sse(
    base_url: str,
    token: str,
    fields: dict[str, str],
    image_paths: list[str],
    timeout: int,
    mask_path: Optional[str] = None,
) -> tuple[str, bytes, list[str]]:
    endpoints = image_edit_endpoints(base_url)
    last_error: Optional[ProviderHTTPError] = None
    for index, endpoint in enumerate(endpoints):
        try:
            raw, events = post_multipart_sse(endpoint, token, fields, image_paths, timeout, mask_path)
            return endpoint, raw, events
        except ProviderHTTPError as exc:
            last_error = exc
            if exc.status not in {404, 405} or index == len(endpoints) - 1:
                raise
    raise last_error or RuntimeError("Image edits API stream request failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Reference image path; repeatable. Images mode automatically uses /v1/images/edits.",
    )
    parser.add_argument("--mask", help="PNG mask for local editing; requires --image and matching dimensions")
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--model", help="Override the selected model")
    parser.add_argument("--api-mode", choices=("responses", "images"), default="responses")
    parser.add_argument("--quality", choices=("low", "medium", "high", "auto"), default=DEFAULT_QUALITY)
    parser.add_argument(
        "--size",
        help="Aspect-ratio hint; valid WIDTHxHEIGHT values are sent to the Images API or Responses image tool",
    )
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument("--stream", dest="stream", action="store_true", help="Use SSE streaming (default)")
    stream_group.add_argument("--no-stream", dest="stream", action="store_false", help="Use one-shot JSON response")
    parser.set_defaults(stream=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout < 1:
        return fail("--timeout must be at least 1 second")

    try:
        prompt = read_prompt(args.prompt, args.prompt_file)
        base_url, active_model, token = load_provider()
        if args.mask:
            validate_mask(args.mask, args.image)
        prompt = add_aspect_hint(prompt, args.size)
        model = args.model or (DEFAULT_IMAGES_MODEL if args.api_mode == "images" else active_model)

        if args.api_mode == "images":
            payload = build_images_payload(prompt, model, args.quality, args.size)
            if args.stream:
                payload["stream"] = True
                payload["partial_images"] = DEFAULT_PARTIAL_IMAGES
            if args.image:
                fields = {
                    key: (str(value).lower() if isinstance(value, bool) else str(value))
                    for key, value in payload.items()
                }
                endpoints = image_edit_endpoints(base_url)
            else:
                fields = {}
                endpoints = image_api_endpoints(base_url)
        else:
            input_value = build_input(prompt, args.image)
            image_tool: dict[str, Any] = {
                "type": "image_generation",
                "quality": args.quality,
            }
            if args.stream:
                image_tool["partial_images"] = DEFAULT_PARTIAL_IMAGES
            if args.size and re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", args.size):
                image_tool["size"] = args.size
            if args.mask:
                _mask_mime, mask_data_url = read_reference(args.mask)
                image_tool["action"] = "edit"
                image_tool["input_image_mask"] = {"image_url": mask_data_url}
            payload = {
                "model": model,
                "input": input_value,
                "tools": [image_tool],
            }
            if args.stream:
                payload["stream"] = True
            endpoints = [f"{base_url}/v1/responses"]

        if args.dry_run:
            safe_payload: dict[str, Any] = dict(payload)
            if args.api_mode == "responses" and args.image:
                safe_payload["input"] = "[prompt plus reference image data omitted]"
            if args.api_mode == "responses" and args.mask:
                safe_tools = [dict(tool) for tool in payload.get("tools", [])]
                for tool in safe_tools:
                    if "input_image_mask" in tool:
                        tool["input_image_mask"] = "[mask image data omitted]"
                safe_payload["tools"] = safe_tools
            dry_run: dict[str, Any] = {
                "api_mode": args.api_mode,
                "endpoints": endpoints,
                "payload": safe_payload,
            }
            if args.api_mode == "images" and args.image:
                dry_run["content_type"] = "multipart/form-data"
                dry_run["image_fields"] = ["image[]" for _ in args.image]
                dry_run["reference_images"] = [str(Path(value).expanduser().resolve()) for value in args.image]
                if args.mask:
                    dry_run["mask_field"] = "mask"
                    dry_run["mask_image"] = str(Path(args.mask).expanduser().resolve())
            print(json.dumps(dry_run, ensure_ascii=False, indent=2))
            return 0

        stream_events: list[str] = []
        if args.api_mode == "images":
            if args.image:
                if args.stream:
                    endpoint, raw_image, stream_events = post_image_edits_sse(
                        base_url, token, fields, args.image, args.timeout, args.mask
                    )
                else:
                    endpoint, response = post_image_edits_api(
                        base_url, token, fields, args.image, args.timeout, args.mask
                    )
                    raw_image = extract_image(response)
            else:
                if args.stream:
                    endpoint, raw_image, stream_events = post_images_sse(
                        base_url, token, payload, args.timeout
                    )
                else:
                    endpoint, response = post_images_api(base_url, token, payload, args.timeout)
                    raw_image = extract_image(response)
        else:
            endpoint = endpoints[0]
            if args.stream:
                raw_image, stream_events = post_sse(endpoint, token, payload, args.timeout)
            else:
                response = post_json(endpoint, token, payload, args.timeout)
                raw_image = extract_image(response)
        output, info = write_image(raw_image, args.out, args.force)
        fmt, width, height = info
        print(f"API_MODE={args.api_mode}")
        print(f"STREAM={str(args.stream).lower()}")
        print(f"REQUEST_URL={endpoint}")
        print(f"IMAGE_PATH={output}")
        print(f"IMAGE_FORMAT={fmt}")
        print(f"IMAGE_SIZE={width}x{height}")
        if stream_events:
            print(f"STREAM_EVENTS={','.join(stream_events)}")
        print(f"PREVIEW=![generated image]({output})")
        return 0
    except (OSError, RuntimeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
