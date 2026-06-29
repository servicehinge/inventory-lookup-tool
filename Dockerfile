# Inventory lookup JSON API（給 LINE bot 的 GAS 呼叫）。
# 適用 Hugging Face Spaces (Docker) / Cloud Run / Render。
# 需要環境變數：HUBSPOT_API_TOKEN、GCP_SERVICE_ACCOUNT_JSON（整段 SA JSON）、API_KEY。
FROM python:3.11-slim
WORKDIR /app
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt
COPY api.py inventory_core.py us_inventory.py ./
ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-7860}"]
