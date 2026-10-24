# Third-party components

Booksmith application code is maintained separately from its dependencies. This release includes no model weights or third-party application source trees. Dependencies installed from the manifests and external tools keep their own licenses and notices.

- Kokoro ONNX: https://github.com/thewh1teagle/kokoro-onnx
- Kokoro and voice/model artifacts: https://github.com/hexgrad/kokoro and https://huggingface.co/hexgrad/Kokoro-82M
- eSpeak NG: https://github.com/espeak-ng/espeak-ng
- ffmpeg: https://ffmpeg.org/legal.html
- ebooklib: https://github.com/aerkalov/ebooklib
- PlexAPI: https://github.com/pkkid/python-plexapi
- faster-whisper: https://github.com/SYSTRAN/faster-whisper
- FastAPI, Uvicorn, Beautiful Soup, lxml, SoundFile, Mutagen, Pillow, Requests, NumPy, ONNX Runtime and their transitive packages: see the installed distributions and pinned manifests for their notices.

The optional GPU container uses a separately distributed base image and packages declared in Dockerfile.native. Model/provider terms are separate from Booksmith source licensing. Do not infer that one project license covers every model or external binary.
