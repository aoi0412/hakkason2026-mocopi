FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HTTP_PORT=8002 \
    WS_PORT=8765 \
    MOCOPI_PORT=12351 \
    DATA_DIR=/data

WORKDIR /app
COPY mocopi_server.py ./mocopi_server.py
COPY mocopi-posture-monitor.html ./mocopi-posture-monitor.html
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8002/tcp 8765/tcp 12351/udp
CMD ["python", "mocopi_server.py"]
