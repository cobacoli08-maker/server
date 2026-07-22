FROM python:3.11-slim

WORKDIR /app

# pinscrape imports OpenCV. Debian slim does not include these runtime libs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY metadata_requirements.txt .

RUN pip install --no-cache-dir -r metadata_requirements.txt

COPY . .

CMD ["python", "metadata_server.py"]
