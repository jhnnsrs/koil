---
sidebar_position: 5
---

# Composition

`koil` supports composition of async models, particularly useful when building complex applications with `pydantic`.

The `KoiledModel` and `Composition` classes allow you to create hierarchies of objects that share the same async context.

```python
from koil.composition import KoiledModel, Composition
from pydantic import Field

class Database(KoiledModel):
    async def __aenter__(self):
        print("Connecting DB")
        return self

class API(KoiledModel):
    async def __aenter__(self):
        print("Connecting API")
        return self

class App(Composition):
    db: Database = Field(default_factory=Database)
    api: API = Field(default_factory=API)

# When entering App, it automatically enters db and api
with App() as app:
    # app.db and app.api are initialized
    pass
```
