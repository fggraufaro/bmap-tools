FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

# Railway sets PORT at runtime; the app reads it via os.environ.
CMD ["python", "rate_radar.py"]
