# Provider presets and capability routing

CostMarshal separates provider API facts from executable routing facts:

- **API capabilities** describe what the selected provider/model documents.
- **Runtime capabilities** describe what the current CostMarshal Codex worker can transport.
- **Effective capabilities** are the intersection. Only this set is written to a provider catalog and used for hard routing.

This distinction is important for multimodal models. A model may accept audio
or video through its native API while the current worker only transports text
and committed local images. CostMarshal must not route an audio/video task to
that model until the attachment protocol supports that modality end to end.

The built-in facts were reviewed against official provider documentation on
2026-07-26. Model names and API behavior can change; inspect the preset and
revalidate it before production deployment. Pricing is intentionally excluded
from presets and still requires a separately reviewed, hash-bound snapshot.

## Included presets

| Preset | Provider protocol | Currently routable input | Key variable |
| --- | --- | --- | --- |
| `deepseek-v4-flash` | Chat Completions / Anthropic | Gateway required | `DEEPSEEK_API_KEY` |
| `deepseek-v4-pro` | Chat Completions / Anthropic | Gateway required | `DEEPSEEK_API_KEY` |
| `kimi-k3` | Chat Completions | Gateway required | `MOONSHOT_API_KEY` |
| `kimi-k2.6` | Chat Completions | Gateway required | `MOONSHOT_API_KEY` |
| `longcat-2.0` | Chat documented; Responses deployment-verified | Text | `LONGCAT_API_KEY` |
| `mimo-v2.5` | Responses / Chat / Anthropic | Text, image | `MIMO_API_KEY` |
| `mimo-v2.5-pro` | Responses / Chat / Anthropic | Text | `MIMO_API_KEY` |
| `doubao-seed-2.0-lite` | Responses / Chat | Text, image | `ARK_API_KEY` |

Short aliases `deepseek`, `kimi`, `longcat`, `mimo`, and `doubao` resolve to
the recommended preset for that provider.

Inspect the machine-readable catalog:

```powershell
python scripts/costmarshal.py provider-presets
python scripts/costmarshal.py provider-presets --preset kimi
```

Generate a Codex provider profile and a matching catalog row without storing
the secret:

```powershell
python scripts/costmarshal.py configure-provider `
  --preset mimo-v2.5 `
  --profile mimo `
  --tier medium
```

For a Responses-compatible preset, the command returns `catalog_provider` in JSON. Its `capabilities` field
contains only effective capabilities and its prices are `null`; review and add
pricing before budgeted routing.

Current Codex releases reject `wire_api = "chat"`. DeepSeek and Kimi therefore
remain queryable capability records but are not emitted as executable profiles.
For either provider, deploy a separately reviewed Chat-to-Responses gateway,
then configure the gateway as a custom provider. Do not label the provider
effective until the gateway passes text, tools, usage, error, and multimodal
contract tests.

## Capability vocabulary

Use exact, modality-specific requirements such as `input:image`; avoid broad
labels such as `vision` or `multimodal`.

| Capability | Meaning |
| --- | --- |
| `input:text` | Text input |
| `input:image` | Image input |
| `input:audio` | Audio input |
| `input:video` | Video input |
| `input:document` | Document/file input |
| `output:text` | Text output |
| `streaming` | Streamed responses |
| `reasoning` | Explicit thinking/reasoning mode |
| `tool-calling` | Function/tool calls |
| `structured-output` | JSON mode or schema-constrained output |
| `context-caching` | Provider prompt/context caching |
| `long-context` | Documented context window of at least 128K |
| `code` | Coding or agent specialization |
| `web-search` | Provider-native search tool |

Capabilities remain case-sensitive because custom catalogs historically
allowed arbitrary labels. Built-in presets use the canonical lowercase names.

## Image tasks

The current end-to-end multimodal path supports local image input. An image
must:

1. be a workspace-relative `.gif`, `.jpeg`, `.jpg`, `.png`, or `.webp`;
2. be no larger than 16 MiB;
3. exist in the workspace's committed `HEAD`; and
4. be projected read-only into the worker.

Create an image task with:

```powershell
python scripts/costmarshal.py new-task `
  --project <project-dir> `
  --title "Inspect dashboard" `
  --purpose "Find the visual defect" `
  --input-image assets/dashboard.png
```

`--input-image` automatically adds both the image to allowed context and
`input:image` to required capabilities. Routing then rejects text-only
providers before any API call. The scheduler binds the image path into the
task brief and immutable context contract; the native and OCI workers pass the
projected file to `codex exec --image`.

Audio, video, and generic document attachments are visible in API facts but are
not yet effective capabilities. A custom catalog must not mark them effective
until its execution adapter implements and verifies those transports.

## Official references

- DeepSeek: <https://api-docs.deepseek.com/quick_start/pricing/>
- Kimi: <https://platform.kimi.ai/docs/api/chat>
- LongCat: <https://longcat.chat/platform/docs/api/chat.html>
- Xiaomi MiMo: <https://mimo.mi.com/docs/en-US/api/chat/responses>
- Doubao / Volcengine Ark: <https://www.volcengine.com/docs/82379/1795150>
