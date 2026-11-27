# Third-party components

The APK acquisition adapter uses **alltechdev/gplay-apk-downloader**, retained
as a Git submodule at the revision recorded in `upstream.json`.

- Source: https://github.com/alltechdev/gplay-apk-downloader
- License: GPL-3.0, provided in the submodule's `LICENSE` file
- Upstream source is unmodified; AppTrace adapters are in `collector/` and `scripts/`.

The Play discovery worker uses **facundoolano/google-play-scraper** through the
versions locked in `discovery-worker/package-lock.json`.

Python and Node dependencies retain their own licenses. EmbeddingGemma 2 weights
are downloaded separately from the official model repository; they are not
included in this source repository. See its model card for license and usage
information: https://huggingface.co/google/embeddinggemma-2
