FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grocery_agent ./grocery_agent

EXPOSE 8080

CMD ["uvicorn", "grocery_agent.api:app", "--host", "0.0.0.0", "--port", "8080"]
