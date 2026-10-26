FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg && pip install --no-cache-dir "boto3>=1.35,<2"
WORKDIR /app
COPY app/app.py app/index.html ./
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "app.py"]
