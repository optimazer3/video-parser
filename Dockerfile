FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ARG WITH_WHISPER=0
COPY requirements.txt requirements-whisper.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_WHISPER" = "1" ]; then pip install --no-cache-dir -r requirements-whisper.txt; fi

COPY bot ./bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "bot"]
