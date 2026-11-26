FROM python:3.11-slim

LABEL org.opencontainers.image.title="traduia-stage" \
      org.opencontainers.image.description="TraduIA Stage: legacy listener client" \
      org.opencontainers.image.source="https://github.com/BSC-AppliedIntelligence/traduia" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
