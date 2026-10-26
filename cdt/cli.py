import typer

from . import __version__
from .cli_commands.agent_release import agent_release_app
from .cli_commands.history import history_app, run_logs, run_status
from .cli_commands.pipeline import pipeline_app
from .cli_commands.run import run_pipeline
from .cli_commands.settings import settings_app
from .cli_commands.utility import doctor, init_project, print_schema, self_update

app = typer.Typer(no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cdt {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
):
    """CDT CLI entry point."""
    return


app.command(name="self-update")(self_update)
app.command(name="doctor")(doctor)
app.command(name="schema")(print_schema)
app.command(name="init")(init_project)
app.command(name="status")(run_status)
app.command(name="logs")(run_logs)
app.command(name="run")(run_pipeline)

app.add_typer(settings_app, name="settings")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(agent_release_app, name="agent-release")
app.add_typer(history_app, name="history")


if __name__ == "__main__":
    app()
