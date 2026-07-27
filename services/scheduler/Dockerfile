FROM python:3.11-alpine
RUN apk add --no-cache docker-cli
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["python", "main.py"]
