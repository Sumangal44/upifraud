FROM python:3.12-slim

WORKDIR /app

COPY ml/xai/requirements.txt /tmp/requirements.txt
ENV PIP_RETRIES=10
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY ml /app/ml

ENV PYTHONPATH=/app \
    ARTIFACTS_DIR=/app/ml/artifacts \
    XAI_PORT=8002

EXPOSE 8002
CMD ["python", "-m", "ml.xai.app"]
