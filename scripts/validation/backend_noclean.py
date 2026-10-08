"""Backend do dokładania instancji w trakcie kampanii.

Zwykły backend przy starcie usuwa wszystkie kontenery symulatora (osierocone po
poprzednim procesie), co przerwałoby przebiegi innych, już działających instancji.
Ten wariant pomija tylko to sprzątanie; resztę aplikacji uruchamia bez zmian.

    PYTHONPATH=backend SIM_NODE_ACCESS=host .venv/bin/python scripts/validation/backend_noclean.py 8107
"""

import sys

if __name__ == "__main__":
    # Osłona jest konieczna: precompute ITM używa puli procesów „spawn”, która importuje
    # ten plik w każdym procesie potomnym.
    import uvicorn

    from app.services.node_containers import NodeContainers

    NodeContainers.cleanup_all_sim_containers = staticmethod(lambda: [])

    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="info")
