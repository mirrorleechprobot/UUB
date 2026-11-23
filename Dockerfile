FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV MALLOC_ARENA_MAX=2
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg aria2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && pip install --no-cache-dir -r requirements.txt
# cbgram (optional 2nd engine, UPLOAD_MODE=cbgram). It also provides the module name `pyrogram`, so it goes into its OWN folder
# and never overwrites wzgram (default). Build without it: --build-arg INSTALL_CBGRAM=false
ARG INSTALL_CBGRAM=true
COPY requirements-cbgram.txt .
RUN mkdir -p /app/engines/cbgram && if [ "$INSTALL_CBGRAM" = "true" ]; then \
      pip install --no-cache-dir --target /app/engines/cbgram -r requirements-cbgram.txt \
      || echo "WARNING: cbgram could not be installed - UPLOAD_MODE=cbgram will fall back to wzgram"; fi
COPY . .
RUN mkdir -p /app/downloads /app/data
EXPOSE 8080
CMD ["python", "-u", "bot.py"]
