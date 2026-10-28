FROM python:3.12-slim

WORKDIR /workspace
COPY receiver.py /app/receiver.py

ENV MOCOPI_UDP_PORT=12351
EXPOSE 12351/udp

CMD ["python", "-u", "/app/receiver.py"]
