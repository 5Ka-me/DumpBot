# Speech-to-text on chutes.ai: research findings (checked 2026-10-06)

## TL;DR
- **There is no official chutes Whisper chute any more.** The old public `chutes/whisper-large-v3` (host `chutes-whisper-large-v3.chutes.ai/transcribe`) returns 404 from `GET https://api.chutes.ai/chutes/whisper-large-v3`. The search `?include_public=true&name=whisper` returns 0 results. The old `kokoro` and `csm-1b` chutes also return 404. The same lookup by name works for chutes that exist (`AudioDojo` and `Nemotron-3-Nano-Omni-30B-TEE` both return 200), so these 404s mean the chutes are gone.
- **The only public multilingual STT on chutes today is the community chute "AudioDojo"** (owner `vonkaiser`). It runs Whisper **large-v3-turbo** at `/stt/whisper`. Its readme says its `/speak` path is a drop-in replacement for the old `chutes/kokoro` chute, which suggests the official audio chutes were folded into it.
- **`llm.chutes.ai` has no `/v1/audio/transcriptions`.** An unauthenticated `POST` with JSON or multipart gets the same generic HTML 404 as a made-up path (`/v1/nonexistent-xyz`). `/v1/chat/completions` on the same host is routed (it answered 429 to the anonymous call). `/v1/audio/speech` is also 404.
- **Not verified:** I had no API key, so I made no authenticated call. Latency, the exact response shape and whether OGG/Opus is accepted all need a quick test with the user's key.

## Main option: AudioDojo `/stt/whisper`
Metadata from `api.chutes.ai/chutes/d6c66e0a-3e04-595b-825c-f3a65e50b2c7`:

| Field | Value |
|---|---|
| Endpoint | `POST https://vonkaiser-audiodojo.chutes.ai/stt/whisper` (or `https://api.chutes.ai/chutes/d6c66e0a-3e04-595b-825c-f3a65e50b2c7/stt/whisper`) |
| Auth | `Authorization: Bearer cpk_...` (`X-API-Key` also documented). Every path on `*.chutes.ai`, including made-up ones, answers 401 without a key, so a 401 does not prove an endpoint exists. |
| Body | JSON with base64 audio, **not multipart**: `{"audio_b64": str, "language": str or null, "return_timestamps": bool (default true)}`. The `args` wrapper in the cord's input_schema is internal: the API passes the JSON body to the function as its first argument (chutes-api `invocation/router.py`), so fields go at the top level, as the readme's curl shows. |
| Language | `language` is a BCP-47 code such as `ru` or `en`; if omitted it is auto-detected. There is **no `initial_prompt`**, so you can't bias the vocabulary. |
| Response | JSON `{text, chunks}`, with chunks only when `return_timestamps=true`. Exact chunk structure not verified (`output_schema` is null). |
| Formats | Schema says "Base64-encoded audio file (any common format)". **Whether OGG/Opus works is not verified.** |
| Runtime | 1 instance on RTX Pro 6000, TEE, hot. Instance verified at 2026-10-06 01:44 UTC. Concurrency 4, preemptible. The image readme says all models are loaded at startup and "Requests queue and execute sequentially", and the same queue is shared with the TTS endpoints. Created 2026-08-19. **Only 125 invocations in total**, so it gets very little use. |
| Cold start | No per-request cold start because the models stay in GPU memory. If no instance is up, the router waits zero seconds and returns **503 "No instances available (yet)"** at once (`get_chute_target_manager(..., max_wait=0)`). That fits the planned retry queue. |
| Price | The listing shows $1.80/h, i.e. $0.0005/s. From chutes-api source (`chute/util.py`, `gpu.py`): a public non-LLM chute is billed as `0.4 (pro_6000 multiplier) × ceil(duration_s) × $4.50/3600`, which is **$0.0005 for each started second**, minus any subscription discount. A 10 s note should take about 1–2 billed seconds, so $0.0005–0.001 per note. At 30 notes a day that is **under $1/month even if every message is a voice note**. Pay-as-you-go needs a positive balance; otherwise you get 402. |
| Latency | **Not measured.** Rough estimate: 0.2–0.5 s of GPU time for 10 s of audio with turbo, plus routing and TEE overhead, so **about 1–3 s end to end**. It can spike if TTS jobs are in the queue. |

## OGG/Opus conversion
Telegram voice notes are OGG/Opus at 48 kHz, about 15–25 KB for 10 s, which is about 30 KB in base64. The server probably decodes them through ffmpeg or librosa, but that is unverified. Recommended approach:
1. In the smoke test, send the raw OGG.
2. Add a config flag `STT_CONVERT=wav`. When it's on, run ffmpeg to produce 16 kHz mono PCM WAV, about 320 KB (430 KB as base64).
   - Write the WAV to a temp file, not a pipe. ffmpeg cannot fill in the WAV header size fields when writing to a pipe.
   - WAV also works for the Nemotron fallback, which only accepts wav or mp3.
3. Put `ffmpeg` in the Docker image (`apt-get install -y ffmpeg`). Billing is by compute time, so sending a larger file costs nothing extra.

## Mixed Russian/English
- Whisper's `language` takes one code.
  - With `ru` forced, English words may come out transliterated into Cyrillic.
  - With auto-detection on a 10 s clip, the language is occasionally misdetected (for example as `uk` or `bg`).
- Make it a setting `STT_LANGUAGE` with values `ru`, `en` or `auto`. Default to `auto`, or to `ru` if most notes are in Russian. Tune it on real notes.
- The LLM step that comes next can tidy the transcript's spelling while it splits the message into items.

## Minimal async httpx example
```python
import asyncio, base64, os, tempfile, httpx

STT_URL = os.getenv("CHUTES_STT_URL", "https://vonkaiser-audiodojo.chutes.ai/stt/whisper")
KEY = os.environ["CHUTES_API_KEY"]

async def ogg_to_wav16k(ogg: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src, dst = f"{d}/in.ogg", f"{d}/out.wav"
        open(src, "wb").write(ogg)
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000", dst,
            stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode:
            raise RuntimeError(err.decode())
        return open(dst, "rb").read()

class SttUnavailable(Exception):  # -> put the message in the retry queue
    pass

async def transcribe(client: httpx.AsyncClient, audio: bytes, language: str | None = "ru") -> str:
    body = {"audio_b64": base64.b64encode(audio).decode(), "return_timestamps": False}
    if language:
        body["language"] = language
    try:
        r = await client.post(STT_URL, json=body,
                              headers={"Authorization": f"Bearer {KEY}"},
                              timeout=httpx.Timeout(60, connect=10))
    except httpx.TransportError as e:
        raise SttUnavailable(str(e))
    if r.status_code in (429, 500, 502, 503, 504):   # 503 = "No instances available"
        raise SttUnavailable(f"{r.status_code} {r.text[:200]}")
    r.raise_for_status()                              # 401 bad key, 402 no balance, 404 chute/cord gone
    return (r.json().get("text") or "").strip()
```
At startup or as a health check, call `GET https://api.chutes.ai/chutes/?include_public=true&name=AudioDojo`. It needs no auth. It shows the chute still exists, its `hot` flag and its slug.

## Fallbacks on chutes, in order
1. **AudioDojo `/stt/parakeet`** (Parakeet TDT 0.6B v3). The body is `{"audio_b64", "timestamps"}` with no language parameter, and it returns `{text, words, segments}`. The chute's readme calls it English-only, but NVIDIA's model card lists 25 European languages including **ru and uk**, with automatic language detection. Russian quality on this deployment is unverified, and code-switching between languages isn't covered in the card. Same chute, so the same outage risk.
2. **`Nemotron-3-Nano-Omni-30B-TEE`** (an official chutes chute). Call it through `llm.chutes.ai/v1/chat/completions` with model `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16`, a content part `{"type":"audio_url","audio_url":{"url": ...}}` and the prompt "Transcribe this audio." NVIDIA recommends non-thinking mode with `temperature=1.0, top_k=1` for this. It accepts wav or mp3. The listing gives $0.0245 per 1M input tokens and $0.0978 per 1M output. **The NVIDIA card says audio is English only**, so this only helps for English notes. Whether chutes accepts a base64 `data:` URI here is unverified.
3. **Deploying your own Whisper chute: rule this out.** It costs a one-time $5.40 plus $1.80/h while the instance runs, about $1,300/month to keep it warm, which is far over the $5 budget.
4. **Outside chutes:** the planned retry queue (keep the `file_id` and the raw message, try again later). Later, local `faster-whisper` (small or medium, int8, on CPU) inside the same Docker Compose stack, which the user already mentioned as a possible future move.

## Open risks
- **Single point of failure.** AudioDojo is one person's community chute with one preemptible instance and very little traffic. It can be deleted, renamed or scaled down without notice. Keep URL, path and body in config, run the health check above, rely on the retry queue, and plan the local Whisper fallback.
- **Unverified pieces:** OGG acceptance, the exact response shape, real latency, and Parakeet's Russian quality. A 10-minute test with the user's key covers all four.
- **Shared queue.** "Requests queue and execute sequentially" with concurrency 4 and TTS traffic on the same instance means latency can spike.
- Billing rounds each request up to whole seconds and includes queue time. Pricing comes from the chutes-api `main` branch on 2026-10-06 and can change.
- I ignored the deployment guides `chutes.ai/docs/guides/modern-audio` and `/docs/examples/audio-processing`. They describe Whisper chutes you deploy yourself (`/transcribe`, `audio_b64`), not public endpoints.

## Sources (all read 2026-10-06)
- https://api.chutes.ai/chutes/?include_public=true&limit=100&page=0..4 (460 unique public chutes, scanned for audio)
- https://api.chutes.ai/chutes/d6c66e0a-3e04-595b-825c-f3a65e50b2c7 (AudioDojo: readme, cords, price, instances)
- https://api.chutes.ai/chutes/761ccdab-2e3d-5268-ab03-55f8ba94a80c (Nemotron Omni)
- https://api.chutes.ai/chutes/whisper-large-v3 (404), plus `/chutes/kokoro` and `/chutes/csm-1b` (404)
- https://llm.chutes.ai/v1/models; `POST https://llm.chutes.ai/v1/audio/transcriptions` (404)
- https://chutes.ai/docs/getting-started/running-a-chute (URL patterns, auth, error codes)
- https://www.chutes.ai/docs/api-reference/authentication
- https://www.chutes.ai/pricing
- https://github.com/chutesai/chutes-api: `api/invocation/router.py`, `api/invocation/util.py`, `api/chute/util.py`, `api/gpu.py` (body passing, `max_wait=0`/503, billing formula, quota/402)
- https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3
- https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16
- https://chutes.ai/docs/guides/modern-audio, https://chutes.ai/docs/examples/audio-processing (self-deploy guides only)

I wrote no project files. Downloaded JSON and source files are in `C:\Users\User\AppData\Local\Temp\claude\C--Work-Dump\7f3b8ff0-829e-45a9-ac5e-03d830d0f0a9\scratchpad\` (`chutes_*.json`, `audiodojo.json`, `omni.json`, `inv_router.py`, `inv_util.py`, `chute_util.py`).