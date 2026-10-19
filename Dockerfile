FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV MALLOC_ARENA_MAX=2
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg aria2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/downloads /app/data
EXPOSE 8080
CMD ["python", "-u", "bot.py"]
