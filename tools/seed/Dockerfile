FROM python:3.12-slim

WORKDIR /app

COPY tools/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY ml /app/ml
COPY tools /app/tools

ENV PYTHONPATH=/app
CMD ["python", "-m", "tools.seed.seed"]
