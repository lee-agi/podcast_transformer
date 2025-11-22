FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \ 
    && apt-get install -y --no-install-recommends ffmpeg \ 
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY any2summary /app/any2summary
COPY podcast_transformer /app/podcast_transformer
COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip \ 
    && pip install --no-cache-dir .

ENTRYPOINT ["any2summary"]
