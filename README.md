# Booksmith

Choose an ebook in Calibre. Pick a narrator. Listen to the finished audiobook in Plex.

Booksmith is an early Windows-first application for turning DRM-free ebooks into local, chaptered audiobooks. Its focus is the whole path: a searchable library, reproducible narration settings, contextual pronunciation and timing, resumable conversions, quality checks, reviewed repairs and verified Plex delivery.

## What it does

- Read-only Calibre search; EPUB/TXT extraction, plus MOBI/AZW3 through a configured Calibre container.
- Local Kokoro narration on CPU, with an optional pinned native CUDA service. New jobs snapshot their renderer; an interrupted GPU edition waits for its original renderer.
- Saved reading profiles and book/series pronunciation dictionaries. Optional Jev suggestions for individual ambiguous word occurrences require approval.
- **Two-level contextual pauses:** Jev proposes sentence-level boundaries, then reviews boundaries in complete paragraph context, comparing timing variants using surrounding text, measured silence and safe edit locations. It can keep, remove or introduce beats. The final plan inserts only the necessary silence once.
- A durable SQLite queue with restart recovery, immutable source/profile records and retained renderer/annotation provenance.
- Numbered, tagged chapter MP3s, cover art, checksummed staging and Plex API import verification. Plex databases are never modified directly.
- Optional local Whisper completeness screening and chapter repair with a listening approval gate. Sampled recognition is a screening tool, not a guarantee of perfect narration.

The tight timing profile uses total-gap targets around 150 ms for clause separation, 300 ms for conversational beats, 650 ms for reflection and 1.1 seconds for a scene reset. It preserves naturally longer pauses. Models annotate source boundaries; they never rewrite the book. Single-narrator speech is implemented; character casting and general acting/style prompts remain future work.

## Start locally

This is a source release, not a turnkey installer. Python 3.12, ffmpeg, a readable Calibre library and an existing Plex Media Server are required. Windows is currently required by the service lock, scheduled task and Plex credential discovery. Linux/macOS support is not established.

1. Create a virtual environment and install `requirements.lock`. For tests, also install pytest and httpx.
2. Edit the supplied generic `config.json`: set your own runtime, Calibre, output/staging and Plex-visible paths. Staging must be outside every Plex library and on the same filesystem as the final destination. All example paths are placeholders.
3. Run `python download_models.py` to fetch and verify the pinned model and voice artifacts. Weights are not included in this repository.
4. With Plex running under your account, run `python configure_plex.py`. It creates or validates a dedicated **Booksmith** music library with local metadata and track offsets. Inspect the configured target first; do not point it at an unrelated media library.
5. Run `python run.py` from the project environment and open `http://127.0.0.1:8787`.

The example binds only to loopback. No credentials, public route or hosted-provider account is supplied. Keep it local until authentication and network/proxy settings are configured for your deployment. Authentication uses a private runtime `data/access.json`; deployment-specific reverse proxy provisioning is intentionally outside this release. Never commit that file, provider connections, Plex tokens, private configuration, books or generated media.

`setup.ps1` combines dependency/model/Plex/startup setup after configuration. Startup is currently tied to a signed-in Windows user. `launch.ps1` checks/starts the service; `stop.ps1` disables its watchdog and preserves progress. The public configuration uses CPU narration. The optional CUDA service is experimental, has a pinned model/runtime identity and strict local authentication, and requires separately provisioned artifacts and private connection files. Its current resource guard also assumes Frigate; adapt that integration before using GPU mode on a different stack. Do not assume benchmarks from another machine describe yours.

## Optional hosted Jev annotation

Speech generation is local. Contextual pronunciation and timing may send ebook passages to a hosted decision model when you explicitly configure and enable those features.

- Point `jev_connection_file` at a private JSON connection file containing `{"headers":{"Authorization":"Bearer YOUR_OWN_KEY"}}`, using the headers required by your route. Keep the real file outside Git.
- Set `jev_url` to your compatible Jev decision endpoint. The implementation uses a typed `/Jev/decide` interface, not a generic chat-completions prompt.
- Set `contextual_pauses:true` to enable two-level timing for new conversions; users can disable it per book. The public example leaves it off and contains no working provider URL or credentials.
- `jev_monthly_limit_usd` and `jev_owner_limit_usd` bound recorded Jev usage. Monthly accounting uses UTC, with reservations before send and no automatic retry after uncertain billing. This does not meter unrelated accounts or providers. Provider failures and oversized/unaligned paragraphs retain baseline timing with an explicit receipt.

In a small original six-passage experiment, 29 sentence decisions plus nine paragraph decisions cost about $0.0019. Paragraph review revised three boundary plans. That is exploratory evidence, not a whole-book cost or quality guarantee. Human listening remains the deciding test.

## Tests and limits

Run `python -m pytest tests -q`. The initial source release was checked with 189 tests covering extraction/source coverage, edition identity, resumable caches, privacy boundaries, timing plans, pronunciation approvals, local QA, MP3 repair/publication and Plex API checks. Tests use isolated stores and mocked providers; they do not contact a hosted model or your real media services. The ffmpeg integration tests require ffmpeg on PATH. Local ASR is optional in a separate environment using `requirements-qa.lock`.

Use `node --check web/app.js` and `python refresh_assets.py --check` after frontend changes. Parser ambiguity, unusual sentence punctuation, long-form listening quality, actual Plex client resume behavior, signed-out startup and portability remain active work. PDF/OCR, DRM removal and multi-character narration are not implemented. No books, generated voices/audio, model weights, private operational logs or previous private history are included here.

## Design references

Booksmith owns one extraction/plan/queue/render/QA/delivery pipeline. It does not combine other applications' queues or interfaces. Related projects studied for ideas include [ebook2audiobook](https://github.com/DrewThomasson/ebook2audiobook), [Folivox](https://github.com/vatavale/folivox), [Alexandria](https://github.com/Finrandojin/alexandria-audiobook) and [Audiblez](https://github.com/santinic/audiblez). Their application source has not been vendored into this release.

The implementation depends on [Kokoro ONNX](https://github.com/thewh1teagle/kokoro-onnx), [Kokoro](https://github.com/hexgrad/kokoro), eSpeak, FastAPI, ebooklib, SoundFile, ffmpeg, Mutagen, PlexAPI and optional faster-whisper. These libraries, tools and model artifacts retain their own licenses. See `THIRD_PARTY_NOTICES.md`.

Pause presets are engineering choices informed by listening and [English inter-sentence pause research](https://www.isca-archive.org/speechprosody_2024/owoicho24_speechprosody.pdf), not universal narration standards. No affiliation with Calibre, Plex or the model providers is implied.

## Project licensing

The project license has not yet been selected. Public source availability does not grant additional licensing rights. Third-party licenses remain applicable to their respective components.
