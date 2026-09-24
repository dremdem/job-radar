from typing import Annotated

import typer

from .config import Settings
from .db import upgrade_database
from .models import SourceType

app = typer.Typer(help="Job Radar: local-first job opportunity review.", no_args_is_help=True)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations to the configured database."""
    settings = Settings()
    upgrade_database(settings.database_url, revision)
    typer.echo(f"Database is at {revision}: {settings.database_url}")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on code changes.")] = False,
) -> None:
    """Run the web UI."""
    import uvicorn

    uvicorn.run("job_radar.web.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def sync(source: SourceType) -> None:
    """Import new items from a source."""
    typer.echo(f"sync {source.value}: not implemented yet", err=True)
    raise typer.Exit(code=1)
