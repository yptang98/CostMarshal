# Provider presets and capability routing

CostMarshal separates provider API facts from executable routing facts:

- **API capabilities** describe what the selected provider/model documents.
- **Runtime capabilities** describe what the current CostMarshal Codex worker can transport.
- **Effective capabilities** are the intersection. Only this set is written to a provider catalog and used for hard routing.

This distinction is important for multimodal models. Agent mode transports text
and committed local images through Codex. The separate `multimodal-api` mode
transports bounded committed attachments directly through a gateway-bound
native Responses endpoint and produces a report only. CostMarshal routes a
modality only when the model fact, gateway policy, worker adapter, and immutable
task receipt all agree.

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
| `mimo-v2.5` | Responses / Chat / Anthropic | Text, image, audio, video | `MIMO_API_KEY` |
| `mimo-v2.5-pro` | Responses / Chat / Anthropic | Text | `MIMO_API_KEY` |
| `doubao-seed-2.0-lite` | Responses / Chat | Text, image, audio, video | `ARK_API_KEY` |

Short aliases `deepseek`, `kimi`, `longcat`, `mimo`, and `doubao` resolve to
the recommended preset for that provider.

Inspect the machine-readable catalog:

```powershell
python scripts/costmarshal.py provider-presets
python scripts/costmarshal.py provider-presets --preset kimi
```

Generate a direct Responses provider profile and a matching catalog row without
storing the secret:

```powershell
python scripts/costmarshal.py configure-provider `
  --preset mimo-v2.5 `
  --profile mimo `
  --tier medium
```

For a Responses-compatible preset, the command returns `catalog_provider` in JSON. Its `capabilities` field
contains only effective capabilities and its prices are `null`; review and add
pricing before budgeted routing.

Current Codex releases reject `wire_api = "chat"`. DeepSeek and Kimi can instead
be emitted as gateway-bound executable profiles:

```powershell
python scripts/costmarshal.py configure-provider `
  --preset deepseek-v4-flash `
  --profile deepseek-gateway `
  --tier low `
  --via-production-gateway

python scripts/costmarshal.py configure-provider `
  --preset kimi-k2.6 `
  --profile kimi-gateway `
  --tier high `
  --via-production-gateway
```

The generated worker profile uses a non-routable `.invalid` placeholder and
the Responses protocol; the production Actor rewrites it to the exact reviewed
Proxy endpoint. The returned catalog row is marked
`runtime_adapter: costmarshal-gateway-v1`. Dispatch refuses that row unless an
enforced production boundary is runtime-ready and externally certified. The
Proxy selects the upstream Chat endpoint from its hash-bound policy and
translates bounded text/image/audio, tools, JSON, usage, errors, and buffered
SSE. Video and document input are deliberately excluded from Chat adapter
capabilities.

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

## Multimodal tasks

Every attachment must be workspace-relative, exist as an exact committed Git
blob, match an allowlisted extension/media type, and be projected read-only.
The receipt binds path, modality, media type, byte length, SHA-256, and Git
object ID into both the task and collaboration contract.

Agent mode supports images up to 16 MiB:

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

Audio, video, and document inputs automatically select `multimodal-api`. This
mode:

- requires `worker_isolation.mode=required`, an externally certified production
  boundary, and a provider row using `costmarshal-gateway-v1`;
- requires a native Responses upstream for video/document (the Chat adapter
  permits only text/image/audio);
- limits all attachments together to 2 MiB and the encoded request to 4 MiB;
- rejects write claims, workspace mutations, custom commands, and tool use;
- requires authoritative input/output usage and
  `X-CostMarshal-Settlement: settled` from the hard-budget Proxy.

Example:

```powershell
python scripts/costmarshal.py new-task `
  --project <project-dir> `
  --title "Review customer call" `
  --purpose "Summarize the committed recording" `
  --input-audio evidence/call.wav `
  --estimated-output-tokens 1200
```

Document transport is implemented, but no built-in preset currently advertises
`input:document`; a reviewed custom native-Responses provider row and matching
gateway policy are therefore required. CostMarshal never infers document
support from a broad “multimodal” label.

## Provider metadata drift

Automated probes can record four bounded dimensions: API schema, pricing,
capabilities, and behavior. A `drift` result disables the provider; `unknown`
lowers its confidence and adds a routing priority penalty. A `match` result does
not add capability or restore a route.

```powershell
python scripts/costmarshal.py record-provider-observation `
  --project <project-dir> --provider mimo `
  --source https://mimo.mi.com/docs/en-US/api/chat/responses `
  --evidence-sha256 sha256:<report-hash> `
  --api-schema match --pricing drift --capabilities match --behavior match `
  --apply --command-id <stable-id>

python scripts/costmarshal.py review-provider-metadata `
  --project <project-dir> --provider mimo `
  --catalog reviewed-providers.json `
  --observation <observation-id> `
  --approved-by <reviewer> --expires-at <rfc3339-within-90-days> `
  --apply --command-id <stable-id>
```

The review installs only the selected normalized provider row, expires within
90 days, and must include every unresolved observation since the preceding
review. Later drift can again only reduce authority.

## Official references

- DeepSeek: <https://api-docs.deepseek.com/quick_start/pricing/>
- Kimi: <https://platform.kimi.ai/docs/api/chat>
- LongCat: <https://longcat.chat/platform/docs/api/chat.html>
- Xiaomi MiMo: <https://mimo.mi.com/docs/en-US/api/chat/responses>
- Doubao / Volcengine Ark: <https://www.volcengine.com/docs/82379/1795150>
