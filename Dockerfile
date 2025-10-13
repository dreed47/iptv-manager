# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install ffmpeg (and clean up apt cache to keep the image small)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure data.db and m3u_files directory are writable
RUN mkdir -p /app/m3u_files && chmod -R 777 /app

EXPOSE 5005

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5005"]
