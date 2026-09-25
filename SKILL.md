---
name: current-provider-imagegen
description: Generate or edit raster images by calling the provider configured in the local Codex config. Use when the built-in image generation tool is unavailable or when the user asks to use the current provider directly.
---

# Current Provider Imagegen

This skill uses the plugin script instead of the built-in `image_gen` tool. The script reads the active provider from the local Codex configuration at runtime and defaults to the Responses API with SSE streaming for conversational image generation and editing. The direct Images API remains available with `--api-mode images`.

## Invocation

Choose the Python launcher by platform before running the script:

- macOS/Linux: use `python3`.
- Windows PowerShell: use `py -3`; if the Python launcher is unavailable, use `python`.
- The script does not rewrite `python3` automatically. The caller must select the launcher that exists on the current system.

macOS/Linux:

```bash
python3 "/absolute/path/to/current-provider-imagegen/scripts/generate_image.py" \
  --api-mode responses \
  --prompt "A photorealistic vertical 9:16 commercial image" \
  --out "/absolute/path/to/output.png"
```

Windows PowerShell:

```powershell
py -3 "C:\Users\<username>\.codex\skills\current-provider-imagegen\scripts\generate_image.py" `
  --api-mode responses `
  --prompt "A photorealistic vertical 9:16 commercial image" `
  --out "C:\Temp\output.png"
```

Use `python` in the Windows example if `py -3` is not available. Apply the same launcher and path rules to the examples below.

The default image model is `gpt-image-2.5-sunburst`, with default quality `xhigh`. Use `--model` or `--quality` only when the provider exposes a different compatible image model or quality setting.

For a local reference image in the default Responses mode, add one or more `--image` arguments:

```bash
python3 "/absolute/path/to/current-provider-imagegen/scripts/generate_image.py" \
  --prompt "Use the attached product only as an appearance reference; create a new scene" \
  --image "/absolute/path/to/reference.png" \
  --out "/absolute/path/to/output.png"
```

To use the direct Images API for generation or editing, pass `--api-mode images`. It also uses streaming by default; reference images automatically select `/v1/images/edits`.

For a masked local edit, pass one reference image and a matching PNG mask:

```bash
python3 "/absolute/path/to/current-provider-imagegen/scripts/generate_image.py" \
  --prompt "Replace the marked area with a vase of flowers" \
  --image "/absolute/path/to/source.png" \
  --mask "/absolute/path/to/mask.png" \
  --out "/absolute/path/to/output.png"
```

To explicitly use the Responses image-tool mode for a text-only prompt:

```bash
python3 "/absolute/path/to/current-provider-imagegen/scripts/generate_image.py" \
  --api-mode responses \
  --prompt "A photorealistic large gray wolf" \
  --size "1536x2048" \
  --out "/absolute/path/to/output.png"
```

## Provider contract

- Read `model_provider`, `model`, and the selected provider's `base_url` at runtime.
- Configure `base_url` as the provider root URL without a trailing `/v1` path, for example `https://provider.example.com`; the script appends `/v1/responses` or the Images API path itself. A trailing slash is allowed and will be removed.
- Read the bearer token only in memory from the provider configuration or its configured environment variable.
- Default `--api-mode responses` sends `POST <base_url>/v1/responses` with the active text model at the top level and `gpt-image-2.5-sunburst` in the `image_generation` tool. It uses `stream: true` and sends `partial_images` for progressive previews.
- In Responses mode, a valid `WIDTHxHEIGHT` `--size` value is sent inside the image-generation tool; ratio-only hints such as `3:4` remain prompt hints.
- Responses reference images are sent as input image data URLs; use the tool's `input_image_mask` only when a mask workflow is explicitly required.
- `--mask` requires at least one `--image`; the mask is sent as `input_image_mask.image_url` in Responses mode or as the `mask` multipart field in Images mode. The mask must be a PNG with an alpha channel and match the first reference image's format and dimensions.
- When multiple reference images are provided with a mask, the provider applies the mask to the first image.
- `--api-mode images` sends text-only requests to `POST <base_url>/v1/images/generations`, or multipart reference-image requests to `POST <base_url>/v1/images/edits`; both use SSE streaming by default and try the no-`/v1` fallback only after a 404/405 response.
- Pass `--no-stream` only for a provider that does not support SSE; it uses one-shot JSON and remains vulnerable to idle connection timeouts.
- Use the requested aspect ratio as a prompt hint. Report the provider-returned dimensions instead of claiming native dimensions that were not returned.

## Completion gate

An image task is complete only when all of the following are true:

1. The script writes a non-empty PNG, JPEG, or WebP file.
2. The script validates the image signature and dimensions.
3. The final response includes the absolute path and an inline Markdown preview using that path.

Never claim that an image was generated when the provider response, file, or preview is missing. Never print or include the bearer token in logs, prompts, errors, or responses.

## Default output

When the user does not specify an output path, use the project-local `output/imagegen/current-provider-image.png`. Do not overwrite an existing file unless the user explicitly requests replacement or passes `--force`.
