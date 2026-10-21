FROM python:3.12-slim

WORKDIR /app

COPY ml/inference/requirements.txt /tmp/requirements.txt
ENV PIP_RETRIES=10
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY ml /app/ml

ENV PYTHONPATH=/app \
    ARTIFACTS_DIR=/app/ml/artifacts \
    INFERENCE_PORT=8001

EXPOSE 8001
CMD ["python", "-m", "ml.inference.app"]
