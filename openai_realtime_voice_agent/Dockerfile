# Home Assistant add-on image.
#
# Uses the Debian (glibc) base image instead of Alpine (musl). Heavy Python
# dependencies pulled in transitively by pipecat (llvmlite, numba, ...) ship
# prebuilt manylinux wheels for glibc, so they install without compiling
# against a specific LLVM. The previous Alpine build compiled llvmlite from
# source and broke once it resolved to llvmlite 0.47 (which requires LLVM 20)
# while the image only provided LLVM 15.
#
# Override the base per-arch via build.yaml (build_from) / --build-arg BUILD_FROM.
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm
FROM ${BUILD_FROM}

ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime deps + minimal build insurance. Most Python deps install from wheels;
# build-essential/python3-dev only cover the rare source-only package.
# ffmpeg + libsndfile1 cover audio decoding/encoding paths in pipecat.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        build-essential \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Install the app and its dependencies from pyproject (PEP 517 via poetry-core).
# No poetry.lock is used, matching upstream behaviour; on glibc this resolves
# to wheels and needs no native LLVM/cmake build.
COPY pyproject.toml /tmp/app_build/pyproject.toml
COPY app/ /tmp/app_build/app/
RUN PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    pip3 install /tmp/app_build \
    && rm -rf /tmp/app_build

# Pipecat 0.0.97 lazily downloads this NLTK tokenizer at first startup. Bundle
# the verified archive in the build context so neither build nor startup needs
# the flaky NLTK/GitHub download path.
COPY nltk_data/punkt_tab.zip /tmp/punkt_tab.zip
RUN python3 -c "import zipfile; zipfile.ZipFile('/tmp/punkt_tab.zip').extractall('/usr/local/share/nltk_data/tokenizers')" \
    && rm /tmp/punkt_tab.zip

# Pipecat 0.0.97 unconditionally calls nltk.download("punkt_tab") at import
# time.  The model is already bundled above; suppressing the redundant network
# request makes add-on restarts reliable when NLTK's index endpoint is flaky.
RUN sed -i 's/^[[:space:]]*nltk.download("punkt_tab", quiet=True)$/        pass  # punkt_tab bundled in image/' /usr/local/lib/python3.11/dist-packages/pipecat/utils/string.py

# App code (kept at /app for `python3 -m app.main` and easy inspection)
COPY app/ /app/

# s6-overlay / bashio run script
COPY root/ /
RUN chmod a+x /run.sh

WORKDIR /
CMD ["/run.sh"]
