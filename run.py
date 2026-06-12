"""Inicia o backend do CRM na porta 8001. Execute: python run.py"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
