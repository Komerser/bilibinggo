FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BINGGO_HOME=/var/lib/binggo BINGGO_PORT=8182 TZ=Asia/Shanghai
WORKDIR /app
COPY app/packaging/linux/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.lock \
    && groupadd -g 10001 binggo \
    && useradd -u 10001 -g binggo --no-create-home --shell /usr/sbin/nologin binggo
COPY app/ /app/
USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["python", "packaging/linux/server.py"]
