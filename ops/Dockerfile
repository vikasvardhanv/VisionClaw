FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.10,<2" httpx uvicorn
COPY ops_server.py .
ENV PORT=8790 OPS_STATE=/data/approvals.json
VOLUME /data
EXPOSE 8790
CMD ["python", "ops_server.py"]
