FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-tested-constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements-tested-constraints.txt
COPY app ./app
COPY static ./static
COPY examples ./examples
COPY scripts ./scripts
COPY docs ./docs
RUN useradd -r -u 10001 ontology && mkdir -p /app/data && chown -R ontology:ontology /app/data
USER ontology
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
