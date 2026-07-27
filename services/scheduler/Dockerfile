FROM python:3.11-alpine
RUN apk add --no-cache docker-cli docker-cli-compose
WORKDIR /app
COPY main.py .
CMD ["python", "main.py"]
