from cdt.sdk import step


@step("offline.fetch_config")
def fetch_config(ctx, output: str):
    path = ctx.project_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    ctx.values["offline_config_path"] = str(path)
