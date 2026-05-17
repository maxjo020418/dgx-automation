FROM python:3.12-slim

WORKDIR /app
COPY gpu_exporter.py /app/gpu_exporter.py

EXPOSE 9400
CMD ["python", "/app/gpu_exporter.py"]
