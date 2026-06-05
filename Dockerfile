FROM python:3.11-slim

WORKDIR /app

COPY metadata_requirements.txt .

RUN pip install --no-cache-dir -r metadata_requirements.txt

COPY . .

CMD ["python", "metadata_server.py"]