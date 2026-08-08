# Blockchain IoT Incentive Simulation Platform

## Database Layer

The SQLite database layer is available through `src.database.Database`.

```python
from src.database import Database

database = Database("data/platform.sqlite3")
database.initialize()

environment_id = database.configurations.create_environment(
    name="Research environment",
)

history = database.simulations.get_simulation_history()
```

Generic parameterized CRUD operations are available directly on `database`.
Configuration-specific operations are grouped under
`database.configurations`, while run data and dashboard queries are grouped
under `database.simulations`.

## Core Simulation

The UI-independent core can create grouped IoT environments and execute a
saved comparison entirely with virtual time.

```python
from src.core import SimulationEngine

engine = SimulationEngine(database)
result = engine.run_experiment(experiment_config_id)
```

Both blockchain engines receive the same persisted Poisson event stream.
Network-specific transactions, blocks, device snapshots, metrics, and the
final comparison are written through the database layer.

## Streamlit Application

Run the complete research interface from the project root:

```powershell
venv\Scripts\streamlit.exe run streamlit_app.py
```

The application stores data in `data/simulation_platform.sqlite3` by default.
Set `SIM_PLATFORM_DB_PATH` to use another SQLite database file.

## Automated Tests

Install the development dependencies and run the complete suite:

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest
```

The suite covers database integration, core business logic, Streamlit user
flows, error handling, configuration portability, and a 100-device virtual
clock performance scenario.
