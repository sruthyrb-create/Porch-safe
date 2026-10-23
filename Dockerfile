FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY porchsafe ./porchsafe
COPY fixtures ./fixtures
ENV HOST=0.0.0.0 PORT=8090 PORCHSAFE_DATA=/data
VOLUME ["/data"]
EXPOSE 8090
CMD ["python", "-m", "porchsafe.app"]
