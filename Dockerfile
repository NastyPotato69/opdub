FROM python:3.11-slim

# ffmpeg for all media I/O
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opdub/ ./opdub/

# /data is the mount point for host media files; outputs land in /data/out/
RUN mkdir -p /data/out

EXPOSE 8000

CMD ["uvicorn", "opdub.server:app", "--host", "0.0.0.0", "--port", "8000"]
