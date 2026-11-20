FROM python:3.13-slim
WORKDIR /app
COPY publisher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY publisher/core.py publisher/main.py ./
ENV PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["python3", "main.py"]
