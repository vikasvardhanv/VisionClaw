FROM python:3.12-slim
WORKDIR /app
# tzdata: zoneinfo America/New_York. git: day-trader-skill is cloned, never copied (it has no license).
RUN apt-get update && apt-get install -y --no-install-recommends git tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && git clone -q https://github.com/Jakeschincariol/day-trader-skill /opt/dts \
 && git -C /opt/dts checkout -q 3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd
RUN pip install --no-cache-dir "mcp>=1.10,<2" httpx uvicorn
COPY ops_server.py trader_job.py trader_config.json ./
ENV PORT=8790 OPS_STATE=/data/approvals.json TRADER_DIR=/data/trader-home
VOLUME /data
EXPOSE 8790
CMD ["python", "ops_server.py"]
