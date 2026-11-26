FROM python:3.11-slim

LABEL org.opencontainers.image.title="traduia-backstage" \
      org.opencontainers.image.description="TraduIA Backstage: operator application and orchestrator for real-time speech translation" \
      org.opencontainers.image.source="https://github.com/BSC-AppliedIntelligence/traduia" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip "setuptools<82" wheel
RUN echo "setuptools<82" > /tmp/build-constraints.txt
ENV PIP_CONSTRAINT=/tmp/build-constraints.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
