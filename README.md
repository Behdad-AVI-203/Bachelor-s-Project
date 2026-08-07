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
