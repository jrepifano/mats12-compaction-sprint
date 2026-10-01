FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN mkdir -p /srv/control /var/log/ops
COPY server.py /app/server.py
EXPOSE 8080
CMD ["python3", "/app/server.py"]
