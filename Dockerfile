FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir . && useradd -r -u 10001 app && mkdir /data && chown app /data
# only what app/api/static.py serves (not the JS tests or package.json)
COPY web/index.html ./web/index.html
COPY web/css ./web/css
COPY web/fonts ./web/fonts
COPY web/vendor ./web/vendor
COPY web/js ./web/js
ENV WEB_DIR=/srv/web
USER app
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/api/health', timeout=3)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
