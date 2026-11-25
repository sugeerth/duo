# AgentLens Cloud: docker build -f cloud/Dockerfile -t agentlens-cloud . && docker run -p 8080:8080 -v agentlens:/data agentlens-cloud
FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV AGENTLENS_CLOUD_DATA=/data PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3)"
CMD ["python3","cloud/server.py"]
