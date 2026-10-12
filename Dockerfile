# Headless NetTransfer CLI (no Qt, no GUI) for jump servers and pipelines:
#
#   docker build -t nettransfer-cli .
#   docker run --rm -v "$PWD:/data" nettransfer-cli hash /data/fw.bin
#   docker run --rm --network host -v "$PWD:/data" nettransfer-cli serve http /data/fw.bin --once
#   docker run --rm -v "$PWD:/data" -e DEVICE_PW nettransfer-cli push sftp /data/fw.bin 10.0.0.1 -u admin --password-env DEVICE_PW
#
# TFTP needs UDP 69: add `--sysctl net.ipv4.ip_unprivileged_port_start=0` (the process is not root) or use --port 6969.
# Containers should use `--network host` (or publish the port) so devices on the lab network can reach the server.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/nettransfer
COPY requirements-cli.txt ./
RUN pip install -r requirements-cli.txt

COPY app ./app

# Not root: a pushed-to device and a served file never need it. known_hosts and audit logs live in the volume below.
RUN useradd --system --create-home --uid 10001 nettransfer \
    && mkdir -p /data /home/nettransfer/.config/nettransfer \
    && chown -R nettransfer /data /home/nettransfer
USER nettransfer
WORKDIR /data
VOLUME ["/home/nettransfer/.config/nettransfer"]
EXPOSE 69/udp 8080/tcp 8765/tcp

ENV PYTHONPATH=/opt/nettransfer
ENTRYPOINT ["python", "-m", "app.cli"]
CMD ["--help"]
