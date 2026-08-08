FROM python:3.11-slim

# ffmpeg/ffprobe are the only media I/O this project uses.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opdub ./opdub

# Media roots are mounted read-only at run time; only /out is writable.
ENV OPDUB_MEDIA=/media/edits:/media/sources \
    OPDUB_OUT=/out

EXPOSE 8000
CMD ["uvicorn", "opdub.server:app", "--host", "0.0.0.0", "--port", "8000"]
