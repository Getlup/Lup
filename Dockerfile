FROM python:3.11-slim

# ffmpeg للقص والتحويل + fonts-noto-core للخط العربي (Noto Naskh/Sans Arabic)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY podlup.py .

# Railway/Render يحقنان PORT تلقائياً — الكود يقرأه من البيئة
CMD ["python3", "podlup.py"]
