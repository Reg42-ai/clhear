FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite:////tmp/clhear.db \
    CLHEAR_SCOPES_DIR=/tmp/scopes \
    CLHEAR_ARTIFACTS_DIR=/tmp/artifacts \
    CLHEAR_LOCAL_SOURCES_DIR=/sources

WORKDIR /opt/clhear

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin clhear \
    && mkdir -p /tmp/scopes \
    && chown -R 10001:10001 /opt/clhear /tmp/scopes

COPY pyproject.toml README.md LICENSE ./
COPY app ./app
COPY migrations ./migrations
COPY openapi ./openapi

RUN pip install --no-cache-dir .

USER 10001

EXPOSE 8000

ENTRYPOINT ["clhear"]
CMD ["serve"]
